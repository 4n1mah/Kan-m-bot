"""FastAPI app + rutas del webhook de Meta.

GET  /webhook  -> verificacion (hub.mode + hub.verify_token + hub.challenge)
POST /webhook  -> recepcion de mensajes. Valida firma X-Hub-Signature-256
                  con META_APP_SECRET sobre el body crudo, responde 200 de
                  inmediato (salvo firma invalida) y procesa el payload en
                  una task en background para que Meta no reintente en loop.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response

from config import META_APP_SECRET, META_VERIFY_TOKEN
import dedup
from dedup import limpieza_oportunista, ya_procesado
from state_machine import procesar_mensaje

log = logging.getLogger("kanm.main")

app = FastAPI(title="Kan M WhatsApp Bot", version="0.1.0")

# Referencias fuertes a las tasks en background (evita que el GC las cancele).
_TAREAS_ACTIVAS: set = set()

# Serializa el procesamiento de mensajes por telefono (evita respuestas
# entrelazadas cuando el cliente manda varios mensajes seguidos).
_LOCKS_POR_PHONE: dict[str, asyncio.Lock] = {}

# Mensajes con mas de esta antiguedad (epoch de Meta) se descartan.
_MAX_EDAD_MENSAJE_SEC = 600


# ============================================================
# Health
# ============================================================
@app.get("/")
async def health() -> dict:
    return {"status": "ok", "service": "kanm-whatsapp-bot"}


# ============================================================
# GET /webhook  — verificacion de Meta
# ============================================================
@app.get("/webhook")
async def webhook_verify(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
):
    if not META_VERIFY_TOKEN:
        log.error("META_VERIFY_TOKEN no configurado")
        raise HTTPException(status_code=500, detail="server misconfigured")

    if hub_mode == "subscribe" and hmac.compare_digest(
        hub_verify_token or "", META_VERIFY_TOKEN
    ):
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="forbidden")


# ============================================================
# POST /webhook  — recepcion de mensajes
# ============================================================
@app.post("/webhook")
async def webhook_receive(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(default=None),
):
    raw = await request.body()

    # 1) Validar firma con el body CRUDO
    if not _firma_valida(raw, x_hub_signature_256):
        log.warning("Firma X-Hub-Signature-256 invalida")
        raise HTTPException(status_code=403, detail="invalid signature")

    # 2) Parsear (reutilizamos los bytes crudos para no consumir el stream dos veces)
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        log.warning("Body no es JSON valido")
        return Response(content='{"status":"ignored"}', media_type="application/json")

    # 3) Procesar en background y devolver 200 de inmediato para que
    #    Meta no reintente por timeout.
    task = asyncio.create_task(_procesar_payload_seguro(payload))
    _TAREAS_ACTIVAS.add(task)
    task.add_done_callback(_TAREAS_ACTIVAS.discard)

    return Response(content='{"status":"ok"}', media_type="application/json")


# ============================================================
# Validacion de firma (timing-safe)
# ============================================================
def _firma_valida(raw_body: bytes, header_val: Optional[str]) -> bool:
    if not META_APP_SECRET:
        log.error("META_APP_SECRET no configurado — rechazo por seguridad")
        return False
    if not header_val or not header_val.startswith("sha256="):
        return False
    enviado = header_val.split("=", 1)[1].strip()
    esperado = hmac.new(
        META_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(enviado, esperado)


# ============================================================
# Procesamiento del payload
# ============================================================
async def _procesar_payload_seguro(payload: dict) -> None:
    """Wrapper con try/except + limpieza oportunista de la tabla de dedupe."""
    try:
        await limpieza_oportunista()
    except Exception as e:
        log.warning("limpieza_oportunista fallo: %s", e)
    try:
        await _procesar_payload(payload)
    except Exception as e:
        log.exception("Error procesando payload: %s", e)


async def _procesar_payload(payload: dict) -> None:
    """Itera el payload tipico de Meta y delega cada mensaje al state machine.
    Tolera con silencio status updates u otros eventos sin 'messages'."""
    if payload.get("object") != "whatsapp_business_account":
        return
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            field = change.get("field")
            if field and field != "messages":
                continue  # echoes de la app, sync de historial, otros eventos de coexistencia
            value = change.get("value") or {}
            messages = value.get("messages") or []
            if not messages:
                continue  # statuses, etc.
            metadata = value.get("metadata") or {}
            numero_negocio = "".join(c for c in (metadata.get("display_phone_number") or "") if c.isdigit())
            contacts = value.get("contacts") or []
            profile_name: Optional[str] = None
            if contacts:
                profile_name = (contacts[0].get("profile") or {}).get("name")
            for msg in messages:
                await _procesar_un_mensaje(msg, profile_name, numero_negocio)


async def _procesar_un_mensaje(msg: dict, profile_name: Optional[str], numero_negocio: str) -> None:
    message_id = msg.get("id") or ""
    if await ya_procesado(message_id):
        log.info("dedup hit message_id=%s", message_id)
        return

    try:
        phone = msg.get("from") or ""
        if not phone:
            return

        phone_digits = "".join(c for c in phone if c.isdigit())
        if numero_negocio and phone_digits == numero_negocio:
            log.info("echo del propio negocio ignorado message_id=%s", message_id)
            return

        # Frescura: Meta puede reenviar webhooks viejos; no responderlos.
        ts_raw = msg.get("timestamp")
        if ts_raw:
            try:
                edad = time.time() - float(ts_raw)
            except (TypeError, ValueError):
                edad = None
            if edad is not None and edad > _MAX_EDAD_MENSAJE_SEC:
                log.info(
                    "mensaje viejo descartado message_id=%s edad=%.0fs",
                    message_id, edad,
                )
                return

        tipo, contenido = _extraer_tipo_y_contenido(msg)

        if len(_LOCKS_POR_PHONE) > 1000:
            _LOCKS_POR_PHONE.clear()
        lock = _LOCKS_POR_PHONE.setdefault(phone, asyncio.Lock())
        async with lock:
            await procesar_mensaje(phone, profile_name, tipo, contenido)
    except Exception:
        # Rollback del dedupe: si Meta reintenta este webhook, que se procese.
        await dedup.eliminar(message_id)
        raise


def _extraer_tipo_y_contenido(msg: dict) -> tuple[str, dict]:
    tipo_meta = msg.get("type") or ""

    if tipo_meta == "text":
        body = (msg.get("text") or {}).get("body", "")
        return "text", {"body": body}

    if tipo_meta == "interactive":
        interactive = msg.get("interactive") or {}
        sub = interactive.get("type")
        if sub in ("list_reply", "button_reply"):
            r = interactive.get(sub) or {}
            return "interactive", {"id": r.get("id", ""), "title": r.get("title", "")}

    # tipos no soportados: imagen, audio, location, contacts, document, ...
    return "other", {}
