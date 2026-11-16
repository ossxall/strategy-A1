#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

APP_ENV=prod docker-compose --env-file .env.prod -p blacker-bot1 \
  down --volumes --rmi all --remove-orphans
