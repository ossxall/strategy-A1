#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: se requiere Python 3.10 o superior." >&2
    exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    echo "Error: se requiere Python 3.10 o superior." >&2
    exit 1
fi

if ! python3 -m venv "$VENV_DIR"; then
    echo "Error al crear el entorno virtual. En Debian/Ubuntu instala python3-venv." >&2
    exit 1
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/requirements.txt"

if [[ ! -f "$ROOT_DIR/.env" ]]; then
    cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
    echo "Creado .env a partir de .env.example."
fi

echo "Instalación completada. Ejecuta el backtest con:"
echo "  $VENV_DIR/bin/python $ROOT_DIR/run_backtest.py"
