set -euo pipefail

export APP_ENV=prod
export AUTHELIA_UID="$(id -u)"
export AUTHELIA_GID="$(id -g)"
echo $APP_ENV

cd strategy-A1/live

git pull origin main

if [ ! -f .env.prod ]; then
  echo "Falta live/.env.prod; ejecuta build_remote.py para copiarlo al VPS." >&2
  exit 1
fi

for path in \
  authelia/configuration.yml \
  authelia/users_database.yml \
  authelia/secrets/session_secret \
  authelia/secrets/storage_encryption_key
do
  if [ ! -s "$path" ]; then
    echo "Falta $path; ejecuta bash deploy/setup_authelia.sh en el VPS primero." >&2
    exit 1
  fi
done

# Migrate the older quoted duration which Authelia rejects; YAML integer -1
# is interpreted as seconds and disables the Remember Me option.
sed -i -E \
  "s/^([[:space:]]*remember_me:[[:space:]]*)['\"]-1['\"]([[:space:]]*(#.*)?)$/\\1-1\\2/" \
  authelia/configuration.yml

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

# Nginx config is bind-mounted; recreate it so updated host files are loaded.
APP_ENV=prod docker-compose \
  --env-file .env.prod \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -p blacker-bot1 \
  up -d --no-deps --force-recreate nginx
