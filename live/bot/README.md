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
- `__main__.py`: entrypoint sin opciones de CLI.

La configuración operativa se toma de `live/.env` y de las variables de
entorno del contenedor. No se admiten argumentos de terminal.

## Reinicio y recuperación

`LIVE_ONLY` está activado por defecto. Deja `LIVE_SINCE` vacío para que el bot
guarde su marca inicial en `SIGNAL_DB` y la reutilice tras reiniciarse. Conserva
esa base de datos: en Docker Compose vive en el volumen persistente `bot_data`.
No elimines el volumen al recrear los contenedores.

Tras una caída, el bot vuelve a recorrer el feed y puede insertar eventos no
procesados desde esa marca; la base de datos evita duplicados. `LIVE_ONLY` no es
un filtro de antigüedad para el ejecutor: cualquier worker que actúe sobre las
señales debe validar su vigencia e idempotencia antes de operar.

## Contrato de señales para consumidores

`signals` es un outbox inmutable de intenciones de estrategia, no un registro de
órdenes ni de posiciones reales. Cada fila incluye `schema_version`, `signal_id`,
`event`, `action`, `side`, `position_id`, `scheduled`, `signal_time` y el
`payload` detallado. `signal_id` es un SHA-256 determinista v2; `position_id`
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

`signal_execution` prepara estado separado por `(account_id, signal_id)` con
reclamo/lease, intentos y referencias de orden. El productor nunca crea ni
reinicia ese estado. El futuro consumidor debe registrar `READY` por cuenta al
descubrir una señal y reclamarla en una transacción,
usar un `client_order_id` determinista compatible con Binance (no el `signal_id`
completo) y reconciliar órdenes/posiciones con la cuenta antes de reintentar.
La ausencia de una fila en `signal_execution` significa que esa cuenta aún no
registró la señal.
La columna `executed` se conserva por compatibilidad, pero no es el acuse
autoritativo. Este paquete todavía no incluye ese consumidor.
Al migrar a v2 se recalculan los `signal_id` existentes para incorporar la acción
y `position_id`; se conservan las filas, pero no se deben mantener referencias
externas a los IDs de la versión anterior.

## Visor de backtest live

El bot actualiza el visor mensual al cambiar una vela de estrategia. Escribe
`live/bot/data/backtest_out/html/backtest.html` y los datos JSON en
`live/bot/data/backtest_out/html/data/`. En Docker Compose, `BACKTEST_REPORT_DIR`
apunta a `/app/legacy/bot1/bot/data/backtest_out`, persistido en el volumen
`blacker-bot1-reports`. Se puede cambiar con esa misma variable de entorno.

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

`signal.db` usa WAL con comprobación del modo, `synchronous=FULL` para
durabilidad de señales y estado de recuperación, y un `busy_timeout` de 5 s.
El autocheckpoint se configura en 1000 páginas y el límite de tamaño de WAL de
64 MiB es orientativo, no un tope duro.

La configuración común de conexiones está en `live/db/sqlite_config.py` y se
reaplica al abrir cada base. Borrar `signal.db` no afecta a la configuración del
código, pero sí elimina las señales, la deduplicación y `live_since_ms`; detén
los servicios antes de borrar archivos SQLite.
