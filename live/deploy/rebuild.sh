#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

compose() {
  APP_ENV=prod docker-compose --env-file .env.prod -p blacker-bot1 "$@"
}

compose down
compose build --no-cache
compose up -d --force-recreate
