"""Maquina de estados del bot Kan M — V2.

Regla de oro: "el estado manda, no el texto".
Capas de respuesta (en orden):
  CAPA 1 — Handler del estado actual (bot convencional + keywords).
  CAPA 2 — ai_client.responder (Gemini/knowledge_base.txt), solo si CAPA 1
            devuelve False.
  CAPA 3 — Fallback fijo + notificacion al owner.

Estados:
    nuevo               Primer mensaje del cliente.
    menu_principal      Menu unificado mostrado, espera seleccion.
    post_info           Post-accion informativa (datos + cierre).
    inactivo_confirmando Mostro botones de reanudar tras inactividad.
    escalado_humano     Bot en silencio; solo responde a comandos explicitos.
"""
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import ai_client
import db
import meta_client as meta
from config import (
    HORARIO_TEXTO,
    INACTIVITY_THRESHOLD_MIN,
    MAX_FALLBACKS_ANTES_DE_OFRECER_HUMANO,
    NEGOCIO_DIRECCION,
    negocio_esta_abierto,
)
from intent import (
    detectar_intenciones_multiples,
    es_saludo_o_menu,
    normalizar,
)

log = logging.getLogger("kanm.fsm")


# ============================================================
# Constantes de UI
# ============================================================
BIENVENIDA_BASE = "¡Hola! 👋 Bienvenido/a a Kan M Repostería y Catering"

MENU_PRINCIPAL_ROWS = [
    {"id": "cotizar",  "title": "📅 Cotizar / Encargar",  "description": "Bizcocho, evento o encargo"},
    {"id": "delivery", "title": "🚗 Delivery",            "description": "Uber Eats"},
    {"id": "horario",  "title": "🕒 Horario y ubicación", "description": "Cuándo y dónde"},
    {"id": "pedido",   "title": "🛒 Quiero ordenar",      "description": "Te paso con alguien"},
]

CIERRE_BUTTONS = [
    {"id": "cierre_menu",   "title": "📋 Ver menú"},
    {"id": "cierre_humano", "title": "Hablar con alguien"},
    {"id": "cierre_no",     "title": "❌ Nada más"},
]

REANUDAR_BUTTONS = [
    {"id": "reanudar_si", "title": "✅ Sí, continuar"},
    {"id": "reanudar_no", "title": "🔄 Empezar de nuevo"},
]

# Comandos explícitos para salir del estado escalado.
ESCAPE_ESCALADO = {"menu", "inicio", "volver", "empezar"}

# Frases para cancelar el flujo de cotizacion y volver al menu.
_CANCELAR_COTIZACION = {"cancelar", "olvidalo", "olvídalo", "no quiero", "dejalo", "déjalo"}

ESCALADO_SILENCIO_MIN = 4320  # 3 días en silencio; luego auto-liberacion. Alto a propósito: en coexistencia la dueña atiende desde la app y el bot no ve esos mensajes.

# Etiquetas para ofrecer la 2da intencion detectada en un mismo mensaje.
SEGUIMIENTO_LABELS = {
    "cotizar":  "cotizar un evento o encargo",
    "delivery": "el delivery",
    "horario":  "el horario y ubicación",
    "ubicacion": "la ubicación",
    "pedido":   "tu pedido",
    "humano":   "hablar con una persona",
}

_MULETILLAS_AFIRMATIVAS = [
    "¡De una!",
    "¡Claro!",
    "¡Por supuesto!",
    "¡Cómo no!",
    "¡Dale!",
]

_RESPUESTAS_AFIRMATIVAS = {
    "si", "sí", "yes", "dale", "ok", "oka", "claro",
    "continuar", "seguir", "reanudar",
}


# ============================================================
# Helpers basicos
# ============================================================
def _bienvenida_personalizada(convo: Optional[dict]) -> str:
    if (convo or {}).get("last_outbound_at") is not None:
        return "¡Hola de nuevo! 😊"
    return "¡Hola! 👋 Bienvenido/a a Kan M Repostería y Catering"


def _esta_inactivo(prev_convo: dict) -> bool:
    last_inbound = prev_convo.get("last_inbound_at")
    if not isinstance(last_inbound, datetime):
        return False
    if last_inbound.tzinfo is None:
        last_inbound = last_inbound.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_inbound) > timedelta(minutes=INACTIVITY_THRESHOLD_MIN)


def _muletilla_afirmativa(convo: Optional[dict] = None) -> str:
    return random.choice(_MULETILLAS_AFIRMATIVAS) + " 😊"


def _body_menu_principal(convo: Optional[dict] = None) -> str:
    return "¿En qué te puedo ayudar? 😊"


async def _pausa_natural() -> None:
    """Pausa breve entre mensajes consecutivos del mismo turno para que no lleguen en rafaga."""
    await asyncio.sleep(0.8)


# ============================================================
# Escalado a humano
# ============================================================
async def _notificar_owner_en_fondo(
    motivo: str, phone: str, nombre: Optional[str], resumen: str | None = None
) -> None:
    """Llama a notificar_owner() sin bloquear el flujo del cliente."""
    try:
        await meta.notificar_owner(motivo, phone, nombre, resumen)
    except Exception:
        log.exception("Fallo notificar_owner en background motivo=%s phone=%s", motivo, phone)


async def stub_escalar_a_humano(phone: str, motivo: str, nombre: str | None = None) -> None:
    log.info("ESCALAR humano phone=%s motivo=%s", phone, motivo)
    await db.mark_escalated(phone, motivo)
    asyncio.create_task(_notificar_owner_en_fondo(motivo, phone, nombre))


async def stub_escalar_a_humano_con_resumen(
    phone: str, motivo: str, resumen: str | None, nombre: str | None = None
) -> None:
    log.info("ESCALAR humano (con resumen) phone=%s motivo=%s resumen=%s", phone, motivo, resumen)
    await db.mark_escalated(phone, motivo)
    asyncio.create_task(_notificar_owner_en_fondo(motivo, phone, nombre, resumen))


# ============================================================
# Senders / UI helpers
# ============================================================
async def _enviar_menu_principal(phone: str, body_text: str) -> None:
    await meta.enviar_lista(
        to=phone,
        body=body_text,
        button="Ver opciones",
        rows=MENU_PRINCIPAL_ROWS,
        section_title="¿En qué te ayudamos?",
    )


async def _enviar_cierre(phone: str) -> None:
    convo_actual = await db.get_conversation(phone)
    contexto = (convo_actual or {}).get("context") or {}
    contador = int(contexto.get("acciones_seguidas") or 0) + 1
    await db.update_context(phone, {"acciones_seguidas": contador})
    if contador % 2 == 1:
        await meta.enviar_botones(phone, "¿Te ayudo con algo más?", CIERRE_BUTTONS)


async def _ofrecer_seguimiento(phone: str, intencion_seg: str) -> None:
    # Solo ofrecemos seguimiento de intenciones despachables por botones
    # (las no despachables, ej. "faq"/"precio", no tienen accion directa).
    if intencion_seg not in _INTENCIONES_MENU:
        return
    label = SEGUIMIENTO_LABELS.get(intencion_seg)
    if not label:
        return
    actual = await db.get_conversation(phone)
    if actual and actual.get("state") == "escalado_humano":
        return
    await meta.enviar_botones(
        phone,
        f"Por cierto, también me preguntaste por {label} 👀",
        [
            {"id": intencion_seg, "title": "Sí, cuéntame"},
            {"id": "cierre_no", "title": "No, gracias"},
        ],
    )


# ============================================================
# Manejo de estado escalado_humano
# ============================================================
async def _manejar_escalado(
    phone: str,
    tipo: str,
    contenido: dict,
    convo: dict,
) -> bool:
    """True si el mensaje quedo manejado (cortar procesamiento).
    False si el bot fue auto-liberado y debe seguir su flujo normal."""
    escalated_at = convo.get("escalated_at")
    if isinstance(escalated_at, datetime):
        ts = escalated_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        mins_escalado = (datetime.now(timezone.utc) - ts).total_seconds() / 60

        if mins_escalado > ESCALADO_SILENCIO_MIN:
            await db.update_state(phone, "menu_principal")
            await db.reset_fallback(phone)
            await meta.enviar_texto(
                phone,
                "¡Hola de nuevo! 😊 Ha pasado un tiempo desde que "
                "te pasé con el equipo. ¿Continuamos con el bot o "
                "necesitas algo más?",
            )
            await _pausa_natural()
            await _enviar_menu_principal(phone, _body_menu_principal(convo))
            await db.touch_outbound(phone)
            log.info(
                "phone=%s auto-liberado tras %d min escalado (umbral %d min)",
                phone, int(mins_escalado), ESCALADO_SILENCIO_MIN,
            )
            return False

    # Salida explícita: "menu" / "inicio" / "volver" / "empezar"
    if tipo == "text":
        texto = (contenido.get("body") or "").strip()
        t_norm = normalizar(texto).replace(" ?", "").strip()
        if t_norm in ESCAPE_ESCALADO:
            await db.update_state(phone, "menu_principal")
            await db.reset_fallback(phone)
            await _enviar_menu_principal(phone, _body_menu_principal(convo))
            await db.touch_outbound(phone)
            log.info("phone=%s salio de escalado via comando '%s'", phone, t_norm)
            return True

    # Silencio total — no se envía nada
    log.info("phone=%s escalado, sin auto-respuesta", phone)
    return True


# ============================================================
# Entry point
# ============================================================
async def procesar_mensaje(
    phone: str,
    profile_name: Optional[str],
    tipo: str,
    contenido: dict,
) -> None:
    """tipo: 'text' | 'interactive' | 'other'."""
    prev = await db.get_conversation(phone)
    convo = await db.ensure_conversation(phone)

    if profile_name and convo.get("name") != profile_name:
        try:
            await db.set_name(phone, profile_name)
            convo["name"] = profile_name
        except Exception as e:
            log.warning("No se pudo guardar el nombre phone=%s: %s", phone, e)

    if convo.get("state") == "escalado_humano":
        if await _manejar_escalado(phone, tipo, contenido, convo):
            return

    if tipo == "other":
        await meta.enviar_texto(
            phone,
            "Por aquí solo te puedo leer *texto* y *botones* 🙏\n"
            "Escríbeme *menú* y te muestro las opciones 😊",
        )
        await db.touch_outbound(phone)
        return

    if (prev is not None
            and convo.get("state") != "inactivo_confirmando"
            and _esta_inactivo(prev)):
        await db.update_context(phone, {
            "estado_pre_inactivo": prev.get("state") or "menu_principal",
        })
        await db.update_state(phone, "inactivo_confirmando")
        await meta.enviar_botones(
            phone,
            "¡Hey! Veo que habíamos quedado a medias 🕒 "
            "¿Seguimos donde lo dejamos?",
            REANUDAR_BUTTONS,
        )
        await db.touch_outbound(phone)
        return

    if tipo == "interactive":
        seleccion_id = (contenido.get("id") or "").strip()
        await _handle_seleccion(phone, seleccion_id, convo)
        return

    # tipo == 'text'
    texto = (contenido.get("body") or "").strip()
    if not texto:
        await _bienvenida_y_menu(phone, convo)
        return

    await db.append_historial(phone, "cliente", texto)

    # CAPA 1 — Bot convencional (handler del estado actual)
    estado = convo.get("state") or "nuevo"
    handler = _HANDLERS.get(estado, handler_estado_desconocido)
    hubo_excepcion = False
    try:
        handled = await handler(phone, texto, convo, profile_name)
    except Exception:
        log.exception("Error en handler phone=%s estado=%s", phone, estado)
        handled = False
        hubo_excepcion = True

    if handled:
        return

    # CAPA 2 — IA (Gemini via ai_client)
    try:
        historial = await db.get_historial(phone)
        respuesta_ia = await ai_client.responder(texto, historial=historial)
    except Exception:
        log.exception("Error en ai_client phone=%s", phone)
        respuesta_ia = None
        hubo_excepcion = True

    if respuesta_ia == "FUERA_DE_CONTEXTO":
        # Pregunta fuera del negocio: no es un "no entendí" — redirigir al
        # menú sin penalizar fallback ni ensuciar el historial.
        await _enviar_menu_principal(
            phone,
            "Eso no lo manejo por aquí 🙈 pero mira lo que sí puedo hacer por ti 👇",
        )
        await db.update_state(phone, "menu_principal")
        await db.touch_outbound(phone)
        return

    if respuesta_ia is not None:
        await meta.enviar_texto(phone, respuesta_ia)
        await db.append_historial(phone, "bot", respuesta_ia)
        await db.touch_outbound(phone)
        return

    # Excepcion real en alguna de las capas anteriores → avisar a la dueña una vez.
    if hubo_excepcion:
        asyncio.create_task(_notificar_owner_en_fondo("error_tecnico", phone, None))

    # CAPA 3 — Ni bot ni IA reconocieron el mensaje; incrementar fallback.
    log.info("CAPA3 phone=%s estado=%s hubo_excepcion=%s", phone, estado, hubo_excepcion)
    fallbacks = await db.increment_fallback(phone)
    if fallbacks <= MAX_FALLBACKS_ANTES_DE_OFRECER_HUMANO:
        await meta.enviar_texto(
            phone,
            "Mmm, no estoy seguro de haberte entendido 😅 "
            "¿Te muestro las opciones?",
        )
    else:
        await _enviar_menu_principal(
            phone,
            "Disculpa, sigo sin captar bien 🙈 "
            "Mejor te dejo el menú para que elijas fácil 👇",
        )
        await db.update_state(phone, "menu_principal")
        await db.reset_fallback(phone)
    await db.touch_outbound(phone)


async def _bienvenida_y_menu(phone: str, convo: dict) -> None:
    body = (
        "¡Hola de nuevo! 😊 ¿En qué te puedo ayudar hoy?"
        if convo.get("last_outbound_at") is not None
        else f"{BIENVENIDA_BASE} ¿En qué te puedo ayudar?"
    )
    await _enviar_menu_principal(phone, body)
    await db.update_state(phone, "menu_principal")
    await db.touch_outbound(phone)


# ============================================================
# Handlers por estado — devuelven True si resolvieron el mensaje,
# False si debe pasar a ai_client (CAPA 2).
# ============================================================
async def handler_nuevo(
    phone: str, texto: str, convo: dict, profile_name: Optional[str]
) -> bool:
    es_primera_interaccion = convo.get("last_outbound_at") is None

    intenciones = detectar_intenciones_multiples(texto)
    if intenciones:
        intencion = intenciones[0]
        seguimiento = intenciones[1] if len(intenciones) > 1 else None
        if es_primera_interaccion:
            await meta.enviar_texto(
                phone,
                f"{_bienvenida_personalizada(convo)} Enseguida te ayudo.",
            )
            await _pausa_natural()
        handled = await _atender_intencion(
            phone, intencion, texto, convo, anunciar=not es_primera_interaccion
        )
        if seguimiento:
            await _ofrecer_seguimiento(phone, seguimiento)
        await db.reset_fallback(phone)
        return handled

    # Sin intencion detectada.
    if es_saludo_o_menu(texto):
        if es_primera_interaccion:
            await _bienvenida_y_menu(phone, convo)
            return True
        # Ya hubo contacto previo: ceder a la IA (CAPA 2).
        return False

    return False



async def handler_menu_principal(
    phone: str, texto: str, convo: dict, profile_name: Optional[str]
) -> bool:
    return await _generico_menu_handler(
        phone, texto, convo,
        body_default=_body_menu_principal(convo),
    )


async def handler_post_info(
    phone: str, texto: str, convo: dict, profile_name: Optional[str]
) -> bool:
    return await _generico_menu_handler(
        phone, texto, convo,
        body_default=_body_menu_principal(convo),
    )


async def handler_cotizando(
    phone: str, texto: str, convo: dict, profile_name: Optional[str]
) -> bool:
    """El cliente respondio mientras el bot recopilaba datos de cotizacion.
    El mensaje ya esta en historial (guardado por procesar_mensaje antes de llegar aqui).
    """
    t_norm = normalizar(texto).replace(" ?", "").strip()
    if t_norm in ESCAPE_ESCALADO or t_norm in _CANCELAR_COTIZACION:
        await db.update_state(phone, "menu_principal")
        await meta.enviar_texto(phone, "Sin problema 😊 Aquí va el menú:")
        await _pausa_natural()
        await _enviar_menu_principal(phone, _body_menu_principal(convo))
        await db.touch_outbound(phone)
        return True

    await _procesar_turno_cotizacion(phone, convo)
    return True


async def handler_inactivo_confirmando(
    phone: str, texto: str, convo: dict, profile_name: Optional[str]
) -> bool:
    if normalizar(texto) in _RESPUESTAS_AFIRMATIVAS:
        await _reanudar(phone, convo)
    else:
        await _empezar_de_nuevo(phone, convo)
    return True


async def handler_escalado_humano(
    phone: str, texto: str, convo: dict, profile_name: Optional[str]
) -> bool:
    # procesar_mensaje() ya corta antes; safety net.
    return True


async def handler_estado_desconocido(
    phone: str, texto: str, convo: dict, profile_name: Optional[str]
) -> bool:
    log.warning("Estado desconocido phone=%s estado=%s", phone, convo.get("state"))
    await db.update_state(phone, "menu_principal")
    await _enviar_menu_principal(
        phone,
        "Arrancamos de nuevo desde el menú 😊",
    )
    await db.touch_outbound(phone)
    return True


_HANDLERS = {
    "nuevo":               handler_nuevo,
    "menu_principal":      handler_menu_principal,
    "post_info":           handler_post_info,
    "cotizando":           handler_cotizando,
    "inactivo_confirmando": handler_inactivo_confirmando,
    "escalado_humano":     handler_escalado_humano,
}


# ============================================================
# Logica generica de menu (texto libre + intencion + fallback)
# ============================================================
async def _generico_menu_handler(
    phone: str,
    texto: str,
    convo: dict,
    body_default: str,
) -> bool:
    """True si resolvio el mensaje, False si debe pasar a ai_client."""
    # 1) Intencion detectada (puede haber 2 en un solo mensaje).
    intenciones = detectar_intenciones_multiples(texto)
    if intenciones:
        intencion = intenciones[0]
        seguimiento = intenciones[1] if len(intenciones) > 1 else None
        handled = await _atender_intencion(phone, intencion, texto, convo, anunciar=True)
        if seguimiento:
            await _ofrecer_seguimiento(phone, seguimiento)
        await db.reset_fallback(phone)
        return handled

    # 2) Saludo / pedido de menu → mostrar menu sin penalizar fallback.
    if es_saludo_o_menu(texto):
        await _enviar_menu_principal(phone, body_default)
        await db.reset_fallback(phone)
        await db.touch_outbound(phone)
        return True

    # 3) Sin intención reconocida → ceder a ai_client (CAPA 2).
    return False


# ============================================================
# Despacho de intencion
# ============================================================
async def _atender_intencion(
    phone: str,
    intencion: str,
    texto_original: str,
    convo: dict,
    anunciar: bool = True,
) -> bool:
    """Despacha la accion. Devuelve False para intenciones que maneja Gemini (CAPA 2)."""
    # Estas intenciones las maneja Gemini desde el knowledge_base; si falla, CAPA 3.
    if intencion in ("faq", "precio"):
        return False

    if anunciar:
        await meta.enviar_texto(phone, _muletilla_afirmativa(convo))
        await _pausa_natural()

    if intencion == "cotizar":
        await _accion_cotizar(phone, convo)
    elif intencion == "delivery":
        await _accion_delivery(phone, convo)
    elif intencion in ("horario", "ubicacion"):
        await _accion_horario_ubicacion(phone, convo)
    elif intencion == "pedido":
        await _accion_pedido(phone, convo)
    elif intencion == "humano":
        await _accion_humano(phone, motivo="usuario_pidio_humano")
        return True  # no touch_outbound adicional tras escalar
    else:
        await _enviar_menu_principal(phone, _body_menu_principal(convo))
        await db.update_state(phone, "menu_principal")

    await db.touch_outbound(phone)
    return True


# ============================================================
# Acciones
# ============================================================
async def _procesar_turno_cotizacion(
    phone: str, convo: dict, motivo_override: str | None = None
) -> None:
    """Ejecuta un turno del flujo de recopilacion de datos para cotizacion/pedido.

    El mensaje del cliente ya fue guardado en historial por procesar_mensaje()
    antes de llegar aqui — no se duplica el registro.
    motivo_override: usar en la primera llamada (convo todavia no tiene el contexto actualizado).
    """
    contexto = (convo or {}).get("context") or {}
    turno_actual = int(contexto.get("cotizacion_turnos") or 0)
    motivo = motivo_override or contexto.get("cotizacion_motivo") or "cotizar"

    historial = await db.get_historial(phone)
    listo, texto = await ai_client.gestionar_cotizacion(
        historial, ai_client._FORMULARIO_COTIZACION, turno_actual
    )

    if listo:
        await meta.enviar_texto(
            phone,
            "¡Perfecto! 😊 Le paso tu solicitud directamente a nuestra repostera, "
            "ella te contacta para coordinar todos los detalles. 🙌",
        )
        await db.append_historial(phone, "bot", "Le paso tu solicitud a la repostera.")
        resumen = texto.strip() if texto else None
        await stub_escalar_a_humano_con_resumen(
            phone, motivo, resumen, nombre=(convo or {}).get("name")
        )
        return

    # Sigue preguntando.
    await meta.enviar_texto(phone, texto)
    await db.append_historial(phone, "bot", texto)
    await db.update_context(phone, {"cotizacion_turnos": turno_actual + 1})
    await db.touch_outbound(phone)


async def _accion_cotizar(phone: str, convo: dict) -> None:
    await db.update_state(phone, "cotizando")
    await db.update_context(phone, {"cotizacion_turnos": 0, "cotizacion_motivo": "cotizar"})
    await _procesar_turno_cotizacion(phone, convo, motivo_override="cotizar")


async def _accion_delivery(phone: str, convo: dict) -> None:
    datos = "Kan M esta disponible en Uber Eats. No estan en PedidosYa ni otras plataformas de delivery."
    historial = await db.get_historial(phone)
    texto = await ai_client.generar_respuesta_tema("Delivery", datos, historial=historial)
    if not texto:
        texto = (
            "¡Claro que llevamos! 🚗 Estamos disponibles en Uber Eats. "
            "No estamos en PedidosYa ni otras plataformas por ahora."
        )
    await meta.enviar_texto(phone, texto)
    await db.append_historial(phone, "bot", texto)
    await db.update_state(phone, "post_info")
    await _pausa_natural()
    await _enviar_cierre(phone)


async def _accion_horario_ubicacion(phone: str, convo: dict) -> None:
    abierto = negocio_esta_abierto()
    datos = (
        f"Direccion: {NEGOCIO_DIRECCION}. "
        f"Horario: {HORARIO_TEXTO}. "
        f"Estado actual: {'abiertos ahora mismo' if abierto else 'cerrados ahora mismo'}."
    )
    historial = await db.get_historial(phone)
    texto = await ai_client.generar_respuesta_tema(
        "Horario y ubicacion", datos, historial=historial
    )
    if not texto:
        estado_txt = (
            "\n\n¡Estamos abiertos ahora mismo! ✅"
            if abierto
            else "\n\nAhora estamos cerraditos ⏳"
        )
        texto = (
            f"📍 Nos encuentras en: {NEGOCIO_DIRECCION}\n\n"
            f"🕒 Nuestro horario:\n{HORARIO_TEXTO}"
            f"{estado_txt}"
        )
    await meta.enviar_texto(phone, texto)
    await db.append_historial(phone, "bot", texto)
    await db.update_state(phone, "post_info")
    await _pausa_natural()
    await _enviar_cierre(phone)


async def _accion_pedido(phone: str, convo: dict) -> None:
    if negocio_esta_abierto():
        historial = await db.get_historial(phone)
        apertura = await ai_client.generar_transicion_escalado(
            destino="nuestra repostera",
            motivo="quiere hacer un pedido o encargo",
            historial=historial,
        )
        if not apertura:
            apertura = "¡Con gusto! 😊"
        texto = (
            f"{apertura} Para pedidos te paso directamente con nuestra "
            f"repostera, ella te ayuda con todos los detalles. "
            f"Dame un momento... 🙌"
        )
        await meta.enviar_texto(phone, texto)
        await stub_escalar_a_humano(phone, "pedido", nombre=(convo or {}).get("name"))
    else:
        await meta.enviar_texto(
            phone,
            f"Ahora mismo estamos cerrados 😴 Nuestro horario es:\n"
            f"{HORARIO_TEXTO}\n\n"
            "Escríbenos cuando abramos y con gusto te ayudamos a ordenar 😊",
        )
        await db.update_state(phone, "post_info")
        await _pausa_natural()
        await _enviar_cierre(phone)


async def _accion_humano(phone: str, motivo: str = "manual") -> None:
    nombre = (await db.get_conversation(phone) or {}).get("name")
    await stub_escalar_a_humano(phone, motivo, nombre=nombre)
    historial = await db.get_historial(phone)
    abierto = negocio_esta_abierto()
    apertura = await ai_client.generar_transicion_escalado(
        destino="alguien del equipo",
        motivo="quiere hablar con una persona",
        historial=historial,
    )
    if abierto:
        if not apertura:
            apertura = "Dame un momento que te paso con alguien del equipo 🙌"
        msg = f"{apertura} Estamos atendiendo ahora, te responden en breve por aquí."
    else:
        if not apertura:
            apertura = "Le pasé tu mensaje al equipo 🙌"
        msg = (
            f"{apertura} Ahora mismo estamos cerrados, así que te responden "
            f"apenas abramos. Nuestro horario: {HORARIO_TEXTO}."
        )
    await meta.enviar_texto(phone, msg)


# ============================================================
# Reanudar / empezar de nuevo (tras inactividad)
# ============================================================
async def _reanudar(phone: str, convo: dict) -> None:
    contexto = (convo or {}).get("context") or {}
    estado_previo = contexto.get("estado_pre_inactivo") or ""

    if estado_previo == "cotizando":
        await db.update_context(phone, {"acciones_seguidas": 0, "estado_pre_inactivo": ""})
        await db.update_state(phone, "cotizando")
        await meta.enviar_texto(phone, "¡Perfecto! Seguimos con tu cotización 😊")
        await db.append_historial(phone, "cliente", "(quiero continuar con la cotización)")  # ← AQUÍ
        await _procesar_turno_cotizacion(phone, convo)
        return

    await db.update_context(phone, {"acciones_seguidas": 0, "estado_pre_inactivo": ""})
    await _enviar_menu_principal(
        phone,
        "¡Hola de nuevo! 😊 ¿En qué te puedo ayudar?",
    )
    await db.update_state(phone, "menu_principal")
    await db.touch_outbound(phone)

async def _empezar_de_nuevo(phone: str, convo: dict) -> None:
    await db.update_context(phone, {"acciones_seguidas": 0})
    await meta.enviar_texto(phone, "¡De cero entonces! 😊 Aquí va el menú:")
    await _pausa_natural()
    await _enviar_menu_principal(
        phone,
        "¿En qué te puedo ayudar?",
    )
    await db.update_state(phone, "menu_principal")
    await db.reset_fallback(phone)
    await db.touch_outbound(phone)


# ============================================================
# Selecciones interactivas (list_reply / button_reply)
# ============================================================
_INTENCIONES_MENU = {
    "cotizar", "delivery", "horario", "ubicacion", "pedido", "humano",
}


async def _handle_seleccion(phone: str, seleccion_id: str, convo: dict) -> None:
    # Opciones del menu principal
    if seleccion_id in _INTENCIONES_MENU:
        await _atender_intencion(phone, seleccion_id, seleccion_id, convo, anunciar=True)
        await db.reset_fallback(phone)
        return

    # Botones de cierre
    if seleccion_id == "cierre_menu":
        await db.update_context(phone, {"acciones_seguidas": 0})
        await _enviar_menu_principal(phone, _body_menu_principal(convo))
        await db.update_state(phone, "menu_principal")
        await db.touch_outbound(phone)
        return

    if seleccion_id == "cierre_humano":
        await _accion_humano(phone, motivo="cierre_humano")
        return

    if seleccion_id == "cierre_no":
        await meta.enviar_texto(
            phone,
            "¡Gracias por escribir! 👋 Que tengas un lindo día, "
            "aquí estamos para lo que necesites 😊",
        )
        await db.update_state(phone, "menu_principal")
        await db.touch_outbound(phone)
        return

    # Botones de reanudar (inactividad)
    if seleccion_id == "reanudar_si":
        await _reanudar(phone, convo)
        return

    if seleccion_id == "reanudar_no":
        await _empezar_de_nuevo(phone, convo)
        return

    # ID desconocido → menu principal
    log.warning("seleccion_id desconocido phone=%s id=%s", phone, seleccion_id)
    await _enviar_menu_principal(phone, _body_menu_principal(convo))
    await db.update_state(phone, "menu_principal")
    await db.touch_outbound(phone)
