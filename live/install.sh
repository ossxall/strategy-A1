#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
APP_ENV="${APP_ENV:-dev}"
case "${APP_ENV}" in
    dev|prod) ;;
    *)
        printf 'APP_ENV debe ser dev o prod (valor recibido: %s)\n' "${APP_ENV}" >&2
        exit 2
        ;;
esac
BASE_CONFIG="${ROOT_DIR}/config.dev.yaml"
PROFILE_CONFIG="${ROOT_DIR}/config.${APP_ENV}.yaml"
if [[ ! -f "${BASE_CONFIG}" || ! -f "${PROFILE_CONFIG}" ]]; then
    printf 'Falta la configuración base o del perfil %s\n' "${APP_ENV}" >&2
    exit 2
fi
read_python_bin() {
    awk -F ': ' '$1 == "PYTHON_BIN" {
    gsub(/^"/, "", $2)
    gsub(/"$/, "", $2)
    print $2
    exit
    }' "$1"
}
PYTHON_BIN="$(read_python_bin "${PROFILE_CONFIG}")"
if [[ -z "${PYTHON_BIN}" && "${APP_ENV}" == "prod" ]]; then
    PYTHON_BIN="$(read_python_bin "${BASE_CONFIG}")"
fi
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
    VENV_PYTHON="${VIRTUAL_ENV}/bin/python"
    ACTIVATE_HINT=""
else
    if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
        "${PYTHON_BIN}" -m venv "${VENV_DIR}"
    fi
    VENV_PYTHON="${VENV_DIR}/bin/python"
    ACTIVATE_HINT="source \"${VENV_DIR}/bin/activate\""
fi

"${VENV_PYTHON}" -m pip install -r "${ROOT_DIR}/requirements.txt"

printf 'Dependencias instaladas en %s\n' "${VENV_PYTHON}"
if [[ -n "${ACTIVATE_HINT}" ]]; then
    printf 'Activa el entorno con:\n  %s\n' "${ACTIVATE_HINT}"
fi
