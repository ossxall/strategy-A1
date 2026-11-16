set -euo pipefail

export APP_ENV=prod
echo $APP_ENV

cd strategy-A1/live

git pull origin main

if [ ! -f .env.prod ]; then
  echo "Falta live/.env.prod; ejecuta build_remote.py para copiarlo al VPS." >&2
  exit 1
fi

APP_ENV=prod docker-compose \
  --env-file .env.prod \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -p blacker-bot1 \
  build --no-cache --pull --force-rm

APP_ENV=prod docker-compose \
  --env-file .env.prod \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -p blacker-bot1 \
  up -d --remove-orphans
