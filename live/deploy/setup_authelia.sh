#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

for command in docker openssl script tee; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Falta el comando requerido: $command" >&2
    exit 1
  fi
done

if [[ ! -t 0 || ! -w /dev/tty ]]; then
  echo "Ejecuta este asistente desde una terminal interactiva." >&2
  exit 1
fi

mkdir -p authelia/data authelia/secrets

if [[ ! -e authelia/configuration.yml ]]; then
  cp authelia/configuration.yml.example authelia/configuration.yml
  chmod 600 authelia/configuration.yml
  echo "Creada authelia/configuration.yml"
else
  echo "Se conserva authelia/configuration.yml existente."
fi

# Older template copies quoted -1 as a string, which Authelia cannot parse as
# a duration. YAML integer -1 is interpreted as seconds and disables remember-me.
sed -i -E \
  "s/^([[:space:]]*remember_me:[[:space:]]*)['\"]-1['\"]([[:space:]]*(#.*)?)$/\\1-1\\2/" \
  authelia/configuration.yml

create_secret() {
  local path="$1"

  if [[ -e "$path" ]]; then
    if [[ ! -s "$path" ]]; then
      echo "El secreto existente está vacío: $path" >&2
      exit 1
    fi
    chmod 600 "$path"
    echo "Se conserva $path"
    return
  fi

  (umask 077 && openssl rand -hex 32 > "$path")
  chmod 600 "$path"
  echo "Creado $path"
}

create_secret authelia/secrets/session_secret
create_secret authelia/secrets/storage_encryption_key

if [[ -e authelia/users_database.yml ]]; then
  echo "Se conserva authelia/users_database.yml existente."
  echo "Authelia ya tiene una base de usuarios; no se modificó."
  exit 0
fi

read -r -p "Nombre de usuario Authelia (letras, números, punto, guion o _): " username
if [[ ! "$username" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then
  echo "Nombre de usuario inválido." >&2
  exit 1
fi

echo "Elige y confirma la contraseña en el prompt de Authelia."
docker pull authelia/authelia:4.39.28 >/dev/null

# Use a PTY so Authelia reads the password without echoing it. `script` saves
# no transcript here; only its output is relayed to the terminal and captured
# in memory to extract the Argon2 digest.
hash_output="$(
  script -q -e -c \
    'docker run --rm -it authelia/authelia:4.39.28 authelia crypto hash generate argon2' \
    /dev/null | tee /dev/tty
)"
password_hash="$(
  printf '%s\n' "$hash_output" |
    tr -d '\r' |
    sed -n 's/.*Digest: //p' |
    tail -n 1
)"

if [[ ! "$password_hash" == '$argon2id$'* ]]; then
  echo "No se pudo obtener un hash Argon2 de Authelia; no se creó la base de usuarios." >&2
  exit 1
fi

cat > authelia/users_database.yml <<EOF
users:
  '$username':
    disabled: false
    displayname: '$username'
    password: '$password_hash'
    groups: []
EOF
chmod 600 authelia/users_database.yml

echo "Creada authelia/users_database.yml para '$username'."
echo "Authelia está preparado; sus archivos permanecen en el VPS y no se suben a Git."
