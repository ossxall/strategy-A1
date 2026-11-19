#!/usr/bin/env bash

# delete_remote.py ejecuta el script con `bash -e`; desactivamos errexit para
# intentar todas las tareas y comunicar al final cualquier fallo.
set +e
set -uo pipefail

TARGET_ROOT=/root
PROJECT_DIR="${TARGET_ROOT}/strategy-A1"
COMPOSE_FILE="${PROJECT_DIR}/live/docker-compose.yml"
failures=0

record_failure() {
  printf 'ERROR: %s\n' "$1" >&2
  failures=$((failures + 1))
}

run_step() {
  local description="$1"
  shift

  printf '\n==> %s\n' "$description"
  "$@"
  local status=$?
  if (( status != 0 )); then
    record_failure "${description} (código ${status})"
  fi
}

remove_all() {
  local description="$1"
  local ids="$2"
  shift 2

  if [[ -z "$ids" ]]; then
    printf '\n==> %s: no hay recursos\n' "$description"
    return
  fi

  printf '\n==> %s\n' "$description"
  printf '%s\n' "$ids" | xargs -r -n 50 "$@"
  local status=$?
  if (( status != 0 )); then
    record_failure "${description} (código ${status})"
  fi
}

if ! command -v docker >/dev/null 2>&1; then
  record_failure "no se encontró el comando docker"
else
  compose=()
  if command -v docker-compose >/dev/null 2>&1; then
    compose=(docker-compose)
  elif docker compose version >/dev/null 2>&1; then
    compose=(docker compose)
  fi

  if [[ -f "$COMPOSE_FILE" ]]; then
    if (( ${#compose[@]} )); then
      run_step "Detener el proyecto blacker-bot1" \
        "${compose[@]}" -f "$COMPOSE_FILE" -p blacker-bot1 \
        down --volumes --rmi all --remove-orphans
    else
      record_failure "no se encontró docker-compose ni el plugin docker compose"
    fi
  else
    printf 'AVISO: no se encontró %s; se omite el apagado de Compose.\n' "$COMPOSE_FILE"
  fi

  if container_ids=$(docker ps -aq); then
    remove_all "Eliminar todos los contenedores del servidor" "$container_ids" docker rm -f
  else
    record_failure "no se pudieron listar los contenedores de Docker"
  fi

  if volume_ids=$(docker volume ls -q); then
    remove_all "Eliminar todos los volúmenes del servidor" "$volume_ids" docker volume rm -f
  else
    record_failure "no se pudieron listar los volúmenes de Docker"
  fi

  run_step "Limpiar imágenes, redes y recursos Docker no utilizados" \
    docker system prune -a --volumes -f
  run_step "Limpiar la caché de compilación de Docker" docker builder prune -af
fi

# Se mantienen las rutas del despliegue original, pero absolutas para que un
# fallo de `cd` no cambie accidentalmente qué directorios se eliminan.
run_step "Eliminar el checkout del proyecto" rm -rf -- "$PROJECT_DIR"
run_step "Eliminar /root/nginx" rm -rf -- "${TARGET_ROOT}/nginx"

if (( failures > 0 )); then
  printf '\nBorrado finalizado con %d error(es). Revisa los mensajes anteriores.\n' "$failures" >&2
  exit 1
fi

printf '\nBorrado completado sin errores.\n'
