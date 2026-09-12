"""Tests de intent.py: deteccion de intencion en texto libre.

Solo logica pura: sin red, sin BD y sin importar config.py.

    py -m pytest tests/
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from intent import (  # noqa: E402
    detectar_intencion,
    detectar_intenciones_multiples,
    es_saludo_o_menu,
)


# ============================================================
# Frases reales -> intencion esperada
# ============================================================
CASOS = [
    # --- cotizar ---
    ("quiero cotizar un bizcocho", "cotizar"),
    ("cuanto me sale un bizcocho para 30 personas", "cotizar"),
    ("necesito una mesa de dulces para un baby shower", "cotizar"),
    ("bizcocho para mi boda", "cotizar"),
    ("hacen catering para eventos?", "cotizar"),
    ("cotisacion para un cumpleaños", "cotizar"),
    # Regresion B7: "personalizado" gana sobre la senal de pedido "quiero un".
    ("quiero un bizcocho personalizado", "cotizar"),
    ("Quiero una torta personalizada para mi hija", "cotizar"),
    ("kiero un bizcocho personalisado", "cotizar"),  # typo s/z

    # --- pedido ---
    ("quiero ordenar", "pedido"),
    ("quiero un bizcocho de chocolate", "pedido"),
    ("kiero pedir unas empanadas", "pedido"),
    ("quiero encargar 6 laticas", "pedido"),
    ("quiero comprar laticas", "pedido"),
    ("como hago un pedido", "pedido"),
    # Antes solo estaban en la lista "pedido", que detectar_intencion no lee
    # por coincidencia exacta ("pedir" ademas es mas corta que _MIN_LEN_KW).
    ("pedir", "pedido"),
    ("ya quiero", "pedido"),
    ("lo quiero", "pedido"),
    # _COTIZAR_STRONG sigue ganando sobre la senal de pedido "ya quiero".
    ("ya quiero cotizar un evento", "cotizar"),

    # --- delivery ---
    ("hacen delivery?", "delivery"),
    ("tienen delivery a los alcarrizos", "delivery"),
    ("lo llevan a domicilio", "delivery"),
    ("estan en uber eats", "delivery"),
    ("hacen dilivery?", "delivery"),  # typo
    ("me lo mandan a la casa?", "delivery"),

    # --- horario ---
    ("a que hora abren", "horario"),
    ("¿A qué hora abren?", "horario"),
    # Regresion B2: "atencion" ya no dispara humano.
    ("cual es su horario de atencion?", "horario"),
    ("Cuál es su horario de atención?", "horario"),
    ("estan abiertos hoy?", "horario"),
    ("a ke ora cierran", "horario"),
    ("cual es el orario", "horario"),  # typo

    # --- ubicacion ---
    ("donde quedan", "ubicacion"),
    ("cual es la direccion", "ubicacion"),
    ("¿Dónde están ubicados?", "ubicacion"),
    ("como llego alla", "ubicacion"),
    ("pasame la ubicasion", "ubicacion"),  # typo

    # --- humano ---
    ("quiero hablar con alguien", "humano"),
    ("pasame con un asesor", "humano"),
    ("necesito hablar con un humano", "humano"),
    ("quiero hablar con una persona", "humano"),

    # --- sin intencion (None) ---
    ("hola", None),
    ("gracias", None),
    ("klk", None),
    ("ok perfecto", None),
    ("buenas tardes", None),
    # Regresion B21 (contenido real que no es saludo ni intencion).
    ("ya llegue al local", None),
]


@pytest.mark.parametrize("texto,esperado", CASOS)
def test_detectar_intencion(texto, esperado):
    assert detectar_intencion(texto) == esperado


# ============================================================
# Regresion B2: frases que antes escalaban a humano por error
# ============================================================
@pytest.mark.parametrize("texto", [
    "cual es su horario de atencion?",
    "bizcocho para una persona",
    "no entiendo",
])
def test_b2_no_escala_a_humano_por_falso_positivo(texto):
    assert detectar_intencion(texto) != "humano"


# ============================================================
# Segunda intencion
# ============================================================
def test_pregunta_de_precio_no_ofrece_cotizar_como_segunda_intencion():
    # "cuanto seria" estaba en la lista "cotizar"; detectar_intencion la
    # ignoraba, pero detectar_intenciones_multiples la usaba y ofrecia cotizar.
    assert "cotizar" not in detectar_intenciones_multiples("cuanto seria el delivery")


# ============================================================
# Limitacion conocida y aceptada: "atencion al cliente" -> humano
# ============================================================
# "humano" se evalua antes que "horario" y no reordenamos prioridades por
# este caso. Este test NO fija un resultado deseable: existe para detectar si
# alguien cambia ese orden de prioridad sin darse cuenta.
@pytest.mark.parametrize("texto", [
    "horario de atencion al cliente",
    "Horario de atención al cliente",
])
def test_limitacion_conocida_atencion_al_cliente_gana_humano(texto):
    assert detectar_intencion(texto) == "humano"


# ============================================================
# Saludos / muletillas (incluye regresion B21)
# ============================================================
@pytest.mark.parametrize("texto,esperado", [
    ("hola", True),
    ("Hola!", True),
    ("klk", True),
    ("buenas tardes", True),
    ("menú", True),
    ("ok gracias", True),
    ("ya", True),
    # Regresion B21: "ya " como prefijo no convierte la frase en saludo.
    ("ya llegue al local", False),
    ("ya pague", False),
    ("quiero cotizar", False),
])
def test_es_saludo_o_menu(texto, esperado):
    assert es_saludo_o_menu(texto) is esperado
