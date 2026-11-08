#!/usr/bin/env bash
#
# dev.sh — atajos locales (sin Docker) para bot1.
#
# Las bases de datos se guardan en ./data :
#   ./data/feed.db     (feed.py)
#   ./data/signal.db   (bot.py)
#
set -euo pipefail
cd "$(dirname "$0")"

FEED_DB=data/feed.db
SIGNAL_DB=data/signal.db
mkdir -p data

PY=python3
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; fi

case "${1:-help}" in
  install)
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    ;;
  feed)  exec "$PY" feed.py --db "$FEED_DB" ;;
  bot)   exec "$PY" bot.py --live-only --feed-db "$FEED_DB" --signal-db "$SIGNAL_DB" ;;
  once)  "$PY" bot.py --once --live-only --feed-db "$FEED_DB" --signal-db "$SIGNAL_DB" ;;
  up)
    LIVE_ARGS=()
    if [[ "${2:-}" == "live-only" ]]; then
      LIVE_ARGS+=(--live-only)
    fi

    "$PY" feed.py --db "$FEED_DB" \
        > >(sed -u 's/^/[feed] /') 2>&1 &
    FEED=$!

    "$PY" bot.py "${LIVE_ARGS[@]}" \
        --feed-db "$FEED_DB" \
        --signal-db "$SIGNAL_DB" \
        > >(sed -u 's/^/[bot]  /') 2>&1 &
    BOT=$!

    trap 'kill "$FEED" "$BOT" 2>/dev/null; exit 0' INT TERM
    wait
    ;;
  status)
    "$PY" - "$FEED_DB" "$SIGNAL_DB" <<'PY'
import sqlite3, sys
for path, sql in [(sys.argv[1], "SELECT COUNT(*) FROM candles"),
                  (sys.argv[2], "SELECT COUNT(*) FROM signals")]:
    try:
        n = sqlite3.connect(f"file:{path}?mode=ro", uri=True).execute(sql).fetchone()[0]
        print(f"{path}: {n}")
    except Exception:
        print(f"{path}: no existe")
PY
    ;;
  *)
    cat <<'EOF'
Uso: ./dev.sh <comando>

  install   Crea .venv e instala dependencias
  up        Arranca feed y bot y muestra sus logs juntos (Ctrl-C para parar)
  feed      Corre feed.py  -> ./data/feed.db
  bot       Corre bot.py   -> ./data/signal.db
  once      Un ciclo del bot y sale
  status    Velas en data/feed.db y eventos en data/signal.db
EOF
    ;;
esac
