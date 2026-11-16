#!/bin/sh
set -eu

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT/live"

docker-compose -f docker-compose.yml down --volumes --rmi all --remove-orphans
rm -rf strategy-A1

docker system prune -a --volumes -f

docker rm -f $(docker ps -aq) 2>/dev/null; docker system prune -a --volumes -f; docker builder prune -af


