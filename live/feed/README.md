# Feed collector

Paquete del colector OHLCV de Binance USDⓈ-M Futures.

## Módulos

- `config.py`: configuración `FEED_*` y utilidades de fechas/intervalos.
- `state.py`: estado compartido y sincronización entre trabajadores.
- `candles.py`: validación y normalización de velas.
- `database.py`: persistencia SQLite y comprobaciones de salud de la base.
- `rest.py`: backfill, descarga REST y reparación de huecos.
- `websocket.py`: flujo en tiempo real, watchdog y reconexión.
- `status.py`: endpoints JSON `/live/feed/health` y `/live/feed/status`.
- `chart.py`: gráfico Plotly con las últimas 100 velas OHLCV.
- `app.py` / `cli.py`: ciclo de vida y puntos de entrada.

## SQLite

`live/db/sqlite_config.py` centraliza la apertura de todas las bases SQLite de
`/live`, incluidas las conexiones de solo lectura. `feed.db` usa WAL y comprueba
al iniciar que SQLite haya devuelto `wal`. Las conexiones del colector usan
`synchronous=NORMAL` porque las velas pueden recuperarse desde REST, un
`busy_timeout` de 5 s, claves foráneas activadas, autocheckpoint cada 1000
páginas y un objetivo de truncado del WAL de 64 MiB. El límite del WAL no es
duro y depende de que el checkpoint pueda completarse.

Al borrar y recrear `feed.db`, el colector vuelve a aplicar esta política al
iniciar. El archivo eliminado también contenía las velas persistidas.

## Ejecución

Desde el directorio `live/`:

```bash
python -m feed
```

`live/feed.py` se conserva como launcher compatible. La configuración se carga
desde el `.env` de la raíz del repositorio; las variables exportadas en el
proceso tienen prioridad.

Los endpoints locales son `http://127.0.0.1:8787/live/feed/health` y
`http://127.0.0.1:8787/live/feed/status`. El gráfico interactivo se sirve en
`http://127.0.0.1:8787/live/feed/chart`. Las rutas anteriores `/health` y
`/status` se conservan como alias. En Docker Compose el puerto solo se publica
en la interfaz local del host.
