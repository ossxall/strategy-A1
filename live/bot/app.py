"""Orquestación del bot de señales."""

from __future__ import annotations

import json
from pathlib import Path
import time

from strategy.strategy import Config, EMACrossStrategy

from .config import (
    DEFAULT_BACKTEST_REPORT_DIR,
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
from .events import build_events_and_result, events_to_trades
from .health import BotHealth, start_health_server
from .series import build_series_frames, resample
from .storage import FeedReader, SignalStore

try:
    from server import SeriesSnapshot
except ModuleNotFoundError as exc:
    if exc.name != "server":
        raise

    class SeriesSnapshot:
        """Placeholder para mantener el endpoint HTTP como dependencia opcional."""

        def __init__(self, *args, **kwargs):
            raise RuntimeError("El endpoint HTTP requiere el módulo server.")


# El endpoint de salud se implementa en el propio paquete y no depende del
# módulo opcional usado históricamente para servir series de depuración.
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
        health: BotHealth | None = None,
        backtest_report_dir: Path | None = None,
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
        self.health = health
        self.backtest_report_dir = backtest_report_dir
        self._last_report_signature = None
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

    def _publish_backtest_files(
        self, raw, bars, signals, adx_exit, weak_exit, rsi_div_exit, trades, equity
    ) -> None:
        """Actualiza los archivos del visor solo cuando llega una vela nueva."""
        if self.backtest_report_dir is None or bars.empty:
            return
        signature = (
            len(bars),
            bars.index[0].value,
            bars.index[-1].value,
            tuple(bars.iloc[-1].to_list()),
        )
        if (
            signature == self._last_report_signature
            and self._backtest_files_available()
        ):
            return
        try:
            from strategy.report import build_monthly_backtest_htmls

            build_monthly_backtest_htmls(
                bars,
                self.cfg,
                signals=signals,
                trades=trades,
                equity=equity,
                adx_exit=adx_exit,
                weak_exit=weak_exit,
                rsi_div_exit=rsi_div_exit,
                raw=raw,
                source=f"{self.symbol} live",
                output_dir=self.backtest_report_dir,
            )
        except Exception:  # noqa: BLE001 - los informes no detienen el ciclo live
            logger.exception("No se pudieron actualizar los archivos del backtest live")
            return
        self._last_report_signature = signature

    def _backtest_files_available(self) -> bool:
        """Comprueba que el último reporte publicado siga completo en disco."""
        html_dir = Path(self.backtest_report_dir) / "html"
        manifest_path = html_dir / "data" / "manifest.json"
        if not (html_dir / "backtest.html").is_file() or not manifest_path.is_file():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pages = manifest["pages"]
            data_dir = manifest_path.parent.resolve()
            for page in pages:
                page_path = (data_dir / page["file"]).resolve()
                page_path.relative_to(data_dir)
                if not page_path.is_file():
                    return False
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        return True

    def process_once(self) -> int:
        """Lee datos cerrados, calcula acciones y persiste los eventos nuevos."""
        self._resolve_live_since()
        raw = self.feed.load_closed()
        if self.health is not None:
            self.health.record_feed(raw)
        if raw.empty:
            logger.debug("feed.db sin velas cerradas todavía.")
            if self.health is not None:
                self.health.record_strategy_ready(False)
            return 0

        bars = resample(raw, self.cfg.timeframe)
        if len(bars) < self.min_bars:
            logger.info(
                "Insuficientes velas %s (%d < %d); esperando más datos.",
                self.cfg.timeframe,
                len(bars),
                self.min_bars,
            )
            if self.health is not None:
                self.health.record_strategy_ready(False)
            return 0
        if self.health is not None:
            self.health.record_strategy_ready(True)

        signals = self.strategy.generate_signals(bars, raw)
        adx_exit, weak_exit, rsi_div_exit = self.strategy.prepare_exits(raw, bars)
        events, trades, equity = build_events_and_result(
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
        self._publish_backtest_files(
            raw, bars, signals, adx_exit, weak_exit, rsi_div_exit, trades, equity
        )
        return inserted

    def run(self, once: bool = False, poll_seconds: float = DEFAULT_POLL_SECONDS) -> None:
        if self.health is not None:
            self.health.mark_running()
        try:
            while not STOP_EVENT.is_set():
                started = time.monotonic()
                if self.health is not None:
                    self.health.begin_cycle()
                try:
                    inserted = self.process_once()
                except FileNotFoundError as exc:
                    logger.warning("%s", exc)
                    if self.health is not None:
                        self.health.finish_cycle(
                            status="error",
                            duration_seconds=time.monotonic() - started,
                            error=str(exc),
                        )
                except Exception as exc:  # noqa: BLE001 - el proceso se recupera
                    logger.exception("Error durante el ciclo del bot")
                    if self.health is not None:
                        self.health.finish_cycle(
                            status="error",
                            duration_seconds=time.monotonic() - started,
                            error=str(exc),
                        )
                else:
                    if self.health is not None:
                        self.health.finish_cycle(
                            status="ok",
                            duration_seconds=time.monotonic() - started,
                            events_inserted=inserted,
                        )

                touch_heartbeat()
                if once or STOP_EVENT.wait(poll_seconds):
                    break
        finally:
            if self.health is not None:
                self.health.mark_stopped()


def request_stop(signum=None, frame=None) -> None:
    logger.info("Señal de parada recibida; cerrando bot…")
    STOP_EVENT.set()


def main() -> int:
    cfg = build_strategy_config()
    symbol = env_str("SYMBOL", DEFAULT_SYMBOL).upper()
    interval = env_str("INTERVAL", DEFAULT_INTERVAL)
    feed_path = resolve_path(env_str("DB_PATH", DEFAULT_FEED_DB))
    signal_path = resolve_path(env_str("SIGNAL_DB", DEFAULT_SIGNAL_DB))
    backtest_report_dir = resolve_path(
        env_str("BACKTEST_REPORT_DIR", DEFAULT_BACKTEST_REPORT_DIR)
    )
    poll_seconds = env_float("POLL_SECONDS", DEFAULT_POLL_SECONDS)
    run_once = env_bool("BOT_RUN_ONCE", DEFAULT_RUN_ONCE)
    live_since_value = env_str("LIVE_SINCE", "").strip()
    live_since_ms = parse_live_since(live_since_value) if live_since_value else None
    live_only = env_bool("LIVE_ONLY", DEFAULT_LIVE_ONLY) or live_since_ms is not None
    http_host = env_str("HTTP_HOST", DEFAULT_HTTP_HOST)
    http_port = env_int("HTTP_PORT", DEFAULT_HTTP_PORT)
    http_max_clients = env_int("HTTP_MAX_CLIENTS", 8)
    http_request_timeout = env_float("HTTP_REQUEST_TIMEOUT", 1.0)
    health_probe_interval = env_float("HEALTH_DB_PROBE_INTERVAL", 5.0)
    health_probe_timeout = env_float("HEALTH_DB_PROBE_TIMEOUT", 0.2)

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
    min_bars = cfg.ema_slow + 2
    health = BotHealth(
        symbol=symbol,
        interval=interval,
        timeframe=cfg.timeframe,
        feed_path=feed_path,
        signal_path=signal_path,
        poll_seconds=poll_seconds,
        min_bars=min_bars,
        live_only=live_only,
        live_since_ms=live_since_ms,
        probe_interval_seconds=health_probe_interval,
    )
    http_server = None
    http_thread = None
    probe_thread = None
    try:
        if http_port > 0:
            try:
                http_server, http_thread, probe_thread = start_health_server(
                    health,
                    http_host,
                    http_port,
                    signal_path,
                    max_clients=http_max_clients,
                    request_timeout=http_request_timeout,
                    probe_interval=health_probe_interval,
                    probe_timeout=health_probe_timeout,
                    backtest_dir=backtest_report_dir / "html",
                )
            except OSError:
                logger.exception(
                    "No se pudo abrir el endpoint de salud en %s:%d; "
                    "el bot sigue sin él.",
                    http_host,
                    http_port,
                )

        store.connect()
        SignalBot(
            cfg,
            feed,
            store,
            symbol,
            interval,
            min_bars=min_bars,
            live_only=live_only,
            live_since_ms=live_since_ms,
            health=health,
            backtest_report_dir=backtest_report_dir,
        ).run(once=run_once, poll_seconds=poll_seconds)
    finally:
        health.stop()
        if http_server is not None:
            http_server.shutdown()
            http_server.server_close()
        if http_thread is not None:
            http_thread.join(timeout=2)
        if probe_thread is not None:
            probe_thread.join(timeout=2)
        feed.close()
        store.close()

    return 0
