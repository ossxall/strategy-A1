"""Carga y valida la configuración del servicio de monitoreo."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")


@dataclass(frozen=True)
class ServiceEndpoint:
    name: str
    url: str


@dataclass(frozen=True)
class MonitorConfig:
    token: str
    password: str
    services: dict[str, ServiceEndpoint]
    request_timeout_seconds: float
    max_response_bytes: int
    max_message_chars: int
    polling_timeout_seconds: int


def _positive_number(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} debe ser un número positivo") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} debe ser un número positivo")
    return number


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> MonitorConfig:
    """Lee el YAML y toma el token de Telegram desde la variable configurada."""
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except OSError as exc:
        raise ValueError(
            f"No se pudo leer la configuración {config_path}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise TypeError("La raíz de config.yaml debe ser un objeto YAML")

    telegram = raw.get("telegram", {})
    if not isinstance(telegram, dict):
        raise TypeError("'telegram' debe ser un objeto YAML")
    token_env = telegram.get("token_env", "MONITOR_TELEGRAM_BOT_TOKEN")
    token = os.getenv(str(token_env), "").strip()
    if not token:
        raise ValueError(
            f"Falta el token de Telegram. Configura la variable de entorno {token_env}."
        )

    password_env = telegram.get("password_env", "MONITOR_PASSWORD")
    password = os.getenv(str(password_env), "").strip()
    if not password:
        raise ValueError(
            f"Falta la contraseña del monitor. Configura la variable de entorno "
            f"{password_env}."
        )

    endpoints = raw.get("services")
    if not isinstance(endpoints, dict) or not endpoints:
        raise ValueError("'services' debe contener al menos un endpoint")

    services: dict[str, ServiceEndpoint] = {}
    for name, endpoint in endpoints.items():
        service_name = str(name).strip().lower()
        if not service_name or not service_name.replace("_", "").isalnum():
            raise ValueError(f"Nombre de servicio no válido: {name!r}")
        if not isinstance(endpoint, dict) or not isinstance(endpoint.get("url"), str):
            raise TypeError(f"'services.{name}.url' es obligatorio")
        url_override = os.getenv(f"MONITOR_{service_name.upper()}_URL", "").strip()
        url = url_override or endpoint["url"].strip()
        parsed_url = urlparse(url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
        ):
            raise ValueError(f"URL HTTP/HTTPS no válida para el servicio {name!r}")
        services[service_name] = ServiceEndpoint(name=service_name, url=url)

    http = raw.get("http", {})
    if not isinstance(http, dict):
        raise TypeError("'http' debe ser un objeto YAML")
    request_timeout = _positive_number(
        http.get("request_timeout_seconds", 3), "http.request_timeout_seconds"
    )
    max_response_bytes = int(http.get("max_response_bytes", 65536))
    if max_response_bytes < 1:
        raise ValueError("http.max_response_bytes debe ser al menos 1")
    max_message_chars = int(http.get("max_message_chars", 3900))
    if not 256 <= max_message_chars <= 4096:
        raise ValueError("http.max_message_chars debe estar entre 256 y 4096")

    polling_timeout = int(telegram.get("polling_timeout_seconds", 30))
    if not 1 <= polling_timeout <= 60:
        raise ValueError("telegram.polling_timeout_seconds debe estar entre 1 y 60")

    return MonitorConfig(
        token=token,
        password=password,
        services=services,
        request_timeout_seconds=request_timeout,
        max_response_bytes=max_response_bytes,
        max_message_chars=max_message_chars,
        polling_timeout_seconds=polling_timeout,
    )
