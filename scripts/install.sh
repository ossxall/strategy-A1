#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
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

"${VENV_PYTHON}" -m pip install -r "${SCRIPT_DIR}/requirements.txt"

printf 'Dependencias instaladas con %s\n' "${VENV_PYTHON}"
if [[ -n "${ACTIVATE_HINT}" ]]; then
    printf 'Activa el entorno con:\n  %s\n' "${ACTIVATE_HINT}"
fi
printf 'Para ejecutar (no se ejecuta automáticamente):\n  %s %s/trade_btcusdt.py\n' \
    "${VENV_PYTHON}" "${SCRIPT_DIR}"
