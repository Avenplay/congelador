import streamlit as st
import requests
from supabase import create_client, Client
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List, Literal

# ==========================================
# 1. CONFIGURACIÓN DE PÁGINA Y CONEXIONES
# ==========================================
st.set_page_config(
    page_title="Gestor de Congelador IA",
    page_icon="🧊",
    layout="wide"
)

@st.cache_resource
def iniciar_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

@st.cache_resource
def iniciar_gemini() -> genai.Client:
    return genai.Client(api_key=st.secrets["GEMINI_API_KEY"])

supabase = iniciar_supabase()
gemini_client = iniciar_gemini()
TELEGRAM_TOKEN = st.secrets["TELEGRAM_TOKEN"]

# ==========================================
# 2. ESQUEMA DE DATOS PARA LA IA (PYDANTIC)
# ==========================================
class ProductoExtraido(BaseModel):
    categoria_tabla: Literal["plato_preparado", "ingrediente"] = Field(
        description=(
            "Clasifica en 'plato_preparado' si es comida ya cocinada lista para descongelar y comer "
            "(ej. filetes de pollo en salsa, lentejas, lasaña). "
            "Clasifica en 'ingrediente' si es materia prima para cocinar o acompañamiento base "
            "(ej. cebolla picada, pechuga cruda, gambas crudas, verduras troceadas)."
        )
    )
    nombre: str = Field(
        description="Nombre limpio del producto con la primera letra en mayúscula (ej. 'Filete de pollo en salsa', 'Cebolla picada')."
    )
    tipo_alimento: Literal["Carne", "Pescado", "Verdura", "Otros"] = Field(
        description="Familia principal del alimento: 'Carne', 'Pescado', 'Verdura' u 'Otros'."
    )
    envase: str = Field(
        description="Tipo y capacidad del envase mencionado (ej. 'Tupper 1L', 'Tupper 6L', 'Bolsa', 'Ración'). Si no se especifica, poner 'Estándar'."
    )
    cantidad: int = Field(
        description="Cantidad numérica de envases o unidades (mínimo 1)."
    )

class ResultadoAnalisisIA(BaseModel):
    entendido: bool = Field(
        description="True si el audio o texto se comprende con claridad y menciona alimentos. False si hay mucho ruido, está vacío o no tiene sentido."
    )
    mensaje_error: str = Field(
        description="Si entendido es False, explica brevemente por qué no se pudo procesar para avisar al usuario. Si es True, dejar vacío."
    )
    productos: List[ProductoExtraido] = Field(
        description="Lista de productos extraídos del mensaje."
    )

# ==========================================
# 3. FUNCIONES DE TELEGRAM Y GEMINI
# ==========================================
def obtener_actualizaciones_telegram():
    """Consulta los mensajes y notas de voz pendientes en Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    resp = requests.get(url, timeout=10).json()
    if resp.get("ok"):
        return resp.get("result", [])
    return []

def confirmar_offset_telegram(ultimo_update_id: int):
    """Limpia la cola de Telegram para no volver a procesar mensajes antiguos."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    requests.get(url, params={"offset": ultimo_update_id + 1}, timeout=10)

def borrar_mensaje_telegram(chat_id: int, message_id: int):
    """Elimina la nota original del chat tras procesarla con éxito."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage"
    requests.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=10)

def enviar_respuesta_telegram(chat_id: int, texto: str):
    """Envía al usuario una confirmación o un aviso de error por Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": texto}, timeout=10)

def descargar_audio_telegram(file_id: str) -> bytes:
    """Descarga los bytes de la nota de voz desde los servidores de Telegram."""
    url_info = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
    info = requests.get(url_info, params={"file_id": file_id}, timeout=10).json()
    file_path = info["result"]["file_path"]
    url_descarga = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    return requests.get(url_descarga, timeout=15).content

def analizar_con_gemini(audio_bytes: bytes = None, texto: str = None) -> ResultadoAnalisisIA:
    """Envía el audio o texto a Gemini 2.5 Flash y devuelve la estructura validada."""
    instrucciones = (
        "Eres el asistente de inventario de un congelador doméstico. "
        "Analiza la entrada del usuario y extrae todos los productos mencionados. "
        "Distingue rigurosamente entre platos ya cocinados ('plato_preparado') y "
        "materia prima sin cocinar o base ('ingrediente'). "
        "Clasifica cada uno en Carne, Pescado, Verdura u Otros. "
        "Si el audio es ininteligible, puro ruido o no habla de comida/congelador, "
        "marca 'entendido' como False y redacta un aviso amable en 'mensaje_error'."
    )

    contenidos = [instrucciones]
    if audio_bytes:
        contenidos.append(
            types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg")
        )
    elif texto:
        contenidos.append(f"Mensaje del usuario: {texto}")

    respuesta = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=contenidos,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ResultadoAnalisisIA,
            temperature=0.1
        )
    )
    return ResultadoAnalisisIA.model_validate_json(respuesta.text)

# ==========================================
# 4. FUNCIONES DE BASE DE DATOS (SUPABASE)
# ==========================================
def guardar_producto_supabase(prod: ProductoExtraido):
    """Inserta el producto o suma su cantidad si ya existe el mismo nombre y envase."""
    tabla = "platos_preparados" if prod.categoria_tabla == "plato_preparado" else "ingredientes"

    existente = (
        supabase.table(tabla)
        .select("*")
        .ilike("nombre", prod.nombre.strip())
        .ilike("envase", prod.envase.strip())
        .execute()
    )

    if existente.data:
        registro = existente.data[0]
        nueva_cantidad = registro["cantidad"] + prod.cantidad
        supabase.table(tabla).update({"cantidad": nueva_cantidad}).eq("id", registro["id"]).execute()
    else:
        supabase.table(tabla).insert({
            "nombre": prod.nombre.strip(),
            "tipo_alimento": prod.tipo_alimento,
            "envase": prod.envase.strip(),
            "cantidad": prod.cantidad
        }).execute()

def consumir_unidad(tabla: str, item_id: int, cantidad_actual: int):
    """Resta 1 unidad al stock o elimina la fila si era la última unidad."""
    if cantidad_actual > 1:
        supabase.table(tabla).update({"cantidad": cantidad_actual - 1}).eq("id", item_id).execute()
    else:
        supabase.table(tabla).delete().eq("id", item_id).execute()

def eliminar_registro(tabla: str, item_id: int):
    """Elimina el registro completo de la base de datos."""
    supabase.table(tabla).delete().eq("id", item_id).execute()

# ==========================================
# 5. INTERFAZ DE USUARIO EN STREAMLIT
# ==========================================
st.title("🧊 Gestor Inteligente de Congelador")

tab_actualizacion, tab_inventario = st.tabs([
    "🔄 1. Actualizar desde Telegram",
    "📦 2. Inventario del Congelador"
])

# ------------------------------------------
# PESTAÑA 1: ACTUALIZACIÓN (TELEGRAM + IA)
# ------------------------------------------
with tab_actualizacion:
    st.subheader("Sincronización de Notas de Voz y Mensajes")
    st.write(
        "Envía una nota de voz a tu bot de Telegram diciendo lo que guardas en el congelador "
        "y pulsa el botón inferior para que la IA lo clasifique, actualice el stock y limpie la nota."
    )

    if st.button("🚀 Buscar y procesar nuevas notas de Telegram", type="primary"):
        with st.spinner("Conectando con Telegram y analizando con Gemini IA..."):
            updates = obtener_actualizaciones_telegram()

            if not updates:
                st.info("No hay notas de voz ni mensajes nuevos pendientes en Telegram.")
            else:
                ultimo_id = 0
                procesados_ok = 0
                errores_ia = 0

                for u in updates:
                    ultimo_id = max(ultimo_id, u["update_id"])
                    mensaje = u.get("message")
                    if not mensaje:
                        continue

                    chat_id = mensaje["chat"]["id"]
                    message_id = mensaje["message_id"]
                    voz = mensaje.get("voice")
                    texto = mensaje.get("text")

                    # Ignorar el comando inicial /start
                    if texto and texto.strip().startswith("/start"):
                        continue

                    try:
                        if voz:
                            audio_bytes = descargar_audio_telegram(voz["file_id"])
                            resultado = analizar_con_gemini(audio_bytes=audio_bytes)
                        elif texto:
                            resultado = analizar_con_gemini(texto=texto)
                        else:
                            continue

                        # Verificar si la IA entendió la nota de voz
                        if not resultado.entendido or not resultado.productos:
                            motivo = resultado.mensaje_error or "No pude distinguir los productos ni las cantidades."
                            aviso_telegram = (
                                f"⚠️ Aviso: No he podido entender bien la nota de voz ({motivo}). "
                                "Por favor, vuelve a enviarla indicando el producto, envase y cantidad."
                            )
                            enviar_respuesta_telegram(chat_id, aviso_telegram)
                            st.warning(f"Nota no comprendida. Se ha enviado un aviso a tu Telegram: {motivo}")
                            errores_ia += 1
                        else:
                            resumen_lineas = []
                            for prod in resultado.productos:
                                guardar_producto_supabase(prod)
                                etiq = "🍲 Plato preparado" if prod.categoria_tabla == "plato_preparado" else "🥕 Ingrediente"
                                resumen_lineas.append(
                                    f"• {etiq}: {prod.nombre} | {prod.envase} | x{prod.cantidad} ({prod.tipo_alimento})"
                                )

                            # Confirmar en Telegram y borrar la nota original procesada
                            texto_confirmacion = "✅ Añadido al congelador:\n" + "\n".join(resumen_lineas)
                            enviar_respuesta_telegram(chat_id, texto_confirmacion)
                            borrar_mensaje_telegram(chat_id, message_id)
                            procesados_ok += 1

                            st.success(texto_confirmacion)

                    except Exception as e:
                        st.error(f"Error procesando un mensaje: {e}")

                # Confirmar lectura a Telegram para vaciar la cola de pendientes
                if ultimo_id > 0:
                    confirmar_offset_telegram(ultimo_id)

                st.toast(f"Proceso finalizado: {procesados_ok} procesados, {errores_ia} avisos enviados.")

# ------------------------------------------
# PESTAÑA 2: INVENTARIO Y CONSUMO
# ------------------------------------------
with tab_inventario:
    st.subheader("Control de Stock del Congelador")

    # Filtro superior por tipo de alimento
    filtro_tipo = st.radio(
        "Filtrar por familia de alimento:",
        options=["Todos", "Carne", "Pescado", "Verdura", "Otros"],
        horizontal=True
    )

    col_platos, col_ingredientes = st.columns(2)

    # --- TABLA 1: PLATOS PREPARADOS ---
    with col_platos:
        st.markdown("### 🍲 Platos Preparados")
        st.caption("Comidas cocinadas listas para descongelar y comer.")

        query_platos = supabase.table("platos_preparados").select("*").order("nombre")
        if filtro_tipo != "Todos":
            query_platos = query_platos.eq("tipo_alimento", filtro_tipo)
        datos_platos = query_platos.execute().data

        if not datos_platos:
            st.info("No hay platos preparados en esta categoría.")
        else:
            for item in datos_platos:
                with st.container(border=True):
                    c1, c2, c3 = st.columns([3, 1.3, 1.2])
                    c1.markdown(f"**{item['nombre']}**")
                    c1.caption(f"🏷️ {item['tipo_alimento']} | 📦 Envase: **{item['envase']}**")
                    c2.metric("Cantidad", f"{item['cantidad']} uds")

                    with c3:
                        if st.button("✅ Consumir 1", key=f"cons_p_{item['id']}", use_container_width=True):
                            consumir_unidad("platos_preparados", item["id"], item["cantidad"])
                            st.rerun()
                        if st.button("🗑️ Borrar", key=f"del_p_{item['id']}", use_container_width=True):
                            eliminar_registro("platos_preparados", item["id"])
                            st.rerun()

    # --- TABLA 2: INGREDIENTES ---
    with col_ingredientes:
        st.markdown("### 🥕 Ingredientes")
        st.caption("Materia prima guardada por tipo de tupper o bolsa.")

        query_ing = supabase.table("ingredientes").select("*").order("nombre")
        if filtro_tipo != "Todos":
            query_ing = query_ing.eq("tipo_alimento", filtro_tipo)
        datos_ing = query_ing.execute().data

        if not datos_ing:
            st.info("No hay ingredientes registrados en esta categoría.")
        else:
            for item in datos_ing:
                with st.container(border=True):
                    c1, c2, c3 = st.columns([3, 1.3, 1.2])
                    c1.markdown(f"**{item['nombre']}**")
                    c1.caption(f"🏷️ {item['tipo_alimento']} | 📦 Envase: **{item['envase']}**")
                    c2.metric("Cantidad", f"{item['cantidad']} uds")

                    with c3:
                        if st.button("✅ Consumir 1", key=f"cons_i_{item['id']}", use_container_width=True):
                            consumir_unidad("ingredientes", item["id"], item["cantidad"])
                            st.rerun()
                        if st.button("🗑️ Borrar", key=f"del_i_{item['id']}", use_container_width=True):
                            eliminar_registro("ingredientes", item["id"])
                            st.rerun()
