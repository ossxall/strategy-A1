#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
START_MONTH="2020-01"
END_MONTH="$(date -u +%Y-%m)"
OUTPUT_DIR="${1:-${OUTPUT_DIR:-"${SCRIPT_DIR}/binance"}}"

if (($# > 1)); then
    echo "Uso: $0 [directorio_salida]" >&2
    exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: se requiere python3." >&2
    exit 1
fi

echo "Descargando BTCUSDT Futures USD-M, velas de 1 minuto: ${START_MONTH} a ${END_MONTH}."
echo "Salida: ${OUTPUT_DIR}"
echo "El resultado combinado será futures/um/BTCUSDT/merged/BTCUSDT-1m.parquet."

exec python3 "${SCRIPT_DIR}/binance_klines.py" \
    --symbol BTCUSDT \
    --interval 1m \
    --market um \
    --from "${START_MONTH}" \
    --to "${END_MONTH}" \
    --merge-format parquet \
    --out "${OUTPUT_DIR}"
