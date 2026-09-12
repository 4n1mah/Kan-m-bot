"""Deduplicacion de mensajes entrantes via tabla wa_processed_messages.

Meta puede reintentar el mismo webhook. Usamos INSERT ... ON CONFLICT
DO NOTHING ... RETURNING para resolver el chequeo + insert en una sola
operacion atomica (evita race conditions entre invocaciones concurrentes).
"""
import logging
import random

from config import DEDUP_RETENTION_HORAS
from db import get_pool

log = logging.getLogger("kanm.dedup")


async def ya_procesado(message_id: str) -> bool:
    """True si el message_id ya estaba. False si recien lo insertamos."""
    if not message_id:
        return False
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO wa_processed_messages (message_id)
        VALUES ($1)
        ON CONFLICT (message_id) DO NOTHING
        RETURNING message_id
        """,
        message_id,
    )
    # row is None -> hubo conflicto -> ya existia
    return row is None


async def eliminar(message_id: str) -> None:
    """Quita un message_id de la tabla (rollback del dedupe cuando el
    procesamiento fallo, para que un reintento de Meta si se procese)."""
    if not message_id:
        return
    try:
        pool = await get_pool()
        await pool.execute(
            "DELETE FROM wa_processed_messages WHERE message_id = $1",
            message_id,
        )
    except Exception as e:
        log.warning("Fallo rollback dedupe message_id=%s: %s", message_id, e)


async def limpiar_antiguos() -> int:
    """Borra filas con mas de DEDUP_RETENTION_HORAS horas. Devuelve cantidad."""
    pool = await get_pool()
    result = await pool.execute(
        """
        DELETE FROM wa_processed_messages
         WHERE created_at < now() - interval '1 hour' * $1
        """,
        DEDUP_RETENTION_HORAS,
    )
    try:
        return int(result.split()[-1])  # asyncpg devuelve 'DELETE n'
    except Exception:
        return 0


async def limpieza_oportunista(probabilidad: float = 0.02) -> None:
    """Con baja probabilidad por request, dispara la limpieza.
    Mientras no haya cron real, esto evita que la tabla crezca sin limite."""
    if random.random() >= probabilidad:
        return
    try:
        n = await limpiar_antiguos()
        if n:
            log.info("Limpieza wa_processed_messages: %d filas borradas", n)
    except Exception as e:
        log.warning("Fallo limpieza dedupe: %s", e)
