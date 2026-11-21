cd strategy-A1/live

docker-compose \
  --profile production \
  --env-file .env.prod \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -p blacker-bot1 \
  logs -f --tail=200
