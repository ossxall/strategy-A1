#!/bin/sh
set -eu

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT/live"

compose() {
  APP_ENV=prod docker-compose --env-file .env.prod \
    -f docker-compose.yml -f docker-compose.runtime.yml \
    -p blacker-bot1 "$@"
}

docker-compose -f docker-compose.yml -p blacker-bot1 down
compose build --no-cache
compose up -d --force-recreate
