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
- `app.py` / `cli.py`: ciclo de vida y puntos de entrada.

## Ejecución

Desde el directorio `live/`:

```bash
python -m feed
```

`live/feed.py` se conserva como launcher compatible. La configuración se carga
desde el `.env` de la raíz del repositorio; las variables exportadas en el
proceso tienen prioridad.

Los endpoints locales son `http://127.0.0.1:8787/live/feed/health` y
`http://127.0.0.1:8787/live/feed/status`. Las rutas anteriores `/health` y
`/status` se conservan como alias. En Docker Compose el puerto solo se publica
en la interfaz local del host.
