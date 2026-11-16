#!/bin/sh
set -eu

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT/live"

docker-compose -f docker-compose.yml -p blacker-bot1 \
  down --volumes --rmi all --remove-orphans
