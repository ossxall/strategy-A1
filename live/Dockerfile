# syntax no requerido: los named build contexts funcionan con BuildKit estándar.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependencias del bot y del colector.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app/legacy

# Código del bot y paquete compartido de estrategia.
COPY . /app/legacy/bot1/
COPY --from=strategy . /app/legacy/strategy/

# Limpieza de artefactos locales que no deben ir a la imagen.
RUN rm -rf \
        /app/legacy/bot1/__pycache__ /app/legacy/bot1/.ruff_cache \
        /app/legacy/bot1/feed/__pycache__ \
        /app/legacy/strategy/__pycache__ /app/legacy/strategy/.ruff_cache \
    && mkdir -p /data

WORKDIR /app/legacy/bot1

# Por defecto ejecuta el recolector; el compose sobreescribe el comando del bot.
CMD ["python", "-m", "feed"]
