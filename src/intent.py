"""Normalizacion de texto y deteccion de intencion en texto libre.

REQUISITO CRITICO (heredado del bot anterior, NO se puede perder):
el bot debe entender al cliente cuando escribe libre, no solo cuando
aprieta botones. Tolera typos y vocabulario dominicano.

Las claves de intencion devueltas aqui son las mismas que usan
state_machine._atender_intencion() y los IDs del menu interactivo:
    cotizar · ubicacion · horario · delivery · pedido · precio · faq · humano

Productos como Laticas o empanadas NO son intenciones: una pregunta sobre
ellos devuelve "precio", "faq" o None y la resuelve la IA (CAPA 2).

Diseno:
  1) PRIORIDAD: humano > cotizar > precio > pedido > delivery /
     ubicacion / horario (gana la que aparece antes en el texto) > faq.
     Las "senales fuertes" de cotizar (evento/boda/baby shower/para N
     personas) y de pedido ("quiero pedir", "kiero un X") ganan sobre las
     informativas aunque el texto mencione un producto.
  2) Match exacto de substring (en orden de prioridad).
  3) Fuzzy: tokens del texto contra keywords de una palabra usando
     difflib.SequenceMatcher con FUZZY_UMBRAL.
  4) Pista FAQ para preguntas abiertas.
"""
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional


# Umbral de similitud para fuzzy matching (texto ya normalizado).
# Calibrado para que "biscocho"~"bizcocho" matchee pero "hola" NO matchee "horario".
FUZZY_UMBRAL = 0.82

# Longitudes minimas para evitar ruido en el fuzzy.
_MIN_LEN_TOKEN = 3
_MIN_LEN_KW = 6


# Saludos y muletillas de continuidad/asentimiento.
# En dominicano "klk" y "que lo que" son saludos. "ok", "dale", "listo",
# "perfecto" son confirmaciones que NO deben caer a fallback.
_SALUDOS = {
    # saludos clasicos
    "hola", "holaa", "holi", "buenas", "buenos dias", "buenas tardes",
    "buenas noches", "buen dia", "que tal", "saludos", "ola",
    "hello", "hi", "hey",
    # saludos dominicanos
    "klk", "k l k", "qlq", "kl k", "ke lo ke", "ke lo que",
    "que lo que", "que lo q", "q lo q",
    # pedidos de menu
    "menu", "menú", "opciones", "ayuda", "inicio", "empezar", "start",
    "volver",
    # continuidad / asentimiento (no son errores)
    "ok", "oka", "okey", "okay", "dale", "ta bien", "tabien", "ta bn", "tabn",
    "aja", "aha", "ahh", "ah", "ya", "listo", "perfecto", "bien", "buenisimo",
    "gracias", "ok gracias",
}

# Muletillas que solo cuentan como saludo si son el mensaje completo. Como
# prefijo introducen contenido real ("ya llegue al local", "ya pague").
_SALUDOS_SOLO_EXACTOS = {"ya"}

# Mapa intencion -> palabras/frases clave (todas ya normalizadas:
# lowercase, sin tildes, sin puntuacion). Incluye vocabulario dominicano
# real y variantes con typos comunes.
_INTENCIONES: dict[str, list[str]] = {
    "humano": [
        "hablar con alguien", "hablar con una persona", "atencion al cliente",
        "humano", "un humano",
        "hablar con un humano", "hablar con alguien real",
        "agente", "asesor", "operador",
        "ayudenme", "necesito ayuda", "alguien que me ayude",
        "ayuda real",
    ],
    "cotizar": [
        "cotizar", "cotizacion", "cotizame", "cotizenme", "presupuesto",
        "evento", "eventos", "cumpleano", "cumpleanos", "cumple",
        "boda", "bodas", "baby shower", "babyshower", "graduacion",
        "compromiso",
        "para un evento",
        "personalizado", "personalizada", "tematico", "tematica",
        "mesa dulce", "mesas dulces", "mesa de dulce", "mesa de dulces",
    ],
    "pedido": [
        "ordenar", "hacer pedido", "hacer un pedido",
        "quiero pedir", "kiero pedir", "quiero ordenar", "kiero ordenar",
        "kiero un", "kiero una", "quiero un", "quiero una",
        "encargar", "como pido", "como ordeno", "comprar",
    ],
    "precio": [
        "a como", "a cuanto", "que precio", "ke precio",
        "cuanto cuesta", "cuanto vale", "cuanto sale", "cuanto e",
        "precio", "precios", "valor",
    ],
    "delivery": [
        "delivery", "domicilio", "a domicilio",
        "envio", "envios", "envian", "llevan", "lo llevan",
        "uber", "ubereats", "uber eats", "pedidosya", "pedido ya",
        "reparto", "hacen entrega", "entregan", "mandan",
    ],
    "ubicacion": [
        "ubicacion", "ubicados", "direccion", "donde estan", "donde quedan",
        "donde estas", "donde queda", "donde e", "donde ta",
        "como llego", "como llegar", "por donde", "sucursal",
    ],
    "horario": [
        "horario", "horarios", "abren", "cierran",
        "cuando abren", "estan abiertos", "estan abierto", "abierto", "cerrado",
        "a que hora", "a ke ora", "a que hora abren", "que hora abren",
        "hasta que hora", "abren hoy", "trabajan hoy",
        "hora abren", "hora cierran", "abierto hoy",
    ],
}

# Orden de iteracion de _INTENCIONES en la busqueda aproximada y en
# detectar_intenciones_multiples (humano siempre primero).
_ORDEN_PRIORIDAD = [
    "humano", "cotizar", "pedido", "precio",
    "delivery", "ubicacion", "horario",
]

# Senales FUERTES de cotizar: si alguna aparece, gana sobre pedido y sobre las
# informativas aunque el texto tambien mencione productos (ej. "bizcocho para mi boda" -> cotizar).
_COTIZAR_STRONG = (
    "evento", "eventos", "boda", "bodas",
    "cumpleano", "cumpleanos", "cumple",
    "baby shower", "babyshower", "graduacion", "compromiso",
    "para un evento", "mesa de dulce", "mesa de dulces", "mesa dulce", "mesas dulces",
    "presupuesto", "cotizar", "cotizacion", "cotizame", "cotizenme",
    # Un encargo personalizado siempre se cotiza, aunque diga "quiero un…".
    "personalizado", "personalizada", "personalizados", "personalizadas",
    # Seseo dominicano (s por z): no es un typo aleatorio sino como se escribe
    # de verdad. Se cubre explicito porque las senales fuertes exigen la palabra
    # exacta y el fuzzy corre despues de que "quiero un" ya dio pedido.
    "personalisado", "personalisada",
)
# Patron "para N personas" o "para N gente" (regex, normalizado).
_COTIZAR_REGEX_PERSONAS = re.compile(r"\bpara\s+\d+\s+(personas?|gente|invitados?)\b")

# Senales FUERTES de pedido: si aparecen, gana sobre las informativas.
_PEDIDO_STRONG = (
    "hacer pedido", "hacer un pedido", "hago un pedido",
    "pedir", "ya quiero", "lo quiero",
    "quiero pedir", "kiero pedir",
    "quiero ordenar", "kiero ordenar",
    "kiero un", "kiero una", "quiero un", "quiero una",
    "encargar", "ordenar",
    "como pido",
    "como ordeno",
)

# Pistas que sugieren pregunta abierta -> intentar resolver via FAQ.
_PISTAS_FAQ = (
    "?", "puedo", "aceptan", "como funciona",
    "cuantos", "cuanta", "cuantas", "cobran", "incluye",
    "tarjeta", "transferencia", "pagar", "pago",
)


# ============================================================
# Normalizacion
# ============================================================
def normalizar(texto: str) -> str:
    """Lowercase, sin tildes, sin puntuacion, espacios colapsados.

    Conserva la presencia del signo de pregunta como un token " ?" al final,
    porque es una pista util para detectar intencion FAQ.
    """
    if not texto:
        return ""
    t = texto.lower().strip()
    tiene_interrogacion = "?" in t or "¿" in t
    # NFD + filtrar combining marks -> quita tildes
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    # quitar puntuacion (deja letras, numeros, _, espacios)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if tiene_interrogacion:
        t = (t + " ?").strip()
    return t


# ============================================================
# Saludo / muletilla de continuidad
# ============================================================
def es_saludo_o_menu(texto: str) -> bool:
    """True si el texto es un saludo, pedido de menu, o muletilla de
    continuidad/asentimiento (ok, dale, listo, klk, etc.)."""
    t = normalizar(texto).replace(" ?", "").strip()
    if not t:
        return False
    if t in _SALUDOS:
        return True
    for s in _SALUDOS - _SALUDOS_SOLO_EXACTOS:
        if t.startswith(s + " "):
            return True
    return False


# ============================================================
# Fuzzy helper (typos / variantes)
# ============================================================
def _parecido(palabra: str, keyword: str, umbral: float = FUZZY_UMBRAL) -> bool:
    """True si dos textos ya normalizados se parecen lo suficiente.

    Usa difflib.SequenceMatcher (stdlib). Calibrado con FUZZY_UMBRAL.
    """
    if not palabra or not keyword:
        return False
    return SequenceMatcher(None, palabra, keyword).ratio() >= umbral


# ============================================================
# Matching con limites de palabra
# ============================================================
def _contiene_kw(t: str, kw: str) -> bool:
    """True si `kw` aparece en `t` como palabra/frase completa (no como
    fragmento dentro de otra palabra, ej. 'hora' NO matchea 'ahorita')."""
    return re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", t) is not None


def _posicion_kw(t: str, kw: str) -> int:
    """Posicion del primer match de `kw` con limites de palabra, o -1."""
    m = re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", t)
    return m.start() if m else -1


# ============================================================
# Senales fuertes
# ============================================================
def _hay_senal_cotizar(t_norm: str) -> bool:
    for kw in _COTIZAR_STRONG:
        if _contiene_kw(t_norm, kw):
            return True
    if _COTIZAR_REGEX_PERSONAS.search(t_norm):
        return True
    return False


def _hay_senal_pedido(t_norm: str) -> bool:
    for kw in _PEDIDO_STRONG:
        if _contiene_kw(t_norm, kw):
            return True
    return False


_PRECIO_STRONG = ("precio", "cuesta", "cuanto", "a como", "vale")


def _hay_senal_precio(t: str) -> bool:
    return any(_contiene_kw(t, kw) for kw in _PRECIO_STRONG)


def _hay_humano(t_norm: str) -> bool:
    for kw in _INTENCIONES["humano"]:
        if _contiene_kw(t_norm, kw):
            return True
    return False


# ============================================================
# Deteccion de intencion (principal)
# ============================================================
def _primer_match_intencion(t: str, intencion: str) -> int:
    """Devuelve la posicion mas temprana en `t` de cualquier keyword de la
    intencion (match con limites de palabra), o -1 si no hay match."""
    mejor = -1
    for kw in _INTENCIONES.get(intencion, ()):
        if not kw:
            continue
        idx = _posicion_kw(t, kw)
        if idx >= 0 and (mejor < 0 or idx < mejor):
            mejor = idx
    return mejor


def detectar_intencion(texto: str) -> Optional[str]:
    """Devuelve la intencion principal o None si no hay match.

    Valores posibles:
      humano · cotizar · precio · pedido · delivery · ubicacion · horario · faq

    Pasos, en orden (el primero que coincide decide). "Exacta" significa
    frase completa con limites de palabra, tras normalizar():

      1) humano   -> _INTENCIONES["humano"], exacta.
      2) cotizar  -> _COTIZAR_STRONG (exacta) + _COTIZAR_REGEX_PERSONAS.
         precio   -> _PRECIO_STRONG, exacta.
         pedido   -> _PEDIDO_STRONG, exacta.
      3) precio   -> _INTENCIONES["precio"], exacta.
      4) delivery / ubicacion / horario -> sus listas de _INTENCIONES,
         exacta; gana la que aparece ANTES en el texto
         ("a que hora abren y donde estan" -> horario).
      5) Busqueda aproximada (SequenceMatcher >= FUZZY_UMBRAL) de cada
         palabra del texto contra las entradas de _INTENCIONES que sean de
         UNA sola palabra y de al menos _MIN_LEN_KW letras, recorriendo
         _ORDEN_PRIORIDAD.
      6) faq      -> cualquier pista de _PISTAS_FAQ como substring.

    IMPORTANTE para quien edite las listas:
      - _INTENCIONES["cotizar"] y _INTENCIONES["pedido"] NO se leen por
        coincidencia exacta en ningun paso. Solo pasan por el paso 5, asi
        que una frase de varias palabras, o una palabra de menos de
        _MIN_LEN_KW letras, que este SOLO en esas listas nunca activa la
        intencion aqui. Para que cuente, ponla en _COTIZAR_STRONG o
        _PEDIDO_STRONG.
      - Aun asi esas listas no son decorativas: detectar_intenciones_multiples
        si las recorre (ver su docstring).
      - Las entradas de _INTENCIONES["precio"] que contienen un termino de
        _PRECIO_STRONG se leen en el paso 3 pero nunca deciden: el paso 2 ya
        devolvio "precio".
    """
    t = normalizar(texto)
    if not t:
        return None

    # 1) Humano siempre primero
    if _hay_humano(t):
        return "humano"

    # 2) Senales fuertes transaccionales
    if _hay_senal_cotizar(t):
        return "cotizar"
    # Si la frase tiene señal de precio, el precio gana sobre pedido.
    # "me das precio del X" debe ir a precio, no a pedido.
    if _hay_senal_precio(t):
        return "precio"
    if _hay_senal_pedido(t):
        return "pedido"

    # 3) Precio gana sobre informativas/etc.
    if _primer_match_intencion(t, "precio") >= 0:
        return "precio"

    # 4) Informativas: posicion mas temprana decide.
    candidatas = []
    for intencion in ("delivery", "ubicacion", "horario"):
        pos = _primer_match_intencion(t, intencion)
        if pos >= 0:
            candidatas.append((pos, intencion))
    if candidatas:
        candidatas.sort(key=lambda x: x[0])
        return candidatas[0][1]

    # 5) Fuzzy: tokens del texto contra keywords de una palabra (>= 4 chars).
    tokens = [w for w in t.replace(" ?", "").split() if len(w) >= _MIN_LEN_TOKEN]
    if tokens:
        for intencion in _ORDEN_PRIORIDAD:
            for kw in _INTENCIONES.get(intencion, ()):
                if not kw or " " in kw or len(kw) < _MIN_LEN_KW:
                    continue
                for tok in tokens:
                    if _parecido(tok, kw):
                        return intencion

    # 6) Heuristica: parece pregunta -> faq
    for pista in _PISTAS_FAQ:
        if pista in t:
            return "faq"
    return None


# ============================================================
# Deteccion de MULTIPLES intenciones (mensajes con 2 pedidos)
# ============================================================
def detectar_intenciones_multiples(texto: str) -> list[str]:
    """Devuelve hasta 2 intenciones, sin duplicados.

    La PRIMERA es siempre la de detectar_intencion(). Si esa devuelve None,
    esta funcion devuelve [] y no busca nada mas.

    La SEGUNDA es la intencion (distinta de la primera) cuya keyword aparece
    antes en el texto. Para buscarla, esta funcion SI recorre todas las
    listas de _INTENCIONES por coincidencia exacta, siguiendo
    _ORDEN_PRIORIDAD, y a cotizar y pedido les suma _COTIZAR_STRONG y
    _PEDIDO_STRONG.

    IMPORTANTE para quien edite las listas: una entrada que
    detectar_intencion() ignora (p. ej. una frase de varias palabras que solo
    esta en _INTENCIONES["cotizar"] o ["pedido"]) SIGUE sirviendo aqui como
    segunda intencion. state_machine la ofrece como boton de seguimiento si
    esta en _INTENCIONES_MENU. Antes de anadir o quitar una entrada, revisa
    su efecto en las dos funciones.

    Ej: "a que hora abren y donde estan" -> ["horario", "ubicacion"].
        "hola" -> [].
    """
    primera = detectar_intencion(texto)
    if not primera:
        return []

    t = normalizar(texto)
    if not t:
        return [primera]

    # Posicion (mas temprana) de cada intencion en el texto.
    posiciones: list[tuple[int, str]] = []
    for intencion in _ORDEN_PRIORIDAD:
        if intencion == primera:
            continue
        kws = list(_INTENCIONES.get(intencion, ()))
        # Para senales fuertes, sumar al pool de la intencion respectiva
        if intencion == "cotizar":
            kws = list(_COTIZAR_STRONG) + kws
        if intencion == "pedido":
            kws = list(_PEDIDO_STRONG) + kws
        mejor = -1
        for kw in kws:
            if not kw:
                continue
            idx = _posicion_kw(t, kw)
            if idx >= 0 and (mejor < 0 or idx < mejor):
                mejor = idx
        if mejor >= 0:
            posiciones.append((mejor, intencion))

    posiciones.sort(key=lambda x: x[0])
    resultado = [primera]
    for _, intencion in posiciones:
        if intencion not in resultado:
            resultado.append(intencion)
            if len(resultado) >= 2:
                break
    return resultado
