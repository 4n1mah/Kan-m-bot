"""Cliente para la WhatsApp Cloud API de Meta.

Toda comunicacion saliente pasa por _send_to_meta(): POST con reintento
(2 intentos, backoff simple) y logging SIN PII (no logueamos el cuerpo
del mensaje al cliente).
"""
import asyncio
import logging
from typing import Any, Optional

import httpx

from config import (
    META_API_BASE,
    META_PHONE_NUMBER_ID,
    META_WHATSAPP_TOKEN,
)

log = logging.getLogger("kanm.meta")

_TIMEOUT = httpx.Timeout(6.0, connect=3.0)
_MAX_INTENTOS = 2


# ============================================================
# Envio base
# ============================================================
async def _send_to_meta(payload: dict) -> Optional[dict]:
    """POST al endpoint de mensajes. Reintenta hasta _MAX_INTENTOS con backoff."""
    if not (META_WHATSAPP_TOKEN and META_PHONE_NUMBER_ID):
        log.error("Faltan credenciales de Meta; no se envia el mensaje")
        return None

    url = f"{META_API_BASE}/{META_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    backoff = 0.3
    last_err: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for intento in range(1, _MAX_INTENTOS + 1):
            try:
                r = await client.post(url, json=payload, headers=headers)
                if r.status_code < 300:
                    log.info(
                        "meta_ok tipo=%s intento=%d status=%d",
                        payload.get("type"), intento, r.status_code,
                    )
                    return r.json()
                error_code = None
                try:
                    error_code = (r.json().get("error") or {}).get("code")
                except Exception:
                    pass
                if r.status_code == 401 or error_code == 190:
                    log.critical(
                        "META_TOKEN_INVALIDO_O_VENCIDO — el bot no puede "
                        "enviar mensajes. Renovar token en Meta Business."
                    )
                log.warning(
                    "meta_err intento=%d status=%d reason=%s body=%s",
                    intento, r.status_code,
                    getattr(r, "reason_phrase", ""),
                    r.text[:300],
                )
                # 4xx (excepto 429) no se reintenta
                if r.status_code < 500 and r.status_code != 429:
                    return None
            except Exception as e:
                last_err = e
                log.warning(
                    "meta_excepcion intento=%d tipo=%s err=%r",
                    intento, type(e).__name__, str(e) or repr(e),
                )
            # Solo esperamos si todavia quedan intentos por delante
            if intento < _MAX_INTENTOS:
                await asyncio.sleep(backoff)
                backoff *= 2
    log.error(
        "meta_falla_definitiva tipo=%s ult_err=%r",
        type(last_err).__name__ if last_err else "N/A",
        str(last_err) or repr(last_err) if last_err else "sin excepcion registrada",
    )
    return None


def _base_payload(to: str) -> dict:
    return {"messaging_product": "whatsapp", "to": to}


# ============================================================
# Texto
# ============================================================
async def enviar_texto(to: str, body: str) -> None:
    payload = {
        **_base_payload(to),
        "type": "text",
        "text": {"body": body[:4096]},
    }
    await _send_to_meta(payload)


# ============================================================
# Lista interactiva (menu principal)
# ============================================================
async def enviar_lista(
    to: str,
    body: str,
    button: str,
    rows: list[dict],
    header: Optional[str] = None,
    footer: Optional[str] = None,
    section_title: str = "Opciones",
) -> None:
    """rows: lista de {id, title, description?}.
    Limites WhatsApp: button <= 20, section_title <= 24, row.title <= 24."""
    interactive: dict[str, Any] = {
        "type": "list",
        "body": {"text": body[:1024]},
        "action": {
            "button": button[:20],
            "sections": [{"title": section_title[:24], "rows": rows}],
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header[:60]}
    if footer:
        interactive["footer"] = {"text": footer[:60]}
    payload = {**_base_payload(to), "type": "interactive", "interactive": interactive}
    await _send_to_meta(payload)


# ============================================================
# Botones (max 3)
# ============================================================
async def enviar_botones(to: str, body: str, buttons: list[dict]) -> None:
    """buttons: [{id, title}], maximo 3. title <= 20 chars."""
    payload = {
        **_base_payload(to),
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": str(b["id"])[:256],
                            "title": str(b["title"])[:20],
                        },
                    }
                    for b in buttons[:3]
                ]
            },
        },
    }
    await _send_to_meta(payload)


async def notificar_owner(
    motivo: str,
    client_phone: str,
    client_name: str | None,
    resumen: str | None = None,
) -> None:
    """Notifica a la dueña via WhatsApp cuando hay un cliente que necesita atencion humana."""
    from config import OWNER_PHONE
    if not OWNER_PHONE:
        log.warning("OWNER_PHONE no configurado, no se puede notificar al owner")
        return
    nombre = client_name or "Cliente sin nombre"
    motivos_legibles = {
        "usuario_pidio_humano": "quiere hablar con alguien",
        "pedido": "quiere hacer un pedido o encargo",
        "cotizar": "quiere cotizar un encargo o evento",
        "cierre_humano": "pidió hablar con una persona",
        "error_tecnico": "tuvo un error técnico que revisar",
    }
    motivo_txt = motivos_legibles.get(motivo, motivo)
    resumen_txt = f"\n📝 Detalles: {resumen}" if resumen else ""
    body = (
        f"🔔 *Cliente nuevo necesita atención*\n\n"
        f"👤 Nombre: {nombre}\n"
        f"📱 WhatsApp: +{client_phone}\n"
        f"💬 Motivo: {motivo_txt}"
        f"{resumen_txt}\n\n"
        f"Escríbele directamente para atenderlo 👆"
    )
    await enviar_texto(OWNER_PHONE, body)
