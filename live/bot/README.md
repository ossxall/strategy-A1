# Paquete de señales live

`live/bot/` contiene la aplicación live del generador de señales. Docker inicia
el paquete desde el directorio live con `python -m bot`; no hay opciones CLI.

## Módulos

- `config.py`: defaults, lectura de `.env` y configuración de `strategy.Config`.
- `storage.py`: persistencia idempotente en `signal.db` y lectura de velas
  cerradas de `feed.db`.
- `series.py`: remuestreo y construcción de series para diagnóstico.
- `events.py`: conversión de la ejecución del backtester a eventos live.
- `health.py`: endpoints JSON de salud y sondeo en segundo plano de `signal.db`.
- `app.py`: ciclo de polling y coordinación de los componentes.
- `execution/worker.py`: consumo y lógica de negocio compartida para las señales.
- `execution/test_mode.py`: secuencia y recuperación del modo de prueba; envía
  señales sintéticas por el mismo `run_once()` del worker.
- `execution/test_mode_policy.py`: sizing y límites de seguridad propios del
  modo de prueba.
- `execution/selftest.py`: escenarios offline con gateway simulado.
- `execution/binance_usdm.py`: adaptador del SDK oficial de Binance.
- `executor.py`: ruta de compatibilidad del worker para consumidores existentes.
- `__main__.py`: entrypoint sin opciones de CLI.

La configuración operativa se toma de `live/.env` y de las variables de
entorno del contenedor. No se admiten argumentos de terminal.

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
`payload` detallado. Los `ENTRY` incluyen en `payload.sizing` los multiplicadores
efectivos de `strategy`, el stop y el leverage simulado. `signal_id` es un
SHA-256 determinista v2; `position_id`
solo correlaciona eventos del backtest y no identifica una posición de Binance.
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
El executor solo acepta señales v3 con `payload.sizing`; eventos legacy sin ese
contrato quedan rechazados y no generan órdenes.

## Executor USDⓈ-M Futures

Compose ejecuta `python -m bot.execution`, pero **no envía órdenes por defecto**:
`BOT_BINANCE_TRADING_ENABLED=false`. Usa el SDK oficial
`binance-sdk-derivatives-trading-usds-futures`; el entorno predeterminado es
testnet. Producción requiere `BOT_BINANCE_ENVIRONMENT=production`,
`BOT_BINANCE_TRADING_ENABLED=true` y `BOT_BINANCE_ALLOW_LIVE_TRADING=true`.
El tope de leverage predeterminado es 10x. `BOT_BINANCE_SET_LEVERAGE=true` aplica
en Binance el menor valor entre ese tope y el de `strategy`, pero no tiene efecto
mientras el trading siga deshabilitado. El tamaño puede ser menor que 10x por
`risk_pct`, los multiplicadores adaptativos y el límite de exposición.
`BOT_STRATEGY_INITIAL_CAPITAL=100` es la referencia del backtest; el worker lee el
equity/margen real de Binance para calcular la cantidad live.
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
protección de posiciones no rastreadas y rechazo de Hedge Mode. Esa matriz no
llama a Binance. El ciclo real comprueba además la comunicación con Binance; no
fuerza allí fallos de red ni el disparo de cada posible error API.

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

La lógica y los multiplicadores siguen saliendo del mismo `Backtester` que el
backtest; el worker no los recalcula. El capital y el equity del sizing live
provienen de Binance. Las diferencias reales de fill/PnL no se retroalimentan al
historial adaptativo simulado, por lo que no se prometen resultados idénticos al
backtest. Una divergencia de posición real detiene la ejecución automática.

Para habilitar, comienza con claves de testnet y
`BOT_BINANCE_TRADING_ENABLED=true`. No habilites producción hasta validar fills,
órdenes reduce-only, stops y recuperación de reinicios en testnet. Usa claves
sin permisos de retiro y restringidas por IP.

## Visor de backtest live

El bot actualiza el visor mensual al cambiar una vela de estrategia. Escribe
`live/bot/data/backtest_out/html/backtest.html` y los datos JSON en
`live/bot/data/backtest_out/html/data/`. En Docker Compose, `BOT_BACKTEST_REPORT_DIR`
apunta a `/app/legacy/bot1/bot/data/backtest_out`, persistido en el volumen
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

La configuración común de conexiones está en `live/db/sqlite_config.py` y se
reaplica al abrir cada base. Borrar `signal.db` no afecta a la configuración del
código, pero sí elimina las señales, la deduplicación y `live_since_ms`; detén
los servicios antes de borrar archivos SQLite.
