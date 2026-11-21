# Paquete de señales live

`live/bot/` contiene la aplicación live del generador de señales. Docker inicia
el paquete desde el directorio live con `python -m bot`; no hay opciones CLI.

## Módulos

- `config.py`: defaults, carga del perfil YAML y configuración de `strategy.Config`.
- `storage.py`: persistencia idempotente en `signal.db` y lectura de velas
  cerradas de `feed.db`.
- `series.py`: remuestreo y construcción de series para diagnóstico.
- `events.py`: conversión de la ejecución del backtester a eventos live.
- `health.py`: endpoints JSON de salud y sondeo en segundo plano de `signal.db`.
- `app.py`: ciclo de polling y coordinación de los componentes.
- `execution/worker.py`: consumo y lógica de negocio compartida para las señales.
- `execution/status.py`: endpoint JSON del executor, servido desde una
  instantánea en memoria.
- `execution/test_mode.py`: secuencia y recuperación del modo de prueba; envía
  señales sintéticas por el mismo `run_once()` del worker.
- `execution/test_mode_policy.py`: sizing y límites de seguridad propios del
  modo de prueba.
- `execution/selftest.py`: escenarios offline con gateway simulado.
- `execution/binance_usdm.py`: adaptador del SDK oficial de Binance.
- `executor.py`: ruta de compatibilidad del worker para consumidores existentes.
- `__main__.py`: entrypoint sin opciones de CLI.

`config.dev.yaml` contiene la base no secreta; `APP_ENV=dev` la usa directamente
y `APP_ENV=prod` aplica encima solo los cambios de `config.prod.yaml`. Las
variables ya exportadas tienen prioridad. Los secretos de cada perfil se cargan
por separado desde `.env.dev` o `.env.prod`. Al ejecutar directamente
`python live/bot.py`, `python -m bot` o
`python live/bot/executor.py`, el perfil también se carga antes de iniciar el
proceso. No se admiten argumentos de terminal.

## Reinicio y recuperación

`LIVE_ONLY` está activado por defecto. Deja `LIVE_SINCE` vacío para que el bot
guarde su marca inicial en `SIGNAL_DB` y la reutilice tras reiniciarse. Conserva
esa base de datos: en Docker Compose los datos persistentes se guardan en
`live/data` del host (montado como `/data` dentro de los contenedores). No
elimines ese directorio al recrear los contenedores.
Las bases temporales de las autopruebas offline del executor también se crean
allí y se eliminan al terminar.
Las instalaciones anteriores pueden conservar datos en el volumen Docker
`blacker-bot1-data`: Compose ya no lo monta ni lo elimina automáticamente.
Antes de volver a iniciar una instalación existente, detén sus servicios y
migra los archivos necesarios a `live/data`; no sobrescribas bases existentes
sin compararlas.

Tras una caída, el bot vuelve a recorrer el feed y puede insertar eventos no
procesados desde esa marca; la base de datos evita duplicados. `LIVE_ONLY` no es
un filtro de antigüedad para el ejecutor: cualquier worker que actúe sobre las
señales debe validar su vigencia e idempotencia antes de operar.

## Contrato de señales para consumidores

`signals` es un outbox inmutable de intenciones de estrategia, no un registro de
órdenes ni de posiciones reales. Cada fila incluye `schema_version`, `signal_id`,
`event`, `action`, `side`, `position_id`, `scheduled`, `signal_time` y el
`payload` detallado. Los `ENTRY` incluyen en `payload.sizing` el riesgo base,
el stop y los costes usados por `strategy`; la estrategia no calcula ni transmite
leverage.
`signal_id` es un SHA-256 determinista v2; `position_id` solo correlaciona eventos
del backtest y no identifica una posición de Binance.
`signal` y `side` se conservan iguales por compatibilidad; en un `EXIT`, `side`
indica la posición que se cierra, no el lado de la orden de Binance.

`scheduled=1` significa que el evento se publicó antes de `signal_time` para la
siguiente apertura; no es el estado de consumo. La fila conserva el contenido
original aunque el bot vuelva a calcular ese evento cuando la vela siguiente
cierra. `signal_time` es el instante de acción, `bar_time` la vela que la originó.
Consúmase en orden por `signal_time`, `bar_time` e `id`; el productor inserta
primero las salidas cuando una reversión produce `EXIT` y `ENTRY` simultáneos.
Los precios son referencias del backtest; los precios y PnL de salidas intrabar
marcados como simulados no son fills de Binance. El consumidor debe validar
frescura, cuenta, tamaño y posición real; no debe ejecutar eventos atrasados a
ciegas.

`signal_execution` mantiene estado separado por `(account_id, signal_id)`, con
reclamo/lease, intentos, fills y referencias de orden. El productor nunca crea
ni reinicia ese estado. El executor reclama atómicamente, usa un
`client_order_id` determinista compatible con Binance (no el `signal_id`
completo) y reconcilia órdenes/posiciones antes de reintentar.
La columna `executed` se conserva por compatibilidad, pero no es el acuse
autoritativo. Las migraciones conservan filas antiguas y recalculan IDs cuando
es necesario.
El productor emite señales v4. En cada `EXIT`, `payload.reason_code` identifica
la familia de salida y `payload.stop_action` declara `MOVE_TO_BREAKEVEN`,
`KEEP` o `CANCEL`; `reason` queda como descripción legible. El executor acepta
señales v3 heredadas mediante una traducción de compatibilidad, además de v4.
Señales anteriores a v3 quedan rechazadas.

Los códigos de entrada son `ENTRY_CROSS`/`ENTRY_OTHER`; los de salida:
`TAKE_PROFIT_1`…`TAKE_PROFIT_10`, `STOP_LOSS`,
`BREAKEVEN`, `EMA_CROSS_EXIT`, `ADX_REVERSAL`,
`RSI_DIVERGENCE_BEAR/BULL`, `GAUSSIAN_TREND_BEAR/BULL`,
`MONTHLY_LOSS_LIMIT`, `TEST_PARTIAL`, `TEST_CLOSE` y `EXIT_OTHER`. `CLOSE`
siempre declara `CANCEL`; `PARTIAL_CLOSE` declara `MOVE_TO_BREAKEVEN` solo para
TP1 cuando `payload.sizing.move_stop_to_breakeven=true`; el resto declara
`KEEP`. La cantidad se determina por `action` y `payload.close_pct`, no por
`reason`, `price` ni el PnL simulado.

## Executor USDⓈ-M Futures

Compose ejecuta `python -m bot.execution`. El perfil dev usa testnet y mantiene
`BOT_BINANCE_TRADING_ENABLED=false`; `config.prod.yaml` selecciona production y
habilita explícitamente `BOT_BINANCE_TRADING_ENABLED=true` y
`BOT_BINANCE_ALLOW_LIVE_TRADING=true`, por lo que puede enviar órdenes reales
con credenciales mainnet válidas. Usa el SDK oficial
`binance-sdk-derivatives-trading-usds-futures`.
El executor es el único componente que configura el leverage en Binance:
`BOT_BINANCE_LEVERAGE` fija el valor objetivo (50x por defecto) y se aplica
antes de dimensionar cada entrada. En el modo de prueba interno se fuerza 10x.
Estos ajustes no tienen efecto mientras el trading siga deshabilitado. El
presupuesto de riesgo se calcula con el
`risk_pct` de la señal; la cantidad disponible también depende del saldo y el
leverage de Binance y debe respetar sus filtros.
La estrategia live usa exclusivamente los valores definidos en
`strategy/config.py`; las variables `BOT_STRATEGY_*` del proceso no los modifican.
El `initial_capital` hardcodeado sirve para simulaciones; el worker lee el saldo
disponible real de Binance para calcular la cantidad live.
El executor fija un corte inicial por cuenta con un margen corto de recuperación
(`BOT_EXECUTOR_INITIAL_BACKFILL_SECONDS`) para no ejecutar el histórico al activarlo.

`BOT_EXECUTOR_TEST_MODE=true` ejecuta ciclos reales de prueba en testnet y
requiere también `BOT_BINANCE_TRADING_ENABLED=true`. Test mode rechaza
`BOT_BINANCE_ENVIRONMENT=production` incluso si se habilitó
`BOT_BINANCE_ALLOW_LIVE_TRADING=true`; nunca envía órdenes de prueba a mainnet.
Fuerza `BTCUSDT`, leverage máximo 10x y stop
al 1%. Interpreta el límite de 11 USDT como margen estimado por orden (hasta
110 USDT de notional a 10x), no como notional ni como límite de pérdidas o
comisiones. Alterna ciclos LONG/SHORT de entrada, cierre parcial del 50% y
cierre total: el ciclo LONG prueba el parcial TP y el movimiento del stop a
breakeven cuando el mark lo permite; el SHORT prueba un parcial no-TP que
conserva el stop original. Espera 10 s entre ciclos y se detiene tras completar
ambos lados, antes de repetir el patrón. Usa
una base de señales y un ledger separados (por ejemplo,
`data/signal.executor-test.db`). Cada señal sintética se persiste allí y se
reclama/procesa con el mismo `ExecutionWorker.run_once()` que usa el flujo normal;
así, las órdenes de prueba pasan por las validaciones, el sizing, el registro de
intención, la ejecución y la reconciliación habituales. Se niega a empezar si
BTCUSDT no está flat o tiene órdenes abiertas. Si el stop protector cierra la
posición durante el ciclo, reconcilia el fill y se detiene antes de abrir otra.
Ante discrepancias o errores no reconciliables, también se detiene.
Los filtros del exchange deben permitir un cierre parcial dentro del tope; si
no, no envía órdenes. Antes del primer ciclo real corre una matriz offline con
gateway simulado que cubre LONG/SHORT, parciales TP y no-TP, breakeven
favorable/no favorable/deshabilitado, cierre total, señales programadas que
esperan su `signal_time`, parcial programado y reversión `EXIT`→`ENTRY` en orden,
fill/reconciliación de stop, recuperación idempotente, señales vencidas,
protección de posiciones no rastreadas, bloqueo por órdenes externas abiertas,
restauración de stops ausentes, reintento seguro antes del envío, conservación
de fills parciales con orden terminal, rechazo de Hedge Mode y cinco reinicios
simulados: PREPARED antes del envío, ENTRY llenada antes de guardar el fill,
parcial antes de breakeven, cierre antes de cancelar stops y stop disparado
mientras el worker está abajo. Se reabre la misma DB y se conserva el gateway
simulado para comprobar que no se dupliquen órdenes. Esa matriz no llama a
Binance ni mata un proceso real. El ciclo real comprueba además la comunicación
con Binance; no fuerza allí fallos de red ni cada posible error API.

Opcionalmente, `BOT_EXECUTOR_PRODUCTION_AUDIT=true` añade una auditoría de
producción **solo lectura** antes de los ciclos testnet. Requiere credenciales
separadas (`BOT_BINANCE_PRODUCTION_AUDIT_API_KEY` y
`BOT_BINANCE_PRODUCTION_AUDIT_API_SECRET`) limitadas a lectura. Comprueba modo
One-way, filtros/mark de BTCUSDT, saldo disponible, posición flat, ausencia de
órdenes abiertas y que el tamaño mínimo de prueba quepa en el tope. No coloca,
cancela ni modifica órdenes en producción; si falla, aborta antes de enviar
órdenes testnet. Esta auditoría no certifica permisos de trading ni fills en
mainnet: esos comportamientos se validan en testnet.

El executor solo admite One-way Mode y el símbolo configurado en `SYMBOL`.
Calcula cantidad con `payload.sizing`, equity/margen reales, precio mark,
`stepSize`, `minQty` y filtros de notional. Usa órdenes MARKET y salidas
`reduceOnly`; `ENTRY` instala un stop condicional y TP1 puede moverlo a
breakeven. Incertidumbres de orden o discrepancias de posición dejan la señal en
`RECONCILIATION_REQUIRED` y bloquean nuevas ejecuciones. Las entradas vencidas
se marcan `EXPIRED`; las salidas atrasadas se contrastan con la posición real.
Las órdenes MARKET abiertas no reconciliadas bloquean nuevas señales. Si una
posición del ledger no tiene stop activo, el worker intenta restaurar el stop
original y se bloquea si no puede confirmarlo. Un error antes de persistir la
intención de orden vuelve la señal a `READY`; una respuesta incierta después de
preparar/enviar la orden no se reintenta a ciegas.
Si una orden termina cancelada con fills parciales, el fill queda contabilizado
y la ejecución pasa a reconciliación obligatoria; no se reenvía el remanente
automáticamente.

Las señales y los parámetros fijos de riesgo salen del mismo `Backtester` que el
backtest; el worker aplica el `risk_pct` y el stop al equity real de Binance. Las
diferencias reales de fill/PnL no se retroalimentan a la simulación, por lo que no
se prometen resultados idénticos al backtest. Una divergencia de posición real
detiene la ejecución automática.

Para habilitar, comienza con claves de testnet y
`BOT_BINANCE_TRADING_ENABLED=true`. No habilites producción hasta validar fills,
órdenes reduce-only, stops y recuperación de reinicios en testnet. Usa claves
sin permisos de retiro y restringidas por IP.

Para una prueba manual del flujo normal, instala `live/requirements.txt` y
ejecuta el executor con el perfil `dev`. Desde otra terminal, ejecuta:

```sh
python live/bot/scripts/test_execution_signal.py --confirm
```

También se puede ejecutar `python scripts/test_execution_signal.py --confirm`
desde `live/bot/`. Comprueba que el executor esté sano, la cuenta testnet plana
y sin órdenes, y usa el sizing de estrategia configurado. Después inserta una
ENTRY, espera el fill y el stop protector, mantiene la posición unos segundos
y crea una señal EXIT `reduceOnly` por el mismo flujo normal. Espera la
confirmación de cierre y de cuenta plana; no cierres manualmente esta posición.
Si un paso requiere reconciliación, el script se detiene y muestra el estado
en vez de abrir otro ciclo.

### Prueba determinista feed → bot → execution

Para validar también el generador real de señales, en vez de insertar la ENTRY
manualmente, ejecuta:

```sh
python live/bot/scripts/test_feed_bot_execution.py --confirm
```

Usa exclusivamente `APP_ENV=dev`, credenciales de Binance testnet,
`BOT_BINANCE_ENVIRONMENT=testnet`, `BOT_BINANCE_TRADING_ENABLED=true` y
`BOT_EXECUTOR_TEST_MODE=false`. El runner crea bases aisladas, escribe velas
cerradas de fixture mediante la persistencia del feed, las lee con `FeedReader`,
y ejecuta `SignalBot.process_once()` y el `ExecutionWorker` normal contra la API
real de testnet. La fixture provoca un cruce LONG y luego un EXIT por stop; se
verifican los fills y que la cuenta y el ledger queden flat. Además, inserta en
el ledger aislado señales contractuales de prueba para comprobar ENTRY SHORT y
EXIT parciales en LONG/SHORT, junto con cierres completos. El parcial LONG es
TP1 con solicitud de breakeven: se comprueba que el executor mueva el stop a
entrada si el mark price es favorable, o que conserve el stop original si el
breakeven lo dispararía inmediatamente. No se inicia el WebSocket ni se prueban
la conexión de mercado ni el proceso de despliegue.
La estrategia usa parámetros cortos de fixture para forzar señales sin esperar
un cruce de producción; se valida la implementación del `SignalBot`, no el
rendimiento ni los filtros de la estrategia operativa.

El runner exige `--confirm`, una cuenta testnet plana, utiliza BTCUSDT con
leverage objetivo 1x y configura el sizing de la señal con un objetivo teórico
de riesgo del 1% del balance disponible, antes de comisiones,
deslizamiento o gaps (la exposición objetivo es aproximadamente 20%, sujeta a
filtros y saldo de Binance; el saldo debe cubrir el notional mínimo del símbolo).
Cada ejecución usa un `account_id` y un ledger únicos en
`live/data/feed_bot_execution/`; no comparte ni modifica las bases del
bot/executor normal. Los archivos se conservan para auditoría o reconciliación,
especialmente si la prueba falla.

## Visor de backtest live

El bot actualiza el visor mensual al cambiar una vela de estrategia. Escribe
`live/bot/data/backtest_out/html/backtest.html` y los datos JSON en
`live/bot/data/backtest_out/html/data/`. En Docker Compose, `BOT_BACKTEST_REPORT_DIR`
apunta a `/app/bot/data/backtest_out`, persistido en el volumen
`blacker-bot1-reports`. Se puede cambiar con `BOT_BACKTEST_REPORT_DIR`.

`backtest.html` es una plantilla estática compartida con `strategy/report.py`;
no contiene resultados. Consume `data/manifest.json` y los JSON mensuales. Para
verlo, usa `http://127.0.0.1:8080/live/bot/backtest/`; el endpoint también sirve
los archivos JSON solicitados por la página. Abrir el HTML directamente con
`file://` bloquea la carga de esos datos en algunos navegadores.

## Salud HTTP

`GET /live/bot/health` y su alias `GET /live/bot/status` devuelven el estado
del proceso, los ciclos recientes, la frescura de las velas, la disponibilidad
de `signal.db`, los umbrales y los checks calculados. También se conservan
`/health` y `/status` como alias cortos. La respuesta es JSON; devuelve HTTP
200 cuando está sano y 503 cuando está degradado o no sano.

`GET /live/bot/backtest/` sirve el visor estático live y los archivos de datos
bajo el directorio generado del reporte.

## Modo de backtest histórico

`BOT_MODE=backtest` hace una simulación completa con todas las velas cerradas que
existan en `feed.db` para `BOT_SYMBOL` y `BOT_INTERVAL`. Aplica la configuración
actual de `strategy.Config`, genera métricas, CSV/JSON y el dashboard resumen en
el directorio persistente del reporte. El informe se sirve en
`/live/bot/report/`; el visor mensual continúa en `/live/bot/backtest/`.

El modo ejecuta el backtest una vez al arrancar y mantiene el proceso HTTP vivo.
Si `feed.db` todavía no existe, está vacío o no tiene suficientes velas, espera
y vuelve a intentarlo. No inserta ni modifica señales en `signal.db`; no es un
modo de trading ni un reemplazo del modo `live`. `BOT_RUN_ONCE` no termina este
modo, para que los reportes sigan disponibles por HTTP.

`strategy/report.html` es la plantilla fuente y no se sobrescribe. El generador
la copia como `report.html` (y como `index.html` por compatibilidad) junto a la
carpeta configurada en `BOT_BACKTEST_REPORT_DIR`. Con el valor predeterminado
`backtest_out`, la página resumen carga los datos de esa carpeta. En Compose,
los resultados permanecen en el volumen `bot_reports`.

Antes de cambiar un despliegue al modo histórico, confirma que el executor no
tenga señales live pendientes que deban procesarse. Cambia el modo con
`BOT_MODE=backtest` en el perfil `.env` que usa Compose; vuelve a `BOT_MODE=live`
para reanudar la generación normal de señales.

## Estado HTTP del executor

El proceso executor publica `GET /live/bot/execution/status`; el alias es
`GET /live/bot/execution/health`. Devuelve en JSON la configuración operativa
(sin credenciales), los ciclos y errores recientes, el estado de reconciliación,
la última señal y el último snapshot de cuenta. Responde HTTP 200 si está sano y
503 si está degradado o no sano.

El endpoint se atiende en un servidor separado, con concurrencia y timeout
limitados. Solo lee una instantánea en memoria: no consulta SQLite ni Binance,
por lo que no espera al ciclo de ejecución. Se configura con
`BOT_EXECUTOR_HTTP_HOST` (por defecto `127.0.0.1`),
`BOT_EXECUTOR_HTTP_PORT` (por defecto `8081`), `BOT_EXECUTOR_HTTP_MAX_CLIENTS`
y `BOT_EXECUTOR_HTTP_REQUEST_TIMEOUT`. En Compose está publicado en
`http://127.0.0.1:8081/live/bot/execution/status`.

El servidor usa concurrencia limitada y solo lee una instantánea en memoria.
El sondeo de SQLite ocurre en un hilo aparte con timeout, por lo que las
peticiones no esperan a consultas ni al ciclo de señales. En Compose está
publicado en `http://127.0.0.1:8080/live/bot/health`.

`signal.db`, el snapshot de cuenta y el ledger del executor comparten
`live/data` con el feed; el heartbeat del executor también se escribe allí.
`signal.db` usa WAL con comprobación del modo, `synchronous=FULL` para
durabilidad de señales y estado de recuperación, y un `busy_timeout` de 5 s.
El autocheckpoint se configura en 1000 páginas y el límite de tamaño de WAL de
64 MiB es orientativo, no un tope duro.

Al arrancar en modo live, el bot valida `feed.db` en solo lectura y
valida/migra de forma transaccional `signal.db`; el executor también valida el
esquema compartido de `signal.db` antes de consumir señales. Se comprueban
tablas, columnas, tipos, claves, índices, `user_version` de `signal.db` e
integridad SQLite. Solo se aceptan las migraciones históricas explícitamente
soportadas por el almacén. Si la estructura es desconocida, está dañada o es de
una versión futura, el proceso falla antes de publicar endpoints o procesar
órdenes. No se borran ni reinicializan bases automáticamente: conserva una copia
y revisa el diagnóstico de arranque.

La configuración común de conexiones está en `live/db/sqlite_config.py` y se
reaplica al abrir cada base. Borrar `signal.db` no afecta a la configuración del
código, pero sí elimina las señales, la deduplicación y `live_since_ms`; detén
los servicios antes de borrar archivos SQLite.
