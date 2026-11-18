"""Carga de perfiles de entorno para entrypoints ejecutados directamente."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_SECRET_ENV = frozenset(
    {
        "BOT_BINANCE_ACCOUNT_ID",
        "BOT_BINANCE_API_KEY",
        "BOT_BINANCE_API_SECRET",
        "BOT_BINANCE_PRODUCTION_AUDIT_API_KEY",
        "BOT_BINANCE_PRODUCTION_AUDIT_API_SECRET",
        "LETSENCRYPT_EMAIL",
        "MONITOR_BOT_URL",
        "MONITOR_EXECUTOR_URL",
        "MONITOR_FEED_URL",
        "MONITOR_PASSWORD",
        "MONITOR_TELEGRAM_BOT_TOKEN",
        "SQLITE_WEB_PASSWORD",
    }
)

_SERVICE_SECRETS = {
    "executor": frozenset(
        {
            "BOT_BINANCE_ACCOUNT_ID",
            "BOT_BINANCE_API_KEY",
            "BOT_BINANCE_API_SECRET",
            "BOT_BINANCE_PRODUCTION_AUDIT_API_KEY",
            "BOT_BINANCE_PRODUCTION_AUDIT_API_SECRET",
        }
    ),
    "healthcheck": frozenset({"BOT_BINANCE_ACCOUNT_ID"}),
    "monitor": frozenset(
        {
            "MONITOR_BOT_URL",
            "MONITOR_EXECUTOR_URL",
            "MONITOR_FEED_URL",
            "MONITOR_PASSWORD",
            "MONITOR_TELEGRAM_BOT_TOKEN",
        }
    ),
    "sqlweb": frozenset({"SQLITE_WEB_PASSWORD"}),
}

_PROD_REQUIRED_ENV = {
    "feed": (
        "FEED_SYMBOL",
        "FEED_INTERVAL",
        "FEED_START_DATE",
        "FEED_DB_PATH",
    ),
    "bot": (
        "BOT_SYMBOL",
        "BOT_INTERVAL",
        "BOT_LIVE_ONLY",
    ),
    "executor": (
        "BOT_BINANCE_ENVIRONMENT",
        "BOT_BINANCE_TRADING_ENABLED",
        "BOT_BINANCE_ACCOUNT_ID",
        "BOT_SIGNAL_DB",
    ),
    "monitor": (
        "MONITOR_TELEGRAM_BOT_TOKEN",
        "MONITOR_PASSWORD",
    ),
}


def _load_simple_env(path: Path, allowed_keys: frozenset[str]) -> None:
    """Fallback para leer solo las claves privadas de un archivo KEY=VALUE."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[7:].lstrip()
        key, separator, raw_value = text.partition("=")
        key = key.strip()
        if (
            not separator
            or key not in allowed_keys
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        ):
            continue
        if key in os.environ:
            continue

        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        os.environ[key] = os.path.expandvars(value)


def _load_yaml_profile(live_dir: Path, profile: str) -> None:
    """Carga config.dev.yaml y en producción aplica config.prod.yaml encima."""
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "Falta PyYAML; instala las dependencias de live/requirements.txt."
        ) from exc

    selected_profiles = ("dev", "prod") if profile == "prod" else ("dev",)
    merged: dict[str, object] = {}
    for selected_profile in selected_profiles:
        path = live_dir / f"config.{selected_profile}.yaml"
        try:
            with path.open("r", encoding="utf-8") as stream:
                values = yaml.safe_load(stream)
        except OSError as exc:
            raise ValueError(
                f"No se pudo leer la configuración {path}: {exc}"
            ) from exc

        if not isinstance(values, dict):
            raise ValueError(f"{path} debe contener un mapa YAML de variables.")

        for name, value in values.items():
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            ):
                raise ValueError(f"Nombre de variable no válido en {path}: {name!r}")
            if name.startswith("BOT_STRATEGY_"):
                raise ValueError(
                    f"{name} no se puede configurar por entorno; "
                    "la estrategia usa los valores de strategy/config.py."
                )
            if name in _SECRET_ENV:
                raise ValueError(
                    f"{name} es privada y debe configurarse en el entorno "
                    "o en el .env del perfil."
                )
            if isinstance(value, (dict, list, tuple, set)):
                raise ValueError(f"{name} debe tener un valor YAML escalar.")
            merged[name] = value

    # Aplicar solo tras fusionar mantiene la prioridad de variables exportadas
    # y permite que el perfil prod reemplace correctamente los valores base.
    for name, value in merged.items():
        os.environ.setdefault(name, "" if value is None else str(value))


def _load_profile_secrets(
    live_dir: Path, profile: str, service: str | None
) -> None:
    """Carga del .env del perfil solo los secretos requeridos por el servicio."""
    allowed_keys = _SERVICE_SECRETS.get(service or "", frozenset())
    if not allowed_keys:
        return

    path = live_dir / f".env.{profile}"
    if not path.is_file():
        return

    try:
        from dotenv import dotenv_values
    except ImportError:
        _load_simple_env(path, allowed_keys)
        return

    for name, value in dotenv_values(path).items():
        if name in allowed_keys and value is not None and name not in os.environ:
            os.environ[name] = value


def load_direct_profile(live_dir: Path, *, service: str | None = None) -> None:
    """Carga la base dev y, si APP_ENV=prod, sus overrides de producción.

    Carga además, solo, las claves privadas del servicio desde ``.env.dev`` o
    ``.env.prod``. ``.env.example`` es únicamente una plantilla.
    """
    profile = os.getenv("APP_ENV", "dev").strip().lower() or "dev"
    if profile not in {"dev", "prod"}:
        raise ValueError("APP_ENV debe ser dev o prod")

    if service is not None and service not in {
        "bot",
        "executor",
        "feed",
        "healthcheck",
        "monitor",
        "sqlweb",
        "webssh",
    }:
        raise ValueError(f"Servicio desconocido para cargar secretos: {service}")

    _load_yaml_profile(live_dir, profile)
    _load_profile_secrets(live_dir, profile, service)


def validate_prod_environment(service: str) -> None:
    """Valida la configuración esencial del servicio cuando APP_ENV=prod."""
    service = service.strip().lower()
    if service not in _PROD_REQUIRED_ENV:
        raise ValueError(f"Servicio desconocido para validar entorno: {service}")
    if os.getenv("APP_ENV", "dev").strip().lower() != "prod":
        return

    missing = [
        name for name in _PROD_REQUIRED_ENV[service]
        if not os.getenv(name, "").strip()
    ]
    invalid: list[str] = []

    if service == "bot" and os.getenv("BOT_LIVE_ONLY", "").strip().lower() not in {
        "1", "true", "yes", "y", "on",
    }:
        invalid.append("BOT_LIVE_ONLY=true")

    if service == "executor":
        environment = os.getenv("BOT_BINANCE_ENVIRONMENT", "").strip().lower()
        if environment not in {"production", "mainnet"}:
            invalid.append("BOT_BINANCE_ENVIRONMENT=production")
        trading_enabled = os.getenv(
            "BOT_BINANCE_TRADING_ENABLED", ""
        ).strip().lower() in {"1", "true", "yes", "y", "on"}
        if trading_enabled:
            if os.getenv(
                "BOT_BINANCE_ALLOW_LIVE_TRADING", ""
            ).strip().lower() not in {"1", "true", "yes", "y", "on"}:
                invalid.append("BOT_BINANCE_ALLOW_LIVE_TRADING=true")
            for name in ("BOT_BINANCE_API_KEY", "BOT_BINANCE_API_SECRET"):
                if not os.getenv(name, "").strip():
                    missing.append(name)

    problems = []
    if missing:
        problems.append("faltan variables: " + ", ".join(missing))
    if invalid:
        problems.append("valores inválidos: " + ", ".join(invalid))
    if problems:
        message = (
            f"❌ APP_ENV=prod: configuración inválida para {service}; "
            + "; ".join(problems)
        )
        print(message, file=sys.stderr)
        raise SystemExit(2)
