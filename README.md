# Kan M WhatsApp Bot

Bot de atención al cliente por WhatsApp para **Kan M Repostería y Catering**,
una repostería de la Zona Colonial de Santo Domingo.

## Qué es y para quién

Es un proyecto hecho para un cliente real y estuvo en producción atendiendo a
sus clientes. No es un ejercicio.

El bot no sustituye a la dueña. Resuelve lo repetitivo (horario, ubicación,
delivery, preguntas frecuentes y la recogida de datos para una cotización) y
deriva a una persona lo que requiere criterio: pedidos, cotizaciones y
peticiones explícitas de hablar con alguien. Al derivar:

1. marca la conversación como escalada y deja de responder;
2. envía a la dueña un WhatsApp con el nombre del cliente, el motivo y, en
   cotizaciones, un resumen de lo recopilado.

A partir de ahí la dueña atiende manualmente.

El bot convive con la app WhatsApp Business en el mismo número
(coexistencia): la dueña responde desde la app y el bot no ve esos mensajes.
El código lo tiene en cuenta de tres formas:

- ignora los eventos del webhook que no son `messages`;
- ignora los mensajes cuyo remitente es el propio número del negocio;
- un chat escalado queda en silencio hasta 3 días, o hasta que el cliente
  escribe "menú", "inicio", "volver" o "empezar".

## Arquitectura

### Recorrido de un mensaje

1. `POST /webhook` valida la firma `X-Hub-Signature-256` con el App Secret de
   Meta, responde 200 y procesa el mensaje en segundo plano.
2. Descarta duplicados por `message_id` y mensajes con más de 10 minutos de
   antigüedad.
3. Toma un lock por teléfono para procesar en orden los mensajes seguidos de
   un mismo cliente.
4. La máquina de estados (`state_machine.py`) decide la respuesta en tres
   capas.

### Las tres capas

**Capa 1: determinista.** Cada estado de la conversación (`nuevo`,
`menu_principal`, `post_info`, `cotizando`, `inactivo_confirmando`,
`escalado_humano`) tiene su handler. `intent.py` clasifica el texto libre con
palabras clave sobre texto normalizado (minúsculas, sin tildes ni
puntuación), prioridades fijas entre intenciones y una búsqueda aproximada
para errores de escritura. La lista del menú y los botones llevan IDs fijos y
no pasan por clasificación.

La capa 1 decide qué acción ejecutar:

| Intención | Acción |
|---|---|
| Horario / ubicación | Responde con dirección y horario de `config.py`. Si el negocio está abierto se calcula localmente (UTC-4). |
| Delivery | Responde con datos fijos del código. |
| Quiero ordenar | Si el negocio está abierto, escala a la dueña. Si está cerrado, informa el horario. |
| Hablar con alguien | Escala a la dueña. |
| Cotizar | Abre un flujo de hasta 4 turnos en el que Gemini hace las preguntas y decide cuándo hay datos suficientes para derivar con un resumen. |

En horario/ubicación, delivery y los mensajes de transición al escalar, Gemini
solo redacta el texto a partir de los datos que le pasa el código, con un
tiempo máximo de 3,5 s. Si falla o tarda, se envía un texto fijo.

**Capa 2: Gemini con `data/knowledge_base.txt`.** Se usa cuando la capa 1 no
reconoce el mensaje, o cuando la intención es una pregunta de precio o una
pregunta abierta. Gemini recibe los últimos 8 turnos de la conversación y el
contenido de `knowledge_base.txt` en el prompt de sistema. El prompt le
prohíbe inventar datos, tomar pedidos y cotizar, y le pide responder
`FUERA_DE_CONTEXTO` en esos casos. Cuando lo hace, el bot muestra el menú en
lugar de enviar esa respuesta.

**Capa 3: fallback.** Si Gemini no devuelve respuesta, el primer fallo envía
un "no estoy seguro de haberte entendido" y el segundo, el menú. Si además hubo
una excepción en las capas anteriores, se avisa a la dueña.

### Por qué menú primero y la IA de respaldo

- **Las acciones con consecuencias no dependen del texto de un modelo.**
  Escalar a la dueña por un pedido o por petición del cliente, y decir si el
  negocio está abierto o cerrado, lo decide código determinista. La excepción
  está acotada: dentro de una cotización, Gemini decide cuándo derivar, con un
  tope de 4 turnos.
- **Los datos operativos vienen del código.** En la capa 1, dirección, horario
  y estado abierto/cerrado salen de `config.py` y se pasan al modelo como
  datos; el prompt le exige no alterarlos.
- **El flujo no depende de que la IA esté disponible.** En horario,
  ubicación, delivery y los mensajes de escalado, la redacción generada tiene
  tiempo máximo y texto fijo de respaldo, así que una caída o lentitud de
  Gemini empeora la redacción, no el recorrido. En la cotización no es así: si
  Gemini falla en el primer turno se envía una pregunta fija, y en los
  siguientes se deriva a la dueña sin resumen.
- **La capa 2 solo se consulta cuando la capa 1 no resuelve.** Menú, botones e
  intenciones reconocidas no pasan por ella.
- **Coste de esta decisión:** la capa 1 es rígida. Reconocer una frase nueva
  exige añadir palabras clave, y las prioridades entre intenciones producen
  casos límite (ver *Limitaciones conocidas*). Por eso los tests se concentran
  en `intent.py`.

## Stack

- Python 3.10 o superior (el código usa la sintaxis `X | None` en firmas).
- FastAPI, servido con Uvicorn.
- asyncpg sobre Neon Postgres.
- httpx para la WhatsApp Cloud API de Meta (Graph API, versión por defecto
  `v21.0`).
- `google-genai` con el modelo `gemini-2.5-flash`, autenticado con API key.
- Despliegue en Railway con Nixpacks.

## Estructura

```
src/
  main.py            FastAPI: health check y webhook de Meta
  state_machine.py   estados de la conversación y las tres capas
  intent.py          normalización y detección de intención
  ai_client.py       cliente de Gemini
  meta_client.py     envío de mensajes por la WhatsApp Cloud API
  db.py              pool de Postgres y operaciones sobre wa_conversations
  dedup.py           deduplicación con wa_processed_messages
  config.py          variables de entorno y constantes del negocio
data/
  knowledge_base.txt información del negocio que usa la capa 2
tests/
  test_intent.py     suite de pytest de intent.py
  smoke.py           script de comprobación sin framework
Procfile, railway.json   arranque en Railway
```

## Cómo correrlo

### Variables de entorno

El arranque falla con `EnvironmentError` si falta cualquiera de estas:

| Variable | Uso |
|---|---|
| `META_VERIFY_TOKEN` | Se compara con `hub.verify_token` en la verificación del webhook. |
| `META_WHATSAPP_TOKEN` | Token Bearer para enviar mensajes por la Graph API. |
| `META_PHONE_NUMBER_ID` | ID del número de WhatsApp desde el que se envía. |
| `META_APP_SECRET` | Valida la firma `X-Hub-Signature-256` de cada webhook. |
| `DATABASE_URL` | Conexión a Postgres. |
| `OWNER_PHONE` | Número de la dueña; recibe las notificaciones de escalado. |
| `GEMINI_API_KEY` | API key de Gemini. |

`.env.example` tiene la plantilla. En local, `config.py` carga `.env` con
python-dotenv.

### Local

```powershell
py -m pip install -r requirements.txt
copy .env.example .env    # rellenar los valores
py -m uvicorn --app-dir src main:app --reload --port 8000
```

Endpoints:

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/` | Health check. |
| GET | `/webhook` | Verificación de Meta (`hub.mode`, `hub.verify_token`, `hub.challenge`). |
| POST | `/webhook` | Recepción de mensajes. Solo procesa el campo `messages`. |

Las tablas se crean solas con el primer mensaje entrante (ver *Base de datos
compartida*).

### Despliegue

`Procfile` y `railway.json` definen el mismo comando de arranque:
`uvicorn --app-dir src main:app --host 0.0.0.0 --port $PORT`. Railway construye con
Nixpacks y reinicia el servicio si falla. Las variables de la tabla anterior
se configuran en el servicio.

## Base de datos compartida

El bot comparte la base Neon con `kan-m-web`, que vive en otro repositorio.
Solo administra dos tablas propias:

- `wa_conversations`: una fila por teléfono con estado, contexto JSONB (incluye
  el historial reciente), contador de fallos y marcas de tiempo.
- `wa_processed_messages`: `message_id` ya procesados, para deduplicar. Las
  filas de más de 24 h se borran de forma oportunista (en torno al 2 % de los
  webhooks disparan la limpieza).

Las dos se crean con `CREATE TABLE IF NOT EXISTS` la primera vez que se abre
el pool de conexiones, no al arrancar el proceso. Todo el SQL del bot está en
`db.py` y `dedup.py`, y no toca ninguna otra tabla.

Por qué importa la separación:

- Cada proyecto es dueño de su esquema. El bot se puede desplegar, pausar o
  retirar sin migraciones en la web, y viceversa.
- El prefijo `wa_` evita colisiones de nombres.
- Contrapartida: el usuario de `DATABASE_URL` necesita permiso para crear
  tablas, y cualquier herramienta de migraciones de la web tiene que ignorar
  las tablas `wa_*` en lugar de tratarlas como sobrantes.

## Tests

```powershell
py -m pip install -r requirements-dev.txt
py -m pytest tests/
```

`tests/test_intent.py` prueba solo `intent.py`, sin red, sin base de datos y
sin importar `config.py`. Cubre:

- una tabla de frases reales por intención (cotizar, pedido, delivery,
  horario, ubicación, humano y frases sin intención), con variantes sin tildes,
  con errores de escritura y con seseo;
- casos de regresión marcados en el código;
- que una pregunta de precio no ofrezca cotizar como segunda intención;
- `es_saludo_o_menu`;
- un caso que fija una limitación aceptada, para detectar si alguien cambia el
  orden de prioridades.

Por qué ahí: `intent.py` es lógica pura, sin dependencias del resto del
proyecto, y es donde viven las decisiones frágiles (listas de palabras clave y
prioridades).

Los casos de regresión cubren errores reales de clasificación que se
encontraron auditando el código y ya están corregidos:

- "cuál es su horario de atención" escalaba a humano;
- "quiero un bizcocho personalizado" se trataba como pedido y no como
  cotización;
- frases como "ya llegué al local" se tomaban como saludo.

Al escribir la tabla de frases, la propia suite encontró tres frases más mal
clasificadas, que también se corrigieron en el código: "quiero hablar con una
persona", "kiero un bizcocho personalisado" y "como hago un pedido".

`db.py`, `dedup.py`, `meta_client.py` y `ai_client.py` son SQL y clientes de
servicios externos. Probarlos con mocks verificaría que se llama a una
función, no que el sistema se comporte bien; eso sería ceremonia.
`state_machine.py` es distinto: orquesta esos servicios, pero tiene lógica de
estados con errores conocidos que se podrían cubrir con dobles en memoria. Esos
tests no existen todavía.

`tests/smoke.py` es un script anterior sin framework (`py tests/smoke.py`). Sus
comprobaciones de `config.py` se omiten si faltan las variables de entorno.

## Limitaciones conocidas

**El webhook confirma antes de procesar.**
- Decisión: responder 200 a Meta de inmediato y procesar en segundo plano, para
  que Meta no reintente por timeout.
- Coste: Meta ya no reintenta. Si la base de datos o cualquier paso falla
  durante el procesamiento, ese mensaje se pierde y el cliente no recibe
  respuesta.
- Para resolverlo: persistir el mensaje entrante antes de confirmar y
  procesarlo desde una cola con reintentos.

**Los fallos de envío a Meta son silenciosos.**
- Decisión: el cliente HTTP hace hasta dos intentos (no reintenta errores 4xx
  salvo 429) y, si falla, registra el error y devuelve `None` sin lanzar
  excepción.
- Coste: si falla la notificación a la dueña, el chat queda escalado y en
  silencio y nadie se entera.
- Para resolverlo: propagar el fallo a quien llama y tener un canal de alerta
  independiente de WhatsApp.

**La notificación a la dueña es texto libre.**
- Decisión: se envía como mensaje de texto normal, no como plantilla.
- Coste: Meta solo acepta texto libre dentro de la ventana de 24 h desde el
  último mensaje de esa persona al número del negocio. Fuera de ella, la
  notificación falla, y por la limitación anterior el fallo no se ve.
- Para resolverlo: usar una plantilla aprobada por Meta para las
  notificaciones.

**El lock por teléfono vive en memoria.**
- Decisión: un `asyncio.Lock` por teléfono dentro del proceso.
- Coste: solo protege con una instancia. Con varias réplicas o workers, dos
  mensajes seguidos del mismo cliente pueden procesarse a la vez. Además, el
  diccionario de locks se vacía al superar 1000 entradas.
- Para resolverlo: un lock en Postgres (por ejemplo, advisory locks por
  teléfono) o garantizar una sola instancia.

**Un fallo de Gemini se trata como "no entendí".**
- Decisión: el cliente de Gemini captura cualquier excepción y devuelve `None`.
- Coste: una API key inválida o una cuota agotada se ve igual que un mensaje no
  reconocido. El cliente recibe el fallback y la dueña no recibe aviso.
- Para resolverlo: distinguir "sin respuesta" de "error" y alertar en el
  segundo caso.

**"Atención al cliente" gana como humano.**
- Decisión: la intención `humano` se evalúa antes que `horario`.
- Coste: "horario de atención al cliente" escala a una persona.
- Para resolverlo: reordenar prioridades o detectar frases compuestas. No se
  hace por este caso, y un test fija el comportamiento actual.

**La base de conocimiento se lee una vez.**
- Decisión: `data/knowledge_base.txt` se carga al importar el módulo.
- Coste: editarla no tiene efecto hasta reiniciar o redesplegar.
- Para resolverlo: recargarla con un TTL o leerla desde la base de datos.

## Estado del proyecto

El servicio está pausado en Railway. El repositorio se mantiene hoy como pieza
de portafolio.

## Licencia

Todos los derechos reservados; ver [LICENSE](LICENSE). Es software desarrollado
para un cliente real, así que el código se publica solo para consulta y evaluación.
