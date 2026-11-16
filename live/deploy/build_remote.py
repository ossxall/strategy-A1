#!/usr/bin/env python3
"""Copia live/.env.prod al VPS y ejecuta build.sh mediante SSH."""

from __future__ import annotations

import argparse
import getpass
import posixpath
import shlex
import stat
import sys
from pathlib import Path


BUILD_SCRIPT = Path(__file__).with_name("build.sh")
LOCAL_ENV_PROD = Path(__file__).resolve().parents[1] / ".env.prod"


def remote_cd_command(remote_dir: str) -> str:
    if remote_dir == "~":
        return 'cd "$HOME"'
    if remote_dir.startswith("~/"):
        return f'cd "$HOME"/{shlex.quote(remote_dir[2:])}'
    return f"cd {shlex.quote(remote_dir)}"


def remote_base_path(sftp, remote_dir: str) -> str:
    home = sftp.normalize(".")
    if remote_dir == "~":
        return home
    if remote_dir.startswith("~/"):
        return posixpath.normpath(posixpath.join(home, remote_dir[2:]))
    if remote_dir.startswith("/"):
        return posixpath.normpath(remote_dir)
    return posixpath.normpath(posixpath.join(home, remote_dir))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copia live/.env.prod al VPS y ejecuta live/deploy/build.sh."
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
        help="Directorio remoto padre de strategy-A1 (por defecto: HOME).",
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
    if not BUILD_SCRIPT.is_file():
        print(f"No se encontró el script: {BUILD_SCRIPT}", file=sys.stderr)
        return 2
    if not LOCAL_ENV_PROD.is_file():
        print(f"No se encontró el archivo local: {LOCAL_ENV_PROD}", file=sys.stderr)
        return 2

    try:
        import paramiko
    except ImportError:
        print(
            "Falta Paramiko. Instálalo con: "
            "python3 -m pip install -r live/deploy/requirements.txt",
            file=sys.stderr,
        )
        return 2

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if args.known_hosts:
        try:
            client.load_host_keys(str(args.known_hosts.expanduser()))
        except OSError as exc:
            print(f"No se pudo cargar known_hosts: {exc}", file=sys.stderr)
            return 2
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

        sftp = client.open_sftp()
        try:
            remote_base = remote_base_path(sftp, args.remote_dir)
            remote_live = posixpath.join(remote_base, "strategy-A1", "live")
            remote_env = posixpath.join(remote_live, ".env.prod")
            try:
                remote_attrs = sftp.stat(remote_live)
            except OSError:
                print(
                    f"No existe el directorio remoto {remote_live}; "
                    "ejecuta deploy.sh primero o ajusta --remote-dir.",
                    file=sys.stderr,
                )
                return 1
            if not stat.S_ISDIR(remote_attrs.st_mode):
                print(f"La ruta remota no es un directorio: {remote_live}", file=sys.stderr)
                return 1

            print("Copiando live/.env.prod al VPS (contenido oculto).")
            sftp.put(str(LOCAL_ENV_PROD), remote_env)
            sftp.chmod(remote_env, 0o600)
        finally:
            sftp.close()

        command = f"{remote_cd_command(args.remote_dir)} && bash -e -s"
        stdin, stdout, _ = client.exec_command(command, get_pty=False)
        stdout.channel.set_combine_stderr(True)
        stdin.write(BUILD_SCRIPT.read_text(encoding="utf-8"))
        stdin.flush()
        stdin.channel.shutdown_write()

        for line in iter(stdout.readline, ""):
            print(line, end="", flush=True)

        status = stdout.channel.recv_exit_status()
        if status:
            print(f"El build remoto terminó con código {status}.", file=sys.stderr)
        else:
            print("Build remoto completado correctamente.")
        return status
    except (OSError, paramiko.SSHException) as exc:
        print(f"Error durante la conexión o el build SSH: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
