# Paquete de señales live

`live/bot/` contiene la aplicación live del generador de señales. Docker inicia
el paquete desde el directorio live con `python -m bot`; no hay opciones CLI.

## Módulos

- `config.py`: defaults, lectura de `.env` y configuración de `strategy.Config`.
- `storage.py`: persistencia idempotente en `signal.db` y lectura de velas
  cerradas de `feed.db`.
- `series.py`: remuestreo y construcción de series para diagnóstico.
- `events.py`: conversión de la ejecución del backtester a eventos live.
- `app.py`: ciclo de polling y coordinación de los componentes.
- `__main__.py`: entrypoint sin opciones de CLI.

La configuración operativa se toma de `live/.env` y de las variables de
entorno del contenedor. No se admiten argumentos de terminal.
