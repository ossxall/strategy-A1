# El contexto de build es la raíz del repositorio para incluir live/ y strategy/.
FROM python:3.12.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependencias del bot y del colector.
COPY live/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app/legacy

# Lanzador del servicio, paquete del bot y paquete compartido de estrategia.
COPY live/ /app/legacy/bot1/
COPY strategy/ /app/legacy/strategy/

# Limpieza de artefactos locales que no deben ir a la imagen.
RUN rm -rf \
        /app/legacy/bot1/__pycache__ /app/legacy/bot1/.ruff_cache \
        /app/legacy/bot1/feed/__pycache__ \
        /app/legacy/bot1/bot/__pycache__ \
        /app/legacy/bot1/bot/execution/__pycache__ \
        /app/legacy/strategy/__pycache__ /app/legacy/strategy/.ruff_cache \
    && mkdir -p /data

WORKDIR /app/legacy/bot1

# Por defecto ejecuta el recolector; el compose sobreescribe el comando del bot.
CMD ["python", "-m", "feed"]
