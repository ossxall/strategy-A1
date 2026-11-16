"""Carga de perfiles de entorno para entrypoints ejecutados directamente."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

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
        "BOT_STRATEGY_TIMEFRAME",
        "BOT_STRATEGY_RISK_PCT",
        "BOT_STRATEGY_STOP_LOSS_PCT",
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


def _load_simple_env(path: Path) -> None:
    """Fallback para perfiles sencillos KEY=VALUE si python-dotenv falta."""
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
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if key in os.environ:
            continue

        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        os.environ[key] = os.path.expandvars(value)


def load_direct_profile(live_dir: Path) -> None:
    """Carga .env.dev por defecto o el perfil indicado por APP_ENV."""
    profile = os.getenv("APP_ENV", "dev").strip().lower() or "dev"
    if profile not in {"dev", "prod"}:
        raise ValueError("APP_ENV debe ser dev o prod")

    profile_path = live_dir / f".env.{profile}"
    common_path = live_dir / ".env"
    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_simple_env(profile_path)
        _load_simple_env(common_path)
        cwd_env = Path.cwd() / ".env"
        if cwd_env.resolve() != common_path.resolve():
            _load_simple_env(cwd_env)
        return

    # El perfil prevalece sobre el .env común; las variables exportadas en
    # la shell mantienen prioridad por override=False.
    if profile_path.is_file():
        load_dotenv(profile_path, override=False)
    load_dotenv(common_path, override=False)
    load_dotenv(override=False)


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
