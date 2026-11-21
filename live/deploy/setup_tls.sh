#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v docker-compose >/dev/null 2>&1; then
  echo "No se encontró docker-compose. Instala Docker Compose antes de continuar." >&2
  exit 1
fi

if [[ -z "${LETSENCRYPT_EMAIL:-}" ]]; then
  read -r -p "Correo para avisos de Let's Encrypt: " LETSENCRYPT_EMAIL
fi
if [[ -z "${LETSENCRYPT_EMAIL}" ]]; then
  echo "LETSENCRYPT_EMAIL es obligatorio para emitir el certificado." >&2
  exit 1
fi
export LETSENCRYPT_EMAIL

compose=(
  docker-compose
  --profile production
  -f docker-compose.yml
  -p blacker-bot1
)
bootstrap=(
  "${compose[@]}"
  -f docker-compose.acme-bootstrap.yml
)

bootstrap_started=0
stop_bootstrap() {
  if [[ "${bootstrap_started}" -eq 1 ]]; then
    "${bootstrap[@]}" stop nginx >/dev/null 2>&1 || true
  fi
}
trap stop_bootstrap EXIT

echo "Iniciando Nginx temporal para el desafío HTTP-01 de lovelacers.com..."
"${bootstrap[@]}" up -d --no-deps nginx
bootstrap_started=1

echo "Solicitando el certificado de Let's Encrypt..."
"${compose[@]}" run --rm --no-deps certbot

if [[ ! -s certbot/conf/live/lovelacers.com/fullchain.pem ||
      ! -s certbot/conf/live/lovelacers.com/privkey.pem ]]; then
  echo "Certbot terminó sin dejar el certificado esperado." >&2
  exit 1
fi

echo "Certificado emitido y guardado en live/certbot/conf/."
echo "Nginx temporal se apagará; el siguiente despliegue iniciará HTTPS."
