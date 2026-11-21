#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
export APP_ENV=prod
compose=(
  docker-compose
  --profile production
  --env-file .env.prod
  -f docker-compose.yml
  -f docker-compose.runtime.yml
  -p blacker-bot1
)

"${compose[@]}" run --rm --no-deps certbot \
  renew --webroot --webroot-path /var/www/certbot --quiet
"${compose[@]}" exec -T nginx nginx -s reload
