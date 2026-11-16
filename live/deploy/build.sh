cd strategy-A1/live

if [ ! -f .env.prod ]; then
  echo "Falta live/.env.prod; ejecuta build_remote.py para copiarlo al VPS." >&2
  exit 1
fi

docker-compose build --no-cache --pull --force-rm

docker-compose up -d --build
