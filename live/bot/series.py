"""Preparación de series OHLCV e indicadores para visualización."""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategy.strategy import Config, EMACrossStrategy
from strategy.utils import resample_ohlcv, source_minutes

from .config import logger


def resample(raw: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Remuestrea OHLCV (copia si el origen ya es igual o superior)."""
    if raw.empty or pd.Timedelta(minutes=source_minutes(raw)) >= pd.Timedelta(timeframe):
        return raw.copy()
    return resample_ohlcv(raw, timeframe)


def series_frame(
    strategy: EMACrossStrategy,
    raw: pd.DataFrame,
    bars: pd.DataFrame,
    name: str,
    computed: tuple | None = None,
) -> pd.DataFrame:
    """OHLCV más las series calculadas para un timeframe."""
    frame = bars.copy()
    try:
        if computed is None:
            signals = strategy.generate_signals(bars, raw)
            exits = strategy.prepare_exits(raw, bars)
        else:
            signals, *exits = computed
        adx_exit, weak_exit, rsi_div_exit = exits
    except Exception:  # noqa: BLE001 - una serie opcional no detiene el bot
        logger.exception("No se pudieron calcular las series de %s", name)
        return frame

    for column in signals.columns:
        if column not in frame.columns:
            frame[column] = signals[column].to_numpy()
    for column, values in (
        ("adx_exit", adx_exit),
        ("weak_exit", weak_exit),
        ("rsi_div_exit", rsi_div_exit),
    ):
        if values is not None and len(values) == len(frame):
            frame[column] = np.asarray(values)
    return frame


def build_series_frames(
    strategy: EMACrossStrategy,
    raw: pd.DataFrame,
    bars: pd.DataFrame,
    computed: tuple,
    cfg: Config,
    interval: str,
) -> dict[str, tuple[list[str], pd.DataFrame]]:
    """Arma las series por timeframe para el endpoint de diagnóstico."""
    frames: dict[str, tuple[list[str], pd.DataFrame]] = {}

    def add(role: str, timeframe: str, make) -> None:
        if timeframe not in frames:
            frames[timeframe] = ([], make())
        frames[timeframe][0].append(role)

    add(
        "strategy",
        cfg.timeframe,
        lambda: series_frame(strategy, raw, bars, cfg.timeframe, computed),
    )
    add("raw", interval, lambda: series_frame(strategy, raw, raw, interval))

    adx_timeframes = cfg.adx_timeframes if cfg.adx_exit_enabled else ()
    weakening_timeframes = (
        cfg.ema_weakening_timeframes if cfg.ema_weakening_exit_enabled else ()
    )
    for role, timeframe in (
        *(("adx_exit", tf) for tf in adx_timeframes),
        *(("ema_weakening", tf) for tf in weakening_timeframes),
    ):
        if timeframe:
            add(
                role,
                timeframe,
                lambda tf=timeframe: series_frame(
                    strategy, raw, resample(raw, tf), tf
                ),
            )
    return frames
