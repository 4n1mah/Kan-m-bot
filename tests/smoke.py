"""Smoke tests V2 (sin framework). Ejecutar:

    py tests/smoke.py

Verifica intent detection, config y funciones basicas del bot V2.
No requiere conexion a servicios externos ni base de datos.
Si config.py no puede cargar (env vars faltantes), los tests de config
se omiten sin fallar — solo se marcan como SKIP.

Si algo falla, sale con codigo 1.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

# intent.py no depende de config — siempre importable
from intent import _INTENCIONES, detectar_intencion

# config.py requiere vars de entorno; importar con manejo graceful
try:
    import config
    _config_ok = True
except (ImportError, EnvironmentError) as _cfg_err:
    config = None  # type: ignore[assignment]
    _config_ok = False
    print(f"[WARN] config no disponible ({str(_cfg_err)[:100]}) "
          "— tests de config se omiten\n")


# ============================================================
# Test 1 — Deteccion de intenciones clave
# ============================================================
def test_intent_detecta_intenciones_clave() -> None:
    """Verifica que detectar_intencion() devuelva el resultado esperado
    para frases representativas de cada intencion.

    NOTA: laticas y empanadas ya NO son intenciones keyword — Gemini las
    maneja directamente desde el knowledge_base (CAPA 2). Verificamos que
    devuelven None (correcto: pasan a Gemini) en vez de un intent obsoleto.
    """
    casos = [
        # (nombre_intencion, texto_entrada, resultado_esperado_real)
        ("cotizar",   "quiero cotizar un bizcocho",  "cotizar"),
        ("delivery",  "hacen delivery",              "delivery"),
        ("ubicacion", "donde quedan",                "ubicacion"),
        ("horario",   "a que hora abren",            "horario"),
        ("pedido",    "quiero ordenar",              "pedido"),
        ("humano",    "hablar con alguien",          "humano"),
    ]
    for nombre, texto, esperado in casos:
        resultado = detectar_intencion(texto)
        assert resultado == esperado, (
            f"intencion '{nombre}': '{texto}' -> '{resultado}' "
            f"(esperado '{esperado}')"
        )
        print(f"  [OK] {nombre}: '{texto}' -> '{resultado}'")

    # Laticas y empanadas → None (van a Gemini, no a keyword handler)
    for texto_info in ("laticas", "quiero empanadas"):
        r = detectar_intencion(texto_info)
        assert r not in ("laticas", "empanadas"), (
            f"'{texto_info}' -> '{r}' — laticas/empanadas no deben ser intenciones keyword"
        )
        print(f"  [OK] '{texto_info}' -> {r!r} (Gemini lo maneja)")

    # Preguntas de precio → "precio" (Gemini las maneja via _atender_intencion retorno False)
    resultado_precio = detectar_intencion("cuanto cuesta la latica")
    assert resultado_precio == "precio", (
        f"'cuanto cuesta la latica' -> '{resultado_precio}' (esperado 'precio')"
    )
    print(f"  [INFO] 'cuanto cuesta la latica' -> '{resultado_precio}' (precio va a Gemini)")

    print("[OK] test_intent_detecta_intenciones_clave")


# ============================================================
# Test 2 — "catalogo" eliminado del diccionario
# ============================================================
def test_intent_ya_no_tiene_catalogo() -> None:
    assert "catalogo" not in _INTENCIONES, \
        "'catalogo' sigue en _INTENCIONES — debio eliminarse en V2"

    resultado = detectar_intencion("catalogo de productos")
    assert resultado != "catalogo", (
        f"detectar_intencion devolvio 'catalogo' para 'catalogo de productos' "
        "— intencion no eliminada correctamente"
    )
    print(f"  [OK] 'catalogo' no esta en _INTENCIONES")
    print(f"  [INFO] 'catalogo de productos' -> {resultado!r} "
          "(None o FAQ — no hay intencion 'catalogo')")
    print("[OK] test_intent_ya_no_tiene_catalogo")


# ============================================================
# Test 3 — negocio_esta_abierto() no crashea
# ============================================================
def test_config_negocio_esta_abierto_no_truena() -> None:
    if not _config_ok:
        print("[SKIP] test_config_negocio_esta_abierto_no_truena")
        return
    resultado = config.negocio_esta_abierto()
    assert isinstance(resultado, bool), (
        f"negocio_esta_abierto() debe devolver bool, devolvio {type(resultado)}"
    )
    print(f"  [INFO] negocio_esta_abierto() -> {resultado}")
    print("[OK] test_config_negocio_esta_abierto_no_truena")


# ============================================================
# Test 4 — Constantes de negocio existen y no estan vacias
# ============================================================
def test_config_constantes_negocio_existen() -> None:
    if not _config_ok:
        print("[SKIP] test_config_constantes_negocio_existen")
        return

    constantes_requeridas = {
        "NEGOCIO_DIRECCION":        config.NEGOCIO_DIRECCION,
        "HORARIO_TEXTO":            config.HORARIO_TEXTO,
    }
    for nombre, valor in constantes_requeridas.items():
        assert valor, f"{nombre} existe pero esta vacio"
        print(f"  [OK] {nombre} = {valor!r}")

    print("[OK] test_config_constantes_negocio_existen")


# ============================================================
# Runner
# ============================================================
if __name__ == "__main__":
    try:
        test_intent_detecta_intenciones_clave()
        test_intent_ya_no_tiene_catalogo()
        test_config_negocio_esta_abierto_no_truena()
        test_config_constantes_negocio_existen()
        print("\n*** smoke tests OK ***")
    except AssertionError as e:
        print(f"\n!!! FALLO: {e}")
        sys.exit(1)
