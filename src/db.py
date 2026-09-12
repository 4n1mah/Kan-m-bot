"""Conexion a Neon Postgres y operaciones sobre las tablas wa_*.

Patron pool lazy + singleton, pensado para serverless (Vercel): el pool se
crea en la primera invocacion del contenedor y se reutiliza mientras este
caliente.

NO toca tablas de la web. Solo wa_conversations y wa_processed_messages.
"""
import asyncio
import json
import logging
from typing import Optional

import asyncpg

from config import DATABASE_URL

log = logging.getLogger("kanm.db")

_pool: Optional[asyncpg.Pool] = None
_pool_lock = asyncio.Lock()


# ============================================================
# Pool e inicializacion
# ============================================================
async def get_pool() -> asyncpg.Pool:
    """Crea (o reutiliza) el pool. Lazy para que funcione bien en serverless.

    Usa un lock + double-check para evitar que dos corutinas concurrentes
    creen el pool dos veces en la primera invocacion."""
    global _pool
    if _pool is None:
        async with _pool_lock:
            # double-check: otra corutina pudo crearlo mientras esperabamos el lock
            if _pool is None:
                if not DATABASE_URL:
                    raise RuntimeError("DATABASE_URL no configurado")
                pool = await asyncpg.create_pool(
                    DATABASE_URL,
                    min_size=1,
                    max_size=5,
                    command_timeout=10,
                )
                await _init_schema(pool)
                _pool = pool
    return _pool


async def _init_schema(pool: asyncpg.Pool) -> None:
    """Crea las tablas wa_* si no existen. Idempotente."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wa_conversations (
                phone TEXT PRIMARY KEY,
                name TEXT,
                state TEXT NOT NULL DEFAULT 'nuevo',
                context JSONB DEFAULT '{}'::jsonb,
                fallback_count INT DEFAULT 0,
                escalated_at TIMESTAMPTZ,
                last_inbound_at TIMESTAMPTZ,
                last_outbound_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wa_processed_messages (
                message_id TEXT PRIMARY KEY,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_wa_processed_messages_created_at
                ON wa_processed_messages (created_at);
            """
        )


# ============================================================
# Conversaciones
# ============================================================
async def get_conversation(phone: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM wa_conversations WHERE phone = $1", phone
    )
    if not row:
        return None
    data = dict(row)
    # asyncpg puede devolver JSONB como dict o como str segun configuracion
    if isinstance(data.get("context"), str):
        try:
            data["context"] = json.loads(data["context"])
        except Exception:
            data["context"] = {}
    return data


async def ensure_conversation(phone: str) -> dict:
    """Crea la conversacion si no existe, actualiza last_inbound_at y la devuelve."""
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO wa_conversations (phone, state, last_inbound_at)
        VALUES ($1, 'nuevo', now())
        ON CONFLICT (phone) DO UPDATE
            SET last_inbound_at = now(),
                updated_at = now()
        """,
        phone,
    )
    convo = await get_conversation(phone)
    if convo is None:
        raise RuntimeError(
            f"ensure_conversation: la conversacion para phone={phone} "
            "no existe tras el upsert"
        )
    return convo


async def update_state(phone: str, new_state: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE wa_conversations SET state = $2, updated_at = now() WHERE phone = $1",
        phone, new_state,
    )


async def update_context(phone: str, patch: dict) -> None:
    """Merge superficial del JSONB context."""
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE wa_conversations
           SET context = COALESCE(context, '{}'::jsonb) || $2::jsonb,
               updated_at = now()
         WHERE phone = $1
        """,
        phone, json.dumps(patch),
    )


async def set_name(phone: str, name: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE wa_conversations SET name = $2, updated_at = now() WHERE phone = $1",
        phone, name,
    )


async def increment_fallback(phone: str) -> int:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE wa_conversations
           SET fallback_count = fallback_count + 1, updated_at = now()
         WHERE phone = $1
        RETURNING fallback_count
        """,
        phone,
    )
    return int(row["fallback_count"]) if row else 0


async def reset_fallback(phone: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE wa_conversations SET fallback_count = 0, updated_at = now() WHERE phone = $1",
        phone,
    )


async def mark_escalated(phone: str, motivo: str = "manual") -> None:
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE wa_conversations
           SET state = 'escalado_humano',
               escalated_at = now(),
               context = COALESCE(context, '{}'::jsonb) ||
                         jsonb_build_object('escalado_motivo', $2::text),
               updated_at = now()
         WHERE phone = $1
        """,
        phone, motivo,
    )


HISTORIAL_MAX_MENSAJES = 8  # ultimos N mensajes (cliente+bot) que se conservan


async def append_historial(phone: str, rol: str, texto: str) -> None:
    """Anexa un turno al historial de la conversacion (context.historial),
    de forma atomica via transaccion, y recorta a los ultimos HISTORIAL_MAX_MENSAJES.

    Opcion A: dos UPDATEs dentro de la misma transaccion — evita race conditions
    y la ventana de lectura-escritura de la Opcion B.

    rol: 'cliente' | 'bot'
    """
    pool = await get_pool()
    entrada = json.dumps({"rol": rol, "texto": texto[:500]})
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE wa_conversations
                   SET context = jsonb_set(
                           COALESCE(context, '{}'::jsonb),
                           '{historial}',
                           COALESCE(context->'historial', '[]'::jsonb)
                           || jsonb_build_array($2::jsonb)
                       ),
                       updated_at = now()
                 WHERE phone = $1
                """,
                phone, entrada,
            )
            await conn.execute(
                """
                UPDATE wa_conversations
                   SET context = jsonb_set(
                           context,
                           '{historial}',
                           (
                               SELECT jsonb_agg(elem)
                               FROM (
                                   SELECT elem
                                   FROM jsonb_array_elements(context->'historial') elem
                                   OFFSET GREATEST(
                                       jsonb_array_length(context->'historial') - $2,
                                       0
                                   )
                               ) sub
                           )
                       )
                 WHERE phone = $1
                """,
                phone, HISTORIAL_MAX_MENSAJES,
            )


async def get_historial(phone: str) -> list[dict]:
    """Devuelve la lista de turnos recientes (cliente+bot) en orden cronologico.
    Lista vacia si no hay historial."""
    convo = await get_conversation(phone)
    if not convo:
        return []
    ctx = convo.get("context") or {}
    historial = ctx.get("historial") or []
    return historial if isinstance(historial, list) else []


async def touch_outbound(phone: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE wa_conversations SET last_outbound_at = now() WHERE phone = $1",
        phone,
    )
