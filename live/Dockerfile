# El contexto de build es la raíz del repositorio para incluir live/ y strategy/.
FROM python:3.12.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# Dependencias del bot y del colector.
COPY live/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Lanzador del servicio, paquete del bot y paquete compartido de estrategia.
COPY live/ /app/
COPY strategy/ /app/strategy/

# Limpieza de artefactos locales que no deben ir a la imagen.
RUN rm -rf \
        /app/__pycache__ /app/.ruff_cache \
        /app/feed/__pycache__ \
        /app/bot/__pycache__ \
        /app/bot/execution/__pycache__ \
        /app/strategy/__pycache__ /app/strategy/.ruff_cache \
    && mkdir -p /data

WORKDIR /app

# Por defecto ejecuta el recolector; el compose sobreescribe el comando del bot.
CMD ["python", "-m", "feed"]
