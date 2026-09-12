"""
Cliente de Gemini (Google) para el bot de WhatsApp de Kan M.
Usa el modelo gemini-2.5-flash via la Gemini Developer API (API key
simple, sin Vertex AI/billing de Google Cloud).

Este modulo tiene memoria de la conversacion actual: recibe el
historial reciente (ultimos N turnos) y lo incluye en cada llamada,
para que el bot pueda dar seguimiento natural a preguntas relacionadas
(ej. "de que tienen" despues de "hay empanadas").

Este modulo SOLO se invoca cuando state_machine.py no reconoce ninguna
intencion por palabras clave (ver intent.py). No es la primera capa.
"""

import asyncio
import logging
import pathlib

from google import genai
from google.genai import types

from config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

_TIMEOUT_TEMA_SEC = 3.5
_MODEL_ID = "gemini-2.5-flash"

# Carga el knowledge base una sola vez al arrancar
_KB_PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "knowledge_base.txt"


def _cargar_knowledge_base() -> str:
    try:
        return _KB_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("knowledge_base.txt no encontrado — el bot tendrá contexto vacío")
        return ""


_KNOWLEDGE_BASE = _cargar_knowledge_base()

_MAX_TURNOS_COTIZACION = 4


def _extraer_formulario_cotizacion(kb_completo: str) -> str:
    """Extrae la seccion '## Formulario de cotizacion' del knowledge base.
    Si no existe, devuelve string vacio — gestionar_cotizacion usa un fallback generico."""
    marcador = "## Formulario de cotización"
    idx = kb_completo.find(marcador)
    if idx < 0:
        return ""
    return kb_completo[idx:].strip()


_FORMULARIO_COTIZACION = _extraer_formulario_cotizacion(_KNOWLEDGE_BASE)

# Cliente singleton
_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def _historial_a_contents(historial: list[dict]) -> list[types.Content]:
    """Convierte el historial guardado en db.py (lista de {rol, texto})
    al formato de 'contents' que espera Gemini (role 'user'/'model')."""
    contents = []
    for turno in historial:
        rol = "user" if turno.get("rol") == "cliente" else "model"
        texto = turno.get("texto", "")
        if texto:
            contents.append(
                types.Content(role=rol, parts=[types.Part.from_text(text=texto)])
            )
    return contents


_SYSTEM_PROMPT = f"""Eres el asistente virtual de Kan M, una repostería artesanal dominicana,
conversando por WhatsApp con un cliente.

Tu único rol es responder preguntas de clientes sobre el negocio usando
la información que tienes abajo, dando seguimiento natural a la
conversación (recuerda lo que el cliente ya preguntó en mensajes
anteriores de este mismo chat, que se te muestran como historial).

REGLAS ESTRICTAS:
- Responde SOLO con información que esté en la sección INFORMACIÓN DEL NEGOCIO.
- Si la pregunta está fuera de ese contexto, responde exactamente: FUERA_DE_CONTEXTO
- NO confirmes pedidos, disponibilidad ni fechas.
- NO inventes precios, horarios ni información que no esté abajo.
- NO tomes pedidos. Si el cliente quiere ordenar, responde exactamente: FUERA_DE_CONTEXTO
- NO cotices bizcochos ni eventos. Si el cliente pregunta por una cotización, responde exactamente: FUERA_DE_CONTEXTO
- NO termines tu respuesta con una pregunta directa (evita signos de interrogación al final). Cierra con una afirmación o frase abierta, de forma cálida, sin necesidad de preguntar nada.
- Responde en español dominicano, de forma amable y breve (máximo 3-4 oraciones).
- Usa emojis con moderación, como lo haría una repostería dominicana.

INFORMACIÓN DEL NEGOCIO:
{_KNOWLEDGE_BASE}
"""

_PROMPT_TEMA = """Eres el asistente de Kan M, una repostería dominicana, conversando
por WhatsApp con un cliente. Responde sobre el tema "{tema}" usando EXCLUSIVAMENTE estos
datos verificados (no agregues, no inventes, no cambies ningún número, precio u horario):

{datos}

Si hay historial de conversación previo, tenlo en cuenta para que tu respuesta fluya
naturalmente y no repitas información que ya diste antes en este mismo chat.

REGLAS ESTRICTAS:
- Empieza tu respuesta con un saludo afirmativo breve y variado (ej. "¡Claro!",
  "¡De una!", "¡Por supuesto!", "¡Cómo no!" — usa uno distinto cada vez), como si
  estuvieras respondiendo con gusto. SOLO si esta es la primera vez que tocas este
  tema en la conversación — si el historial muestra que ya vienen hablando de esto,
  puedes omitir el saludo y responder de forma más directa y conversacional.
- NO copies estos datos palabra por palabra. Redacta con tus propias palabras.
- NO inventes precios, horarios, ni disponibilidad que no esté en los datos de arriba.
- Máximo 3-4 líneas de texto en total.
- NO termines tu respuesta con una pregunta directa (evita signos de interrogación
  al final). Cierra con una afirmación o invitación abierta. El cliente va a ver
  botones de seguimiento después de tu mensaje, así que NO necesitas preguntar nada.
- Tono dominicano, cálido, máximo 1-2 emojis.
- Responde SOLO con el mensaje final para el cliente, sin preámbulo ni comillas.
"""


async def responder(mensaje_usuario: str, historial: list[dict] | None = None) -> str | None:
    """
    Envía el historial completo de la conversacion (que ya incluye el
    mensaje actual del cliente como ultimo turno, guardado por
    state_machine.py antes de llamar aqui) a Gemini, y devuelve la
    respuesta. mensaje_usuario se conserva como parametro por
    compatibilidad/logging, pero NO se vuelve a agregar a contents
    si ya esta presente en historial — evita duplicar el mismo turno
    role=user dos veces consecutivas.

    Retorna None si Gemini falla o tarda demasiado. Retorna el string
    exacto "FUERA_DE_CONTEXTO" si la pregunta está fuera de contexto
    (incluye pedidos y cotizaciones — esos los maneja state_machine.py,
    no la IA).
    """
    try:
        client = _get_client()
        contents = _historial_a_contents(historial or [])
        if not contents:
            # Caso borde: historial vacio (state_machine.py siempre guarda el
            # mensaje antes de llamar aqui, pero por robustez usamos mensaje_usuario directo).
            contents = [types.Content(role="user", parts=[types.Part.from_text(text=mensaje_usuario)])]

        coro = client.aio.models.generate_content(
            model=_MODEL_ID,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                temperature=0.3,
                max_output_tokens=500,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        response = await asyncio.wait_for(coro, timeout=_TIMEOUT_TEMA_SEC + 2)

        try:
            finish_reason = response.candidates[0].finish_reason
            if finish_reason and "MAX_TOKENS" in str(finish_reason):
                logger.warning("responder truncada por MAX_TOKENS")
                return None
        except (IndexError, AttributeError):
            pass

        texto = (response.text or "").strip()

        if "FUERA_DE_CONTEXTO" in texto:
            logger.info("Gemini marcó pregunta como fuera de contexto")
            return "FUERA_DE_CONTEXTO"

        return texto or None

    except Exception as e:
        logger.error(f"Error al consultar Gemini (responder): {e}")
        return None


_PROMPT_TRANSICION = """Eres el asistente de Kan M, una repostería dominicana, conversando
por WhatsApp. El cliente va a ser transferido a {destino} porque {motivo}.

Si hay historial de conversación previo, genera una frase MUY breve (1 línea) que
reconozca de forma natural el tema que se venía hablando, como transición cálida
antes de pasar al siguiente paso. Si no hay historial relevante, no hace falta
mencionar ningún tema, solo sé breve y cálido.

REGLAS ESTRICTAS — MUY IMPORTANTE:
- NO menciones precios, cantidades, disponibilidad, ni fechas.
- NO confirmes ni sugieras que el pedido/cotización ya está resuelto o aceptado.
- Esto es una sola línea de transición, no una respuesta completa.
- NO termines con pregunta.
- Tono cálido, máximo 1 emoji.
- Responde SOLO con esa línea de transición, sin comillas ni preámbulo.
"""


async def generar_transicion_escalado(
    destino: str, motivo: str, historial: list[dict] | None = None
) -> str | None:
    """Genera una linea breve de transicion conversacional antes de escalar.
    destino: ej. 'nuestro equipo', 'nuestra repostera'
    motivo: ej. 'quiere hacer un pedido', 'quiere hablar con alguien'

    Devuelve None si Gemini falla o tarda — el caller SIEMPRE debe tener
    un texto fijo de respaldo listo.
    """
    try:
        client = _get_client()
        contents = _historial_a_contents(historial or [])
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="(transición de escalado)")],
            )
        )
        coro = client.aio.models.generate_content(
            model=_MODEL_ID,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_PROMPT_TRANSICION.format(destino=destino, motivo=motivo),
                temperature=0.5,
                max_output_tokens=100,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        response = await asyncio.wait_for(coro, timeout=_TIMEOUT_TEMA_SEC)

        try:
            finish_reason = response.candidates[0].finish_reason
            if finish_reason and "MAX_TOKENS" in str(finish_reason):
                logger.warning("generar_transicion_escalado truncada por MAX_TOKENS")
                return None
        except (IndexError, AttributeError):
            pass

        texto = (response.text or "").strip()
        # Salvaguarda: 1 linea corta esperada; si viene vacio o sospechosamente largo, descartar.
        if not texto or len(texto) > 200:
            return None
        return texto
    except Exception as e:
        logger.warning(f"generar_transicion_escalado fallo: {e}")
        return None


_PROMPT_COTIZACION = """Eres el asistente de Kan M, una repostería dominicana, conversando
por WhatsApp con un cliente que quiere cotizar un encargo personalizado o evento
(bizcocho personalizado, mesa de dulces, catering, picaderas para evento, etc.).

Tu tarea es conversar de forma natural para recopilar la información que la
repostera necesita para dar seguimiento, usando como guía esta lista de datos
(extraída del formulario de cotización del negocio):

{formulario}

INSTRUCCIONES:
- Revisa el historial de la conversación: si el cliente ya mencionó alguno de
  estos datos, NO lo vuelvas a preguntar.
- Si falta información del formulario, haz UNA pregunta a la vez, de forma
  natural y conversacional (no enumeres todos los datos pendientes de una vez).
- Si ya tienes información suficiente del formulario (no necesariamente el 100%,
  pero lo esencial: qué producto/servicio y al menos un dato más como cantidad
  o fecha), o si llevas {turno_actual} de {max_turnos} intentos de pregunta,
  responde EXACTAMENTE con: LISTO_PARA_DERIVAR
  seguido en la siguiente línea de un resumen breve de lo recopilado, en este
  formato: RESUMEN: <resumen breve en una línea>
- Si el cliente da señales de querer hablar directo con alguien (ej. "solo
  pásame con alguien", "olvídalo", "mejor hablo con una persona"), responde
  también con LISTO_PARA_DERIVAR y RESUMEN: cliente prefirió hablar directamente.

REGLAS ESTRICTAS (igual que en el resto del bot):
- NO confirmes precios, disponibilidad, fechas de entrega, ni aceptes el
  encargo. Tu única tarea es recopilar información y derivar.
- NO inventes información que no esté en el formulario ni en lo que el
  cliente ya dijo.
- Tono dominicano, cálido, máximo 1-2 emojis por mensaje.
- Si haces una pregunta (no derivas todavía), responde SOLO con esa pregunta,
  sin preámbulo ni comillas, sin terminar con "LISTO_PARA_DERIVAR".
"""


async def gestionar_cotizacion(
    historial: list[dict], formulario: str, turno_actual: int
) -> tuple[bool, str]:
    """Gestiona la conversacion de recopilacion de datos para cotizar/encargos.

    Devuelve (listo_para_derivar, texto):
      - Si listo_para_derivar es True: texto es el RESUMEN recopilado
        (puede ser string vacio si Gemini no devolvio resumen).
      - Si listo_para_derivar es False: texto es la siguiente pregunta
        a enviarle al cliente.

    Si Gemini falla o tarda, devuelve (True, '') — fallback seguro:
    mejor derivar sin resumen que dejar al cliente sin respuesta.
    """
    try:
        client = _get_client()
        contents = _historial_a_contents(historial or [])
        if not contents:
            contents = [types.Content(role="user", parts=[types.Part.from_text(text="(inicio cotizacion)")])]

        prompt = _PROMPT_COTIZACION.format(
            formulario=formulario or "(no definido — preguntar qué tipo de encargo o evento le interesa)",
            turno_actual=turno_actual,
            max_turnos=_MAX_TURNOS_COTIZACION,
        )

        coro = client.aio.models.generate_content(
            model=_MODEL_ID,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=prompt,
                temperature=0.4,
                max_output_tokens=200,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        response = await asyncio.wait_for(coro, timeout=_TIMEOUT_TEMA_SEC)

        try:
            finish_reason = response.candidates[0].finish_reason
            if finish_reason and "MAX_TOKENS" in str(finish_reason):
                logger.warning("gestionar_cotizacion truncada por MAX_TOKENS")
                return (True, "")
        except (IndexError, AttributeError):
            pass

        texto = (response.text or "").strip()

        if not texto:
            return (True, "")

        if "LISTO_PARA_DERIVAR" in texto:
            resumen = ""
            for linea in texto.splitlines():
                if linea.strip().upper().startswith("RESUMEN:"):
                    resumen = linea.split(":", 1)[1].strip()
                    break
            return (True, resumen)

        # Red de seguridad: si la IA no devolvio LISTO_PARA_DERIVAR al llegar al limite, forzar.
        if turno_actual >= _MAX_TURNOS_COTIZACION:
            return (True, "")

        return (False, texto)

    except Exception as e:
        logger.warning(f"gestionar_cotizacion fallo: {e}")
        if turno_actual == 0:
            # Primer turno: no hay nada recopilado; mejor una pregunta fija
            # que derivar a la repostera sin contexto alguno.
            return (
                False,
                "¡Con gusto te ayudo! 😊 Cuéntame qué tipo de encargo o evento "
                "tienes en mente, para cuándo y para cuántas personas.",
            )
        return (True, "")


async def generar_respuesta_tema(
    tema: str, datos: str, historial: list[dict] | None = None
) -> str | None:
    """Genera una respuesta natural y variada para un tema informativo
    (laticas, empanadas, delivery, horario), usando SOLO los datos pasados
    y el historial reciente de la conversacion para dar continuidad.

    Devuelve None si Gemini falla, tarda mas de _TIMEOUT_TEMA_SEC, o la
    respuesta viene vacia — el caller debe usar el texto fijo como
    fallback silencioso en ese caso.
    """
    try:
        client = _get_client()
        contents = _historial_a_contents(historial or [])
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=f"Cliente pregunta sobre: {tema}")],
            )
        )

        coro = client.aio.models.generate_content(
            model=_MODEL_ID,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_PROMPT_TEMA.format(tema=tema, datos=datos),
                temperature=0.6,
                max_output_tokens=300,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        response = await asyncio.wait_for(coro, timeout=_TIMEOUT_TEMA_SEC)

        try:
            finish_reason = response.candidates[0].finish_reason
            if finish_reason and "MAX_TOKENS" in str(finish_reason):
                logger.warning(f"generar_respuesta_tema truncada por MAX_TOKENS (tema={tema})")
                return None
        except (IndexError, AttributeError):
            pass

        texto = (response.text or "").strip()
        return texto or None
    except Exception as e:
        logger.warning(f"generar_respuesta_tema fallo (tema={tema}): {e}")
        return None
