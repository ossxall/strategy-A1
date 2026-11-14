"""Orquestación del bot de señales."""

from __future__ import annotations

from strategy.strategy import Config, EMACrossStrategy

from .config import (
    DEFAULT_FEED_DB,
    DEFAULT_HTTP_HOST,
    DEFAULT_HTTP_PORT,
    DEFAULT_INTERVAL,
    DEFAULT_LIVE_ONLY,
    DEFAULT_POLL_SECONDS,
    DEFAULT_RUN_ONCE,
    DEFAULT_SIGNAL_DB,
    DEFAULT_SYMBOL,
    STOP_EVENT,
    build_strategy_config,
    env_bool,
    env_float,
    env_int,
    env_str,
    logger,
    ms_to_iso,
    parse_live_since,
    resolve_path,
    touch_heartbeat,
    ts_ms,
    utc_now,
)
from .events import build_events, events_to_trades
from .series import build_series_frames, resample
from .storage import FeedReader, SignalStore

try:
    from server import SeriesSnapshot, start_server
except ModuleNotFoundError as exc:
    if exc.name != "server":
        raise

    class SeriesSnapshot:
        """Placeholder para mantener el endpoint HTTP como dependencia opcional."""

        def __init__(self, *args, **kwargs):
            raise RuntimeError("El endpoint HTTP requiere el módulo server.")

    start_server = None
    HTTP_SERVER_AVAILABLE = False
else:
    HTTP_SERVER_AVAILABLE = True


class SignalBot:
    """Lee el feed, genera eventos idempotentes y mantiene el estado live-only."""

    def __init__(
        self,
        cfg: Config,
        feed: FeedReader,
        store: SignalStore,
        symbol: str,
        interval: str,
        min_bars: int | None = None,
        live_only: bool = False,
        live_since_ms: int | None = None,
        snapshot: SeriesSnapshot | None = None,
    ):
        self.cfg = cfg
        self.feed = feed
        self.store = store
        self.symbol = symbol
        self.interval = interval
        self.strategy = EMACrossStrategy(cfg)
        self.min_bars = min_bars or (cfg.ema_slow + 2)
        self.live_only = live_only
        self.live_since_ms = live_since_ms
        self.snapshot = snapshot
        self._live_resolved = False

    def _resolve_live_since(self) -> None:
        """Fija y persiste la marca desde la que se publican eventos live-only."""
        if self._live_resolved:
            return
        self._live_resolved = True
        if not self.live_only:
            return

        if self.live_since_ms is None:
            stored = self.store.get_state("live_since_ms")
            if stored is not None:
                self.live_since_ms = int(stored)
                logger.info(
                    "Modo live-only: reanudando desde %s.",
                    ms_to_iso(self.live_since_ms),
                )
                return
            self.live_since_ms = ts_ms(utc_now())

        self.store.set_state("live_since_ms", str(self.live_since_ms))
        logger.info(
            "Modo live-only: solo señales desde %s (se ignora el histórico).",
            ms_to_iso(self.live_since_ms),
        )

    def _publish_snapshot(self, raw, bars, computed, events) -> None:
        """Actualiza el endpoint de series; un fallo no detiene el ciclo."""
        if self.snapshot is None:
            return
        try:
            frames = build_series_frames(
                self.strategy,
                raw,
                bars,
                computed,
                self.cfg,
                self.interval,
            )
            self.snapshot.publish(
                frames,
                self.cfg.timeframe,
                events_to_trades(events),
            )
        except Exception:  # noqa: BLE001 - el snapshot es accesorio al ciclo de señales
            logger.exception("No se pudo actualizar el snapshot de series")

    def process_once(self) -> int:
        """Lee datos cerrados, calcula acciones y persiste los eventos nuevos."""
        self._resolve_live_since()
        raw = self.feed.load_closed()
        if raw.empty:
            logger.debug("feed.db sin velas cerradas todavía.")
            return 0

        bars = resample(raw, self.cfg.timeframe)
        if len(bars) < self.min_bars:
            logger.info(
                "Insuficientes velas %s (%d < %d); esperando más datos.",
                self.cfg.timeframe,
                len(bars),
                self.min_bars,
            )
            return 0

        signals = self.strategy.generate_signals(bars, raw)
        adx_exit, weak_exit, rsi_div_exit = self.strategy.prepare_exits(raw, bars)
        events = build_events(
            bars,
            signals,
            adx_exit,
            weak_exit,
            rsi_div_exit,
            self.cfg,
            self.symbol,
            self.interval,
        )
        self._publish_snapshot(
            raw,
            bars,
            (signals, adx_exit, weak_exit, rsi_div_exit),
            events,
        )
        if self.live_since_ms is not None:
            events = [
                event
                for event in events
                if event["signal_time"] >= self.live_since_ms
            ]
        inserted = self.store.insert(events)
        if inserted:
            logger.info(
                "Publicados %d eventos nuevos (de %d acciones del backtest).",
                inserted,
                len(events),
            )
        return inserted

    def run(self, once: bool = False, poll_seconds: float = DEFAULT_POLL_SECONDS) -> None:
        while not STOP_EVENT.is_set():
            try:
                self.process_once()
            except FileNotFoundError as exc:
                logger.warning("%s", exc)
            except Exception:  # noqa: BLE001 - el proceso se recupera en el siguiente ciclo
                logger.exception("Error durante el ciclo del bot")

            touch_heartbeat()
            if once or STOP_EVENT.wait(poll_seconds):
                break


def request_stop(signum=None, frame=None) -> None:
    logger.info("Señal de parada recibida; cerrando bot…")
    STOP_EVENT.set()


def main() -> int:
    cfg = build_strategy_config()
    symbol = env_str("SYMBOL", DEFAULT_SYMBOL).upper()
    interval = env_str("INTERVAL", DEFAULT_INTERVAL)
    feed_path = resolve_path(env_str("DB_PATH", DEFAULT_FEED_DB))
    signal_path = resolve_path(env_str("SIGNAL_DB", DEFAULT_SIGNAL_DB))
    poll_seconds = env_float("POLL_SECONDS", DEFAULT_POLL_SECONDS)
    run_once = env_bool("BOT_RUN_ONCE", DEFAULT_RUN_ONCE)
    live_since_value = env_str("LIVE_SINCE", "").strip()
    live_since_ms = parse_live_since(live_since_value) if live_since_value else None
    live_only = env_bool("LIVE_ONLY", DEFAULT_LIVE_ONLY) or live_since_ms is not None
    http_host = env_str("HTTP_HOST", DEFAULT_HTTP_HOST)
    http_port = env_int("HTTP_PORT", DEFAULT_HTTP_PORT)

    logger.info(
        "Bot iniciado: symbol=%s interval=%s timeframe=%s feed=%s signal=%s",
        symbol,
        interval,
        cfg.timeframe,
        feed_path,
        signal_path,
    )

    feed = FeedReader(feed_path, symbol, interval)
    store = SignalStore(signal_path)
    snapshot = (
        SeriesSnapshot(symbol, interval)
        if http_port > 0 and HTTP_SERVER_AVAILABLE
        else None
    )
    http_server = None
    try:
        store.connect()
        if http_port > 0 and not HTTP_SERVER_AVAILABLE:
            logger.warning(
                "Módulo server no disponible; el endpoint HTTP de debug queda desactivado."
            )
        if snapshot is not None:
            try:
                http_server = start_server(snapshot, http_host, http_port)
            except OSError:
                logger.exception(
                    "No se pudo abrir el endpoint HTTP en %s:%d; "
                    "el bot sigue sin él.",
                    http_host,
                    http_port,
                )
                snapshot = None
        SignalBot(
            cfg,
            feed,
            store,
            symbol,
            interval,
            live_only=live_only,
            live_since_ms=live_since_ms,
            snapshot=snapshot,
        ).run(once=run_once, poll_seconds=poll_seconds)
    finally:
        if http_server is not None:
            http_server.shutdown()
            http_server.server_close()
        feed.close()
        store.close()

    return 0
