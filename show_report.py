#!/usr/bin/env python3
"""Sirve el dashboard y los informes HTML de report/ por HTTP."""

from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "report"


class ReportHandler(SimpleHTTPRequestHandler):
    """Sirve exclusivamente archivos contenidos en el directorio report/."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPORT_DIR), **kwargs)

    def translate_path(self, path: str) -> str:
        requested = unquote(urlsplit(path).path)
        if requested in {"/report", "/report/"}:
            return str(REPORT_DIR / "index.html")
        if not requested.startswith("/report/"):
            return str(REPORT_DIR / "__not_found__")

        relative_path = requested.removeprefix("/report/")
        target = (REPORT_DIR / relative_path).resolve()
        try:
            target.relative_to(REPORT_DIR.resolve())
        except ValueError:
            return str(REPORT_DIR / "__not_found__")
        return str(target)

    def log_message(self, format: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sirve el dashboard del backtest por HTTP.")
    parser.add_argument("--host", default="127.0.0.1", help="interfaz de escucha (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="puerto HTTP (default: 8000)")
    args = parser.parse_args()

    if not (REPORT_DIR / "index.html").is_file():
        parser.error(f"No se encontró {REPORT_DIR / 'index.html'}")

    server = ThreadingHTTPServer((args.host, args.port), ReportHandler)
    base_url = f"http://{args.host}:{args.port}"
    print(f"Dashboard: {base_url}/report/index.html")
    print(f"HTML de backtests: {base_url}/report/backtest_out/html/")
    print("Pulsa Ctrl+C para detener el servidor.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
