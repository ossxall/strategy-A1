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

## Visor de backtest live

El bot actualiza el visor mensual al cambiar una vela de estrategia. Por defecto
escribe `data/backtest_out/html/backtest.html` y los datos JSON en
`data/backtest_out/html/data/`. En Docker Compose, `BACKTEST_REPORT_DIR` apunta
a `/data/backtest_out`, dentro del volumen persistente. Se puede cambiar con esa
misma variable de entorno.

`backtest.html` es una plantilla estática compartida con `strategy/report.py`;
no contiene resultados. Consume `data/manifest.json` y los JSON mensuales. Para
verlo, hay que servir la carpeta por HTTP (por ejemplo, `python -m http.server`
desde el directorio `html`); abrir el archivo directamente con `file://` bloquea
la carga de esos datos en algunos navegadores.

## Salud HTTP

`GET /live/bot/health` y su alias `GET /live/bot/status` devuelven el estado
del proceso, los ciclos recientes, la frescura de las velas, la disponibilidad
de `signal.db`, los umbrales y los checks calculados. También se conservan
`/health` y `/status` como alias cortos. La respuesta es JSON; devuelve HTTP
200 cuando está sano y 503 cuando está degradado o no sano.

El servidor usa concurrencia limitada y solo lee una instantánea en memoria.
El sondeo de SQLite ocurre en un hilo aparte con timeout, por lo que las
peticiones no esperan a consultas ni al ciclo de señales. En Compose está
publicado en `http://127.0.0.1:8080/live/bot/health`.
