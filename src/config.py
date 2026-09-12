"""Carga de variables de entorno y constantes del negocio.

Todo el codigo importa desde aqui sus configuraciones. NO leer os.getenv en
otros modulos: centralizado para que sea facil ver que hay configurable.
"""
import logging
import os
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()


# ============================================================
# Meta (WhatsApp Cloud API)
# ============================================================
META_VERIFY_TOKEN: str = os.getenv("META_VERIFY_TOKEN", "")
META_WHATSAPP_TOKEN: str = os.getenv("META_WHATSAPP_TOKEN", "")
META_PHONE_NUMBER_ID: str = os.getenv("META_PHONE_NUMBER_ID", "")
META_APP_SECRET: str = os.getenv("META_APP_SECRET", "")
META_GRAPH_VERSION: str = os.getenv("META_GRAPH_VERSION", "v21.0")
META_API_BASE: str = f"https://graph.facebook.com/{META_GRAPH_VERSION}"


# ============================================================
# Base de datos (Neon Postgres)
# ============================================================
DATABASE_URL: str = os.getenv("DATABASE_URL", "")


# ============================================================
# IA — Gemini (Google Developer API, sin Vertex AI)
# ============================================================
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")


# ============================================================
# Notificaciones internas
# ============================================================
OWNER_PHONE: str = os.getenv("OWNER_PHONE", "")


# ============================================================
# Logging
# ============================================================
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ============================================================
# Constantes del negocio
# ============================================================
NEGOCIO_DIRECCION: str = "C. Espaillat 58, Zona Colonial, Santo Domingo"

# Fallbacks en menu_principal: al exceder esto se ofrece "hablar con persona".
# 1 => 1er fallo reformula, 2do fallo ofrece humano.
MAX_FALLBACKS_ANTES_DE_OFRECER_HUMANO: int = 1

# Retencion de la tabla de dedupe (horas). La limpieza se dispara oportunista.
DEDUP_RETENTION_HORAS: int = 24

# Inactividad: tras N minutos sin mensajes del cliente, al volver le
# preguntamos si quiere "continuar donde lo dejamos" o "empezar de nuevo".
INACTIVITY_THRESHOLD_MIN: int = 120


# ============================================================
# Horario de atencion (calculado localmente, sin API externa)
# ============================================================
# Horas en formato 24h. Lunes=0 ... Domingo=6 (datetime.weekday()).
HORARIO_NEGOCIO: dict[str, tuple[int, int]] = {
    "lunes_jueves": (9, 19),    # 9:00 AM - 7:00 PM
    "viernes_domingo": (9, 22), # 9:00 AM - 10:00 PM
}
HORARIO_TEXTO: str = (
    "Lunes a jueves: 9:00 AM - 7:00 PM\n"
    "Viernes a domingo: 9:00 AM - 10:00 PM"
)

# ============================================================
# Validacion de startup
# ============================================================
# Falla rapido al arrancar si falta alguna variable critica, en vez de
# fallar en runtime cuando llega el primer webhook.
_REQUERIDAS = {
    "META_VERIFY_TOKEN": META_VERIFY_TOKEN,
    "META_WHATSAPP_TOKEN": META_WHATSAPP_TOKEN,
    "META_PHONE_NUMBER_ID": META_PHONE_NUMBER_ID,
    "META_APP_SECRET": META_APP_SECRET,
    "DATABASE_URL": DATABASE_URL,
    "OWNER_PHONE": OWNER_PHONE,
    "GEMINI_API_KEY": GEMINI_API_KEY,
}
_faltantes = [nombre for nombre, valor in _REQUERIDAS.items() if not valor]
if _faltantes:
    raise EnvironmentError(
        "Faltan variables de entorno requeridas: " + ", ".join(_faltantes)
    )


def negocio_esta_abierto() -> bool:
    """True si la hora actual (zona horaria de Santo Domingo, UTC-4,
    sin horario de verano) cae dentro del horario de atencion."""
    from datetime import timezone, timedelta
    tz_rd = timezone(timedelta(hours=-4))
    ahora = datetime.now(tz_rd)
    dia_semana = ahora.weekday()  # 0=lunes ... 6=domingo
    hora_actual = ahora.hour + ahora.minute / 60

    if dia_semana <= 3:  # lunes a jueves
        apertura, cierre = HORARIO_NEGOCIO["lunes_jueves"]
    else:  # viernes a domingo
        apertura, cierre = HORARIO_NEGOCIO["viernes_domingo"]

    return apertura <= hora_actual < cierre
