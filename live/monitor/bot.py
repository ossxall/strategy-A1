"""Bot de Telegram y consulta asíncrona de endpoints JSON de salud."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import MonitorConfig, load_config

logger = logging.getLogger(__name__)


def _utf16_length(text: str) -> int:
    """Telegram limita el texto por unidades UTF-16, no por bytes UTF-8."""
    return len(text.encode("utf-16-le")) // 2


def _truncate_utf16(text: str, max_units: int) -> str:
    units = 0
    for index, character in enumerate(text):
        units += 2 if ord(character) > 0xFFFF else 1
        if units > max_units:
            return text[:index]
    return text


def verify_password(provided: str, expected: str) -> bool:
    """Compara secretos sin filtrar diferencias por tiempo de ejecución."""
    return hmac.compare_digest(
        provided.encode("utf-8"),
        expected.encode("utf-8"),
    )


def _count_label(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _json_entry_lines(label: str, value: Any, prefix: str, is_last: bool) -> list[str]:
    connector = "└─ " if is_last else "├─ "
    branch = prefix + connector

    if isinstance(value, dict):
        if not value:
            return [f"{branch}{label} · objeto vacío {{}}"]
        count = _count_label(len(value), "campo", "campos")
        lines = [f"{branch}{label} · objeto ({count})"]
        child_prefix = prefix + ("   " if is_last else "│  ")
        items = list(value.items())
        for index, (key, child) in enumerate(items):
            lines.extend(
                _json_entry_lines(
                    json.dumps(str(key), ensure_ascii=False),
                    child,
                    child_prefix,
                    index == len(items) - 1,
                )
            )
        return lines

    if isinstance(value, list):
        if not value:
            return [f"{branch}{label} · lista vacía []"]
        count = _count_label(len(value), "elemento", "elementos")
        lines = [f"{branch}{label} · lista ({count})"]
        child_prefix = prefix + ("   " if is_last else "│  ")
        for index, child in enumerate(value):
            lines.extend(
                _json_entry_lines(
                    f"[{index}]",
                    child,
                    child_prefix,
                    index == len(value) - 1,
                )
            )
        return lines

    scalar = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return [f"{branch}{label}: {scalar}"]


def render_json_tree(payload: Any) -> str:
    """Representa recursivamente todos los campos de un valor JSON."""
    if isinstance(payload, dict):
        count = _count_label(len(payload), "campo", "campos")
        lines = [f"📦 Respuesta JSON · objeto ({count})"]
        items = list(payload.items())
        for index, (key, value) in enumerate(items):
            lines.extend(
                _json_entry_lines(
                    json.dumps(str(key), ensure_ascii=False),
                    value,
                    "",
                    index == len(items) - 1,
                )
            )
        return "\n".join(lines)

    if isinstance(payload, list):
        count = _count_label(len(payload), "elemento", "elementos")
        lines = [f"📦 Respuesta JSON · lista ({count})"]
        for index, value in enumerate(payload):
            lines.extend(
                _json_entry_lines(
                    f"[{index}]",
                    value,
                    "",
                    index == len(payload) - 1,
                )
            )
        return "\n".join(lines)

    return "\n".join(
        [
            "📦 Respuesta JSON · valor",
            *_json_entry_lines("valor", payload, "", True),
        ]
    )


@dataclass(frozen=True)
class CheckResult:
    status_code: int | None
    payload: Any = None
    raw_body: str | None = None
    error: str | None = None
    response_truncated: bool = False


def fetch_endpoint(
    url: str, timeout_seconds: float, max_response_bytes: int
) -> CheckResult:
    """Hace GET al endpoint y lee JSON sin depender de su esquema."""
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "live-monitor/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            status_code = response.getcode()
            body = response.read(max_response_bytes + 1)
    except HTTPError as exc:
        status_code = exc.code
        try:
            body = exc.read(max_response_bytes + 1)
        except OSError:
            body = b""
        finally:
            exc.close()
    except (OSError, URLError, TimeoutError, ValueError) as exc:
        reason = getattr(exc, "reason", None)
        return CheckResult(
            status_code=None,
            error=str(reason or exc)[:300],
        )

    truncated = len(body) > max_response_bytes
    body = body[:max_response_bytes]
    text = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return CheckResult(
            status_code=status_code,
            raw_body=text,
            response_truncated=truncated,
        )
    return CheckResult(
        status_code=status_code,
        payload=payload,
        response_truncated=truncated,
    )


def format_result(service: str, url: str, result: CheckResult) -> str:
    """Crea una cabecera estable y un árbol con todos los datos de respuesta."""
    if result.status_code is None:
        lines = [
            f"❌ Monitor · {service} · sin respuesta HTTP",
            f"Endpoint: {url}",
            f"Error: {result.error or 'error de conexión'}",
        ]
    else:
        try:
            reason = HTTPStatus(result.status_code).phrase
        except ValueError:
            reason = "HTTP"
        icon = "✅" if 200 <= result.status_code < 300 else "❌"
        lines = [
            f"{icon} Monitor · {service}",
            f"HTTP: {result.status_code} {reason}",
            f"Endpoint: {url}",
        ]
        if result.raw_body is not None:
            lines.extend(["Respuesta:", result.raw_body or "(cuerpo vacío)"])
        else:
            lines.append(render_json_tree(result.payload))

    if result.response_truncated:
        lines.append("… respuesta recortada por superar el límite configurado")

    return "\n".join(lines)


def _split_text(text: str, max_units: int) -> list[str]:
    """Divide texto sin perder caracteres y respetando el límite UTF-16."""
    if not text:
        return [""]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        remainder = line
        while remainder:
            available = max_units - _utf16_length(current)
            if _utf16_length(remainder) <= available:
                current += remainder
                remainder = ""
            elif current:
                chunks.append(current)
                current = ""
            else:
                part = _truncate_utf16(remainder, max_units)
                chunks.append(part)
                remainder = remainder[len(part) :]
    if current:
        chunks.append(current)
    return chunks


def paginate_message(text: str, max_chars: int = 3900) -> list[str]:
    """Parte una respuesta larga en mensajes Telegram sin descartar contenido."""
    if max_chars < 64:
        raise ValueError("max_chars debe ser al menos 64")
    body_chunks = _split_text(text, max_chars - 32)
    total = len(body_chunks)
    return [
        f"📄 {index}/{total}\n{chunk}"
        for index, chunk in enumerate(body_chunks, start=1)
    ]


def create_dispatcher(config: MonitorConfig):
    """Crea handlers de aiogram; el módulo puede probarse sin importar aiogram."""
    from aiogram import Dispatcher, Router
    from aiogram.filters import Command
    from aiogram.filters.command import CommandObject
    from aiogram.types import Message

    router = Router(name="live-monitor")
    authenticated_users: set[int] = set()

    def is_authenticated(message: Message) -> bool:
        user = message.from_user
        return user is not None and user.id in authenticated_users

    @router.message(Command("start", "help"))
    async def show_help(message: Message) -> None:
        services = ", ".join(sorted(config.services))
        await message.answer(
            "Monitor de salud de servicios live.\n"
            "Primero autentícate con /auth <contraseña>.\n"
            f"Comando: /status <servicio>\n"
            f"Servicios: {services}\n"
            "También puedes usar /status all y /logout."
        )

    @router.message(Command("auth"))
    async def authenticate(message: Message, command: CommandObject) -> None:
        if message.chat.type != "private":
            await message.answer(
                "Por seguridad, autentícate en un chat privado con el bot."
            )
            return
        user = message.from_user
        if user is None:
            await message.answer("No se pudo identificar tu usuario de Telegram.")
            return

        supplied_password = (command.args or "").strip()
        if not supplied_password:
            await message.answer("Uso: /auth <contraseña>")
            return
        if not verify_password(supplied_password, config.password):
            await message.answer("Contraseña incorrecta.")
            return

        authenticated_users.add(user.id)
        await message.answer(
            "Autenticación correcta. Ya puedes consultar los servicios; "
            "la sesión dura hasta /logout o el reinicio del monitor."
        )

    @router.message(Command("logout"))
    async def logout(message: Message) -> None:
        user = message.from_user
        if user is not None:
            authenticated_users.discard(user.id)
        await message.answer("Sesión cerrada.")

    @router.message(Command("status"))
    async def show_status(message: Message, command: CommandObject) -> None:
        if message.chat.type != "private":
            await message.answer("Por seguridad, usa el monitor en un chat privado.")
            return
        if not is_authenticated(message):
            await message.answer("Primero autentícate con /auth <contraseña>.")
            return

        service_name = (command.args or "").strip().lower()
        if not service_name:
            await message.answer(
                "Indica un servicio. Uso: /status <servicio>\n"
                f"Servicios: {', '.join(sorted(config.services))}"
            )
            return

        if service_name == "all":
            selected = list(config.services.values())
        else:
            service = config.services.get(service_name)
            if service is None:
                await message.answer(
                    f"Servicio desconocido: {service_name}\n"
                    f"Disponibles: {', '.join(sorted(config.services))}"
                )
                return
            selected = [service]

        results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    fetch_endpoint,
                    service.url,
                    config.request_timeout_seconds,
                    config.max_response_bytes,
                )
                for service in selected
            ),
            return_exceptions=True,
        )
        for service, result in zip(selected, results):
            if isinstance(result, BaseException):
                logger.exception(
                    "Falló la consulta de salud para %s",
                    service.name,
                    exc_info=result,
                )
                result = CheckResult(
                    status_code=None,
                    error="error inesperado al consultar el endpoint",
                )
            formatted = format_result(service.name, service.url, result)
            for page in paginate_message(formatted, config.max_message_chars):
                await message.answer(page)

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


async def run() -> None:
    from aiogram import Bot
    from aiogram.exceptions import TelegramUnauthorizedError

    config = load_config()
    bot = Bot(token=config.token)
    dispatcher = create_dispatcher(config)
    try:
        # Verifica las credenciales antes de iniciar el polling para poder dar
        # una instrucción útil si Telegram rechaza el token configurado.
        await bot.get_me()
        await dispatcher.start_polling(
            bot,
            polling_timeout=config.polling_timeout_seconds,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    except TelegramUnauthorizedError:
        logger.error(
            "Telegram rechazó el token. Revisa MONITOR_TELEGRAM_BOT_TOKEN "
            "en el perfil activo (.env.dev por defecto; .env.prod con APP_ENV=prod)."
        )
        raise SystemExit(1) from None
    finally:
        await bot.session.close()
