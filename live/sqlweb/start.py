#!/usr/bin/env python3
"""Inicia sqlite-web con todas las bases .db del directorio configurado."""

from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import subprocess
import sys
from pathlib import Path

LIVE_DIR = Path(__file__).resolve().parents[1]
if str(LIVE_DIR) not in sys.path:
    sys.path.insert(0, str(LIVE_DIR))

from env_profiles import load_direct_profile

load_direct_profile(LIVE_DIR, service="sqlweb")


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("el puerto debe ser un número") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("el puerto debe estar entre 1 y 65535")
    return port


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _default_database_dir() -> Path:
    configured = os.environ.get("SQLWEB_DATABASE_DIR")
    if configured:
        return Path(configured)

    # En producción puede montarse /live; en el checkout, los datos están
    # guardados en live/data.
    mounted_dir = Path("/live")
    if mounted_dir.is_dir():
        return mounted_dir
    if any(path.is_file() for path in LIVE_DIR.glob("*.db")):
        return LIVE_DIR
    data_dir = LIVE_DIR / "data"
    if data_dir.is_dir():
        return data_dir
    return LIVE_DIR


def _sqlite_web_command() -> str:
    executable = Path(sys.executable).resolve().parent / "sqlite_web"
    if executable.is_file():
        return str(executable)
    found = shutil.which("sqlite_web")
    if found:
        return found
    raise SystemExit(
        "No se encontró sqlite_web. Instala las dependencias con "
        "`python -m pip install -r live/requirements.txt`."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-dir",
        type=Path,
        default=_default_database_dir(),
        help="directorio que contiene las bases (por defecto: /live o live/data)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("SQLWEB_HOST", "127.0.0.1"),
        help="interfaz de escucha (por defecto: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=os.environ.get("SQLWEB_PORT", "8082"),
        help="puerto HTTP (por defecto: 8082)",
    )
    parser.add_argument(
        "--writable",
        action="store_true",
        help="permitir cambios en las bases (por defecto: solo lectura)",
    )
    parser.add_argument(
        "--behind-auth-proxy",
        action="store_true",
        help="permite no usar password local al escuchar tras un proxy autenticado",
    )
    args = parser.parse_args()

    if not args.database_dir.is_dir():
        raise SystemExit(f"No existe el directorio de bases: {args.database_dir}")

    databases = sorted(
        path.resolve()
        for path in args.database_dir.glob("*.db")
        if path.is_file()
    )
    if not databases:
        raise SystemExit(f"No hay archivos .db en {args.database_dir}")

    if (
        not _is_loopback(args.host)
        and not os.environ.get("SQLITE_WEB_PASSWORD")
        and not args.behind_auth_proxy
    ):
        raise SystemExit(
            "Para escuchar fuera de localhost, define SQLITE_WEB_PASSWORD "
            "con una contraseña segura o usa --behind-auth-proxy solo tras "
            "un proxy autenticado."
        )

    command = [
        _sqlite_web_command(),
        "--no-browser",
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    url_prefix = os.environ.get("SQLWEB_URL_PREFIX", "").rstrip("/")
    if url_prefix:
        if not url_prefix.startswith("/") or url_prefix.startswith("//"):
            raise SystemExit("SQLWEB_URL_PREFIX debe comenzar con una sola '/'.")
        command.extend(["--url-prefix", url_prefix])
    if not args.writable:
        command.append("--read-only")
    command.extend(str(database) for database in databases)

    print(
        f"sqlite-web en http://{args.host}:{args.port}{url_prefix}/ "
        f"({len(databases)} bases; "
        f"{'lectura/escritura' if args.writable else 'solo lectura'})",
        flush=True,
    )
    for database in databases:
        print(f"  - {database}", flush=True)

    result = subprocess.run(command, check=False)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
