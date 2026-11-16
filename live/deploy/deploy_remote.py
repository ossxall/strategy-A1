#!/usr/bin/env python3
"""Ejecuta deploy.sh en un VPS mediante SSH usando Paramiko."""

from __future__ import annotations

import argparse
import getpass
import shlex
import sys
from pathlib import Path

DEPLOY_SCRIPT = Path(__file__).with_name("deploy.sh")


def remote_cd_command(remote_dir: str) -> str:
    """Genera un `cd` remoto que admite rutas relativas y `~`."""
    if remote_dir == "~":
        return 'cd "$HOME"'
    if remote_dir.startswith("~/"):
        return f'cd "$HOME"/{shlex.quote(remote_dir[2:])}'
    return f"cd {shlex.quote(remote_dir)}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Conecta por SSH al VPS y ejecuta live/deploy/deploy.sh."
    )
    parser.add_argument("--host", required=True, help="Nombre o IP del VPS.")
    parser.add_argument("--user", required=True, help="Usuario SSH del VPS.")
    parser.add_argument("--port", type=int, default=22, help="Puerto SSH (por defecto: 22).")
    parser.add_argument(
        "--key",
        type=Path,
        help="Clave privada SSH. Si se omite, solicita la contraseña SSH de forma oculta.",
    )
    parser.add_argument(
        "--known-hosts",
        type=Path,
        help="Archivo known_hosts adicional. Las claves del sistema también se cargan.",
    )
    parser.add_argument(
        "--remote-dir",
        default="~",
        help="Directorio remoto desde el que se ejecuta deploy.sh (por defecto: HOME).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15,
        help="Tiempo máximo de conexión en segundos (por defecto: 15).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        import paramiko
    except ImportError:
        print(
            "Falta Paramiko. Instálalo con: "
            "python3 -m pip install -r live/deploy/requirements.txt",
            file=sys.stderr,
        )
        return 2

    if not DEPLOY_SCRIPT.is_file():
        print(f"No se encontró el script: {DEPLOY_SCRIPT}", file=sys.stderr)
        return 2

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if args.known_hosts:
        try:
            client.load_host_keys(str(args.known_hosts.expanduser()))
        except OSError as exc:
            print(f"No se pudo cargar known_hosts: {exc}", file=sys.stderr)
            return 2
    # No aceptar claves desconocidas automáticamente: se evitan conexiones MITM.
    client.set_missing_host_key_policy(paramiko.RejectPolicy())

    try:
        password = None
        if not args.key:
            password = getpass.getpass(f"Contraseña SSH para {args.user}@{args.host}: ")

        client.connect(
            hostname=args.host,
            port=args.port,
            username=args.user,
            key_filename=str(args.key.expanduser()) if args.key else None,
            password=password,
            timeout=args.timeout,
            banner_timeout=args.timeout,
            auth_timeout=args.timeout,
            allow_agent=bool(args.key),
            look_for_keys=bool(args.key),
        )
        command = f"{remote_cd_command(args.remote_dir)} && bash -e -s"
        stdin, stdout, _ = client.exec_command(command, get_pty=False)
        stdout.channel.set_combine_stderr(True)

        stdin.write(DEPLOY_SCRIPT.read_text(encoding="utf-8"))
        stdin.flush()
        stdin.channel.shutdown_write()

        for line in iter(stdout.readline, ""):
            print(line, end="", flush=True)

        status = stdout.channel.recv_exit_status()
        if status:
            print(f"El despliegue remoto terminó con código {status}.", file=sys.stderr)
        else:
            print("Despliegue remoto completado correctamente.")
        return status
    except (OSError, paramiko.SSHException) as exc:
        print(f"Error durante la conexión o el despliegue SSH: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
