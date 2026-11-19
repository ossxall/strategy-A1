#!/usr/bin/env python3
"""
Strategy 2 — EMA Cross + MACD + ADX / EMA Weakening
============================================

Gestión de salida por defecto: take-profit parcial a 1R (50%) y stop a
breakeven del resto (`partial_tp_enabled`). Sube el win rate y recorta la
racha de pérdidas en whipsaw; puede desactivarse con `partial_tp_enabled=False`.

La salida por debilitamiento de las EMAs (`ema_weakening_exit_enabled`) cierra
`ema_weakening_exit_pct` % de la posición cuando el cruce de las EMAs del
timeframe `ema_weakening_timeframe` se estrecha por debajo de
`ema_weakening_gap_pct` (paso de separado a convergido). Sus periodos son
independientes de los del cruce de entrada: `ema_weakening_fast` /
`ema_weakening_slow` (0 = reutilizar `ema_fast` / `ema_slow`).

Y, por último, una **divergencia RSI en contra cierra el porcentaje configurado**
(`rsi_divergence_exit_pct`, 100 % por defecto): la divergencia bajista (precio
con máximos más altos, RSI con máximos más bajos) cierra un LONG y la alcista
(precio con mínimos más bajos, RSI con mínimos más altos) cierra un SHORT. El
pivote de precio se confirma tras `rsi_divergence_pivot` velas y se compara con
el pivote anterior del mismo tipo en `rsi_divergence_lookback` velas.

Archivo único con API reutilizable para backtesting y bots.

Protecciones de riesgo (ver `Config`):

* `entry_price_ema_filter_enabled` (default True): tras confirmarse un cruce
  alcista no se entra si el cierre queda por debajo de las EMAs rápida y lenta;
  tras un cruce bajista no se entra si queda por encima de ambas. Evita
  entradas contra el precio cuando el cruce ya se ha girado.
* `entry_gaussian_trend_enabled` (default True): exige que la línea principal del
  Gaussian Filter Trend esté alcista para LONG y bajista para SHORT.
* `gaussian_trend_reversal_exit_enabled` (default True): cierra parcialmente
  cuando la línea Gaussian gira contra la posición (`gaussian_trend_reversal_exit_pct`,
  25 % por defecto).
* `streak_trend_only` (default True): tras una posición cerrada en pérdida,
  la siguiente entrada sólo se abre si la señal coincide con el sesgo de
  tendencia de `trend_timeframe` (vela superior ya cerrada, sin lookahead).
  Sus periodos se configuran con `trend_ema_fast` / `trend_ema_slow` (0 = usar
  `ema_fast` / `ema_slow`). Corta las rachas de -1R del whipsaw sin recortar
  los cruces en tendencia.
* `monthly_loss_limit_pct` (default 5.0): si la equity cae más de X% por
  debajo de la equity al inicio del mes calendario se cierra la posición (opcional)
  y no se abren nuevas hasta el siguiente mes (o `monthly_loss_limit_days` días).
Uso:
    python run_backtest.py

Los parámetros de la estrategia se toman de los defaults de
`strategy.config.Config`; los de ejecución (dataset, salida y gráfico) están
hardcodeados como constantes al final de este módulo.

API para bots:
    cfg = Config()
    bars = load_parquet("dataset/binance/futures/um/BTCUSDT/merged/BTCUSDT-1m.parquet")
    strategy = EMACrossStrategy(cfg)
    signals = strategy.generate_signals(bars)
    # signals contiene señales conocidas al cierre de cada vela.
    # En ejecución real, actuar en la siguiente apertura/iteración.

Dependencias:
    pip install pandas==3.1.1 pyarrow==26.0.1 numpy==2.5.5 matplotlib==3.11.2 TA-Lib==0.8.3
"""
from __future__ import annotations
from dataclasses import asdict, replace
from pathlib import Path
from typing import Optional
from strategy.config import Config
from strategy.models import (
    Position,
    Trade,
    CROSS_REASON,
    MONTHLY_LIMIT_REASON,
    RSI_DIVERGENCE_BULL_REASON,
    RSI_DIVERGENCE_BEAR_REASON,
    GAUSSIAN_TREND_BULL_REASON,
    GAUSSIAN_TREND_BEAR_REASON,
    DEFAULT_OUTPUT_DIR,
    empty_trades,
)
from strategy.indicators import (
    ema,
    adx,
    atr_volatility_filter,
    gaussian_filter_trend,
    confirmed_crosses,
    align_closed_values,
    build_adx_exit,
    build_weakening_exit,
    build_rsi_divergence_flags,
    build_rsi_divergence_exit,
    spread_flags,
)
from strategy.utils import (
    load_parquet,
    resample_ohlcv,
    source_minutes,
    to_timeframe
)
from strategy.report import generate_report
import time
import talib
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Estrategia: compartida por backtesting y bots
# ---------------------------------------------------------------------------

class EMACrossStrategy:
    """Generador de señales sin estado de cartera ni ejecución de órdenes."""

    def __init__(self, config: Config):
        self.cfg = config

    def _upper_bars(self, bars: pd.DataFrame, timeframe: str) -> Optional[tuple[pd.DataFrame, str]]:
        """Velas del timeframe superior alineadas a ``bars`` (o ``None``)."""
        if pd.Timedelta(timeframe) <= pd.Timedelta(self.cfg.timeframe):
            upper, upper_tf = bars, self.cfg.timeframe
        else:
            try:
                upper, upper_tf = resample_ohlcv(bars, timeframe), timeframe
            except ValueError:
                return None
        if len(upper) < 2:
            return None
        return upper, upper_tf

    def _macd_line(self, bars: pd.DataFrame, raw: Optional[pd.DataFrame]) -> np.ndarray:
        """Calcula la línea MACD en su timeframe y la alinea sin lookahead a ``bars``."""
        cfg = self.cfg
        macd_timeframe = cfg.macd_timeframe or cfg.timeframe
        macd_delta = pd.Timedelta(macd_timeframe)
        strategy_delta = pd.Timedelta(cfg.timeframe)

        if macd_delta < strategy_delta:
            if raw is None or raw.empty:
                raise ValueError(
                    "macd_timeframe inferior a timeframe requiere datos raw de mayor resolución."
                )
            raw_delta = pd.Timedelta(minutes=source_minutes(raw))
            if raw_delta > macd_delta:
                raise ValueError(
                    f"Los datos raw ({raw_delta}) no tienen resolución suficiente para "
                    f"macd_timeframe={macd_timeframe}."
                )
            source = raw if raw_delta == macd_delta else resample_ohlcv(raw, macd_timeframe)
        elif macd_delta == strategy_delta:
            source = bars
        else:
            source = resample_ohlcv(bars, macd_timeframe)

        if len(source):
            close = np.ascontiguousarray(source.close.to_numpy(dtype=np.float64))
            macd_line, _, _ = talib.MACD(
                close,
                fastperiod=cfg.macd_fast,
                slowperiod=cfg.macd_slow,
                signalperiod=cfg.macd_signal,
            )
            macd_line = np.asarray(macd_line, dtype=float)
        else:
            macd_line = np.empty(0, dtype=float)

        if macd_delta == strategy_delta:
            return macd_line
        return align_closed_values(
            source.index,
            macd_line,
            bars.index,
            macd_timeframe,
            cfg.timeframe,
            fill=np.nan,
        )

    def _entry_adx_line(
        self, bars: pd.DataFrame, raw: Optional[pd.DataFrame], timeframe: str
    ) -> np.ndarray:
        """Calcula ADX de entrada en un timeframe y lo alinea sin lookahead."""
        cfg = self.cfg
        target_delta = pd.Timedelta(timeframe)
        strategy_delta = pd.Timedelta(cfg.timeframe)

        if target_delta == strategy_delta:
            source = bars
        elif target_delta > strategy_delta:
            source = resample_ohlcv(bars, timeframe)
        else:
            if raw is None or raw.empty:
                raise ValueError(
                    f"entry_adx_timeframe={timeframe} requiere datos raw de mayor resolución."
                )
            raw_delta = pd.Timedelta(minutes=source_minutes(raw))
            if raw_delta > target_delta:
                raise ValueError(
                    f"Los datos raw ({raw_delta}) no tienen resolución suficiente para "
                    f"entry_adx_timeframe={timeframe}."
                )
            source = raw if raw_delta == target_delta else resample_ohlcv(raw, timeframe)

        values = adx(source, cfg.entry_adx_len, cfg.entry_adx_len)["adx"].to_numpy(float)
        if target_delta == strategy_delta:
            return values
        return align_closed_values(
            source.index,
            values,
            bars.index,
            timeframe,
            cfg.timeframe,
            fill=np.nan,
        )

    def trend_bias(self, bars: pd.DataFrame) -> np.ndarray:
        """Sesgo de tendencia (+1 alcista, -1 bajista, 0 sin información).

        Calcula el sesgo en ``cfg.trend_timeframe`` y lo alinea a cada barra del
        timeframe de la estrategia usando **sólo la vela superior ya cerrada**
        (ver :func:`align_closed_values`), de modo que no hay lookahead. Se usa
        exclusivamente para exigir alineación tras una posición perdedora.
        """
        cfg = self.cfg
        if not cfg.streak_trend_only or not len(bars):
            return np.zeros(len(bars), int)
        upper = self._upper_bars(bars, cfg.trend_timeframe)
        if upper is None:
            return np.zeros(len(bars), int)
        bars_upper, upper_tf = upper
        trend_fast, trend_slow = cfg.trend_ema_lengths
        fast = ema(bars_upper.close, trend_fast).to_numpy(float)
        slow = ema(bars_upper.close, trend_slow).to_numpy(float)
        direction = np.sign(fast - slow)
        aligned = align_closed_values(bars_upper.index, direction, bars.index, upper_tf, cfg.timeframe)
        return np.where(np.isfinite(aligned), np.sign(aligned), 0.0).astype(int)

    def generate_signals(
        self, bars: pd.DataFrame, raw: Optional[pd.DataFrame] = None
    ) -> pd.DataFrame:
        cfg = self.cfg
        fast = ema(bars.close, cfg.ema_fast).to_numpy(float)
        slow = ema(bars.close, cfg.ema_slow).to_numpy(float)
        # Usar la línea principal MACD, no la línea de señal ni el histograma.
        macd_line = self._macd_line(bars, raw)
        prev_fast, prev_slow = np.r_[np.nan, fast[:-1]], np.r_[np.nan, slow[:-1]]
        raw_up = (fast > slow) & (prev_fast <= prev_slow)
        raw_down = (fast < slow) & (prev_fast >= prev_slow)
        up, down = raw_up.copy(), raw_down.copy()
        valid = np.isfinite(fast) & np.isfinite(slow) & np.isfinite(prev_fast) & np.isfinite(prev_slow)
        unconfirmed_up = np.zeros(len(bars), dtype=bool)
        unconfirmed_down = np.zeros(len(bars), dtype=bool)
        if cfg.confirm_bars:
            for raw_crosses, side_ok, rejected in (
                (raw_up, fast > slow, unconfirmed_up),
                (raw_down, fast < slow, unconfirmed_down),
            ):
                for cross_index in np.flatnonzero(raw_crosses):
                    end = cross_index + cfg.confirm_bars
                    if end >= len(bars) or not side_ok[cross_index : end + 1].all():
                        rejected[cross_index] = True
            up = confirmed_crosses(up, fast > slow, cfg.confirm_bars)
            down = confirmed_crosses(down, fast < slow, cfg.confirm_bars)
        candidate_up, candidate_down = up.copy(), down.copy()
        rejected_long_reasons: list[list[str]] = [[] for _ in range(len(bars))]
        rejected_short_reasons: list[list[str]] = [[] for _ in range(len(bars))]

        def record_rejection(long_mask: np.ndarray, short_mask: np.ndarray, reason: str) -> None:
            for index in np.flatnonzero(long_mask):
                rejected_long_reasons[index].append(reason)
            for index in np.flatnonzero(short_mask):
                rejected_short_reasons[index].append(reason)

        record_rejection(candidate_up & ~valid, candidate_down & ~valid, "indicadores no disponibles")
        record_rejection(
            unconfirmed_up, unconfirmed_down, "confirmación EMA no completada"
        )
        entry_adx_values = (
            adx(bars, cfg.entry_adx_len, cfg.entry_adx_len)
            if cfg.entry_adx_enabled
            else None
        )
        entry_adx_by_timeframe: dict[str, np.ndarray] = {}
        if cfg.entry_adx_enabled:
            for timeframe in cfg.entry_adx_timeframes:
                if pd.Timedelta(timeframe) == pd.Timedelta(cfg.timeframe):
                    values = entry_adx_values["adx"].to_numpy(float)
                else:
                    values = self._entry_adx_line(bars, raw, timeframe)
                entry_adx_by_timeframe[timeframe] = values
        # Alias histórico: conserva el ADX del primer timeframe configurado.
        entry_adx = (
            next(iter(entry_adx_by_timeframe.values()))
            if entry_adx_by_timeframe else np.full(len(bars), np.nan)
        )
        if cfg.entry_atr_enabled:
            atr_filter = atr_volatility_filter(
                bars,
                cfg.entry_atr_len,
                cfg.entry_atr_lookback,
                cfg.entry_atr_min_ratio,
                cfg.entry_atr_max_ratio,
            )
            entry_atr = atr_filter["atr"].to_numpy(float)
            entry_atr_mean = atr_filter["atr_mean"].to_numpy(float)
            entry_atr_ratio = atr_filter["atr_ratio"].to_numpy(float)
            entry_atr_ready = atr_filter["atr_filter_ready"].to_numpy(bool)
            atr_allowed = atr_filter["atr_filter_allowed"].to_numpy(bool)
        else:
            entry_atr = entry_atr_mean = entry_atr_ratio = np.full(len(bars), np.nan)
            entry_atr_ready = np.zeros(len(bars), dtype=bool)
            atr_allowed = np.ones(len(bars), dtype=bool)
        if cfg.entry_rsi_divergence_enabled:
            divergence = build_rsi_divergence_flags(raw, bars, cfg)
            entry_rsi_bull_divergence_blocked = spread_flags(
                divergence > 0 if divergence is not None else np.zeros(len(bars), bool),
                cfg.entry_rsi_divergence_lookback,
            )
            entry_rsi_bear_divergence_blocked = spread_flags(
                divergence < 0 if divergence is not None else np.zeros(len(bars), bool),
                cfg.entry_rsi_divergence_lookback,
            )
            # Divergencia alcista (+) anticipa subida → bloquea SHORT; bajista (-) anticipa caída → bloquea LONG.
            record_rejection(
                candidate_up & entry_rsi_bear_divergence_blocked,
                candidate_down & entry_rsi_bull_divergence_blocked,
                "divergencia RSI",
            )
            up &= ~entry_rsi_bear_divergence_blocked
            down &= ~entry_rsi_bull_divergence_blocked
        else:
            entry_rsi_bull_divergence_blocked = np.zeros(len(bars), dtype=bool)
            entry_rsi_bear_divergence_blocked = np.zeros(len(bars), dtype=bool)
        if cfg.entry_gaussian_trend_enabled or cfg.gaussian_trend_reversal_exit_enabled:
            gaussian_trend = gaussian_filter_trend(
                bars,
                cfg.entry_gaussian_length,
                cfg.entry_gaussian_poles,
                cfg.entry_gaussian_atr_length,
                cfg.entry_gaussian_fixed_multiplier,
                cfg.entry_gaussian_adaptive_width,
                cfg.entry_gaussian_efficiency_length,
                cfg.entry_gaussian_trend_multiplier,
                cfg.entry_gaussian_chop_multiplier,
                cfg.entry_gaussian_efficiency_smooth,
                cfg.entry_gaussian_source,
            )
            gaussian_trend_line = gaussian_trend["trend_line"].to_numpy(float)
            gaussian_trend_dir = gaussian_trend["trend_dir"].to_numpy(int)
        else:
            gaussian_trend_line = np.full(len(bars), np.nan)
            gaussian_trend_dir = np.zeros(len(bars), dtype=int)
        gaussian_long_allowed = (
            (gaussian_trend_dir == 1) if cfg.entry_gaussian_trend_enabled
            else np.ones(len(bars), dtype=bool)
        )
        gaussian_short_allowed = (
            (gaussian_trend_dir == -1) if cfg.entry_gaussian_trend_enabled
            else np.ones(len(bars), dtype=bool)
        )
        record_rejection(
            candidate_up & ~gaussian_long_allowed,
            candidate_down & ~gaussian_short_allowed,
            "Gaussian Filter Trend",
        )
        up &= gaussian_long_allowed
        down &= gaussian_short_allowed
        if cfg.entry_adx_enabled:
            allowed = np.ones(len(bars), dtype=bool)
            for values in entry_adx_by_timeframe.values():
                allowed &= np.isfinite(values) & (values > cfg.entry_adx_min)
            record_rejection(candidate_up & ~allowed, candidate_down & ~allowed, "ADX")
            up, down = up & allowed, down & allowed
        if cfg.entry_atr_enabled:
            record_rejection(
                candidate_up & ~atr_allowed,
                candidate_down & ~atr_allowed,
                "ATR fuera de rango",
            )
        up, down = up & atr_allowed, down & atr_allowed
        macd_up_allowed = macd_line > 0
        macd_down_allowed = macd_line < 0
        record_rejection(
            candidate_up & ~macd_up_allowed,
            candidate_down & ~macd_down_allowed,
            "MACD",
        )
        up, down = up & macd_up_allowed, down & macd_down_allowed
        if cfg.entry_price_ema_filter_enabled:
            # Tras un cruce alcista no se entra si el precio queda por debajo
            # de ambas EMAs; tras un bajista, no se entra si queda por encima.
            price = bars.close.to_numpy(float)
            long_price_blocked = (price < fast) & (price < slow)
            short_price_blocked = (price > fast) & (price > slow)
            record_rejection(
                candidate_up & long_price_blocked,
                candidate_down & short_price_blocked,
                "precio al lado incorrecto de las EMAs",
            )
            up &= ~long_price_blocked
            down &= ~short_price_blocked
        bias = self.trend_bias(bars) if cfg.streak_trend_only else None
        up &= valid
        down &= valid
        long_rejected = (candidate_up & ~up) | unconfirmed_up
        short_rejected = (candidate_down & ~down) | unconfirmed_down
        output = {
            "ema_fast": fast, "ema_slow": slow, "macd_line": macd_line,
            "adx_entry": entry_adx,
            "atr": entry_atr,
            "atr_mean": entry_atr_mean,
            "atr_ratio": entry_atr_ratio,
            "atr_filter_ready": entry_atr_ready,
            "atr_filter_allowed": atr_allowed,
            "entry_rsi_bull_divergence_blocked": entry_rsi_bull_divergence_blocked,
            "entry_rsi_bear_divergence_blocked": entry_rsi_bear_divergence_blocked,
            # Alias histórico del filtro LONG (ahora la divergencia bajista bloquea LONG).
            "entry_rsi_divergence_blocked": entry_rsi_bear_divergence_blocked,
            "entry_gaussian_trend_line": gaussian_trend_line,
            "entry_gaussian_trend_dir": gaussian_trend_dir,
            "entry_gaussian_long_allowed": gaussian_long_allowed,
            "entry_gaussian_short_allowed": gaussian_short_allowed,
            "cross_up_candidate": candidate_up | unconfirmed_up,
            "cross_down_candidate": candidate_down | unconfirmed_down,
            "cross_up_rejected": long_rejected,
            "cross_down_rejected": short_rejected,
            "entry_rejection_reasons_long": [
                "; ".join(reasons) for reasons in rejected_long_reasons
            ],
            "entry_rejection_reasons_short": [
                "; ".join(reasons) for reasons in rejected_short_reasons
            ],
            "cross_up": up, "cross_down": down, "valid": valid,
            "trend_bias": bias if bias is not None else np.zeros(len(bars), int),
            "target": np.select([up, down], [1, -1], default=0),
        }
        for timeframe, values in entry_adx_by_timeframe.items():
            output[f"adx_entry_{timeframe}"] = values
        return pd.DataFrame(output, index=bars.index)

    def prepare_exits(self, raw: pd.DataFrame, bars: pd.DataFrame) -> tuple[
            Optional[np.ndarray], Optional[np.ndarray], Optional[np.ndarray]]:
        """Señales existentes: (ADX reversal, EMA weakening, divergencia RSI)."""
        return (
            build_adx_exit(raw, bars, self.cfg),
            build_weakening_exit(raw, bars, self.cfg),
            build_rsi_divergence_exit(raw, bars, self.cfg),
        )

# ---------------------------------------------------------------------------
# Motor de ejecución
# ---------------------------------------------------------------------------

class Backtester:
    """Motor de ejecución.

    Las señales se evalúan al cierre del timeframe de estrategia y se ejecutan
    en la apertura siguiente. Si se proporciona ``intrabar_bars`` de 1m, sólo
    se usa para recorrer stops y take-profits dentro de cada vela de estrategia.
    """

    def __init__(self, bars: pd.DataFrame, config: Config, signals: Optional[pd.DataFrame] = None,
                 adx_exit: Optional[np.ndarray] = None, weak_exit: Optional[np.ndarray] = None,
                 rsi_div_exit: Optional[np.ndarray] = None,
                 intrabar_bars: Optional[pd.DataFrame] = None,
                 process_intrabar_tail: bool = False,
                 intrabar_timeframe: Optional[str] = None):
        self.bars, self.cfg = bars, config
        self.signals = signals if signals is not None else EMACrossStrategy(config).generate_signals(bars)
        self._intrabar_bars: Optional[pd.DataFrame] = None
        self._intrabar_slices: list[tuple[int, int, bool]] = []
        self._intrabar_o = self._intrabar_h = self._intrabar_l = None
        self._intrabar_c = None
        self._intrabar_times_ns: Optional[np.ndarray] = None
        self._intrabar_timeframe = intrabar_timeframe
        self._intrabar_tail_last: tuple[pd.Timestamp, float] | None = None
        self.process_intrabar_tail = process_intrabar_tail
        # Sólo conserva timestamps de salidas intrabar reales, no todos los
        # minutos recorridos (el histórico puede contener millones).
        self.intrabar_exit_times: set[pd.Timestamp] = set()
        if intrabar_bars is not None and not intrabar_bars.empty:
            self._prepare_intrabar_bars(intrabar_bars)
        if config.entry_atr_enabled:
            if "atr_filter_allowed" in self.signals:
                self.entry_atr_allowed = self.signals["atr_filter_allowed"].to_numpy(bool)
            else:
                # También imponer el filtro al usar señales personalizadas en
                # Backtester, que podrían no venir de EMACrossStrategy.
                self.entry_atr_allowed = atr_volatility_filter(
                    bars,
                    config.entry_atr_len,
                    config.entry_atr_lookback,
                    config.entry_atr_min_ratio,
                    config.entry_atr_max_ratio,
                )["atr_filter_allowed"].to_numpy(bool)
        else:
            self.entry_atr_allowed = np.ones(len(bars), dtype=bool)
        if config.entry_rsi_divergence_enabled:
            divergence = None
            if ("entry_rsi_bull_divergence_blocked" not in self.signals
                    or "entry_rsi_bear_divergence_blocked" not in self.signals):
                divergence = build_rsi_divergence_flags(None, bars, config)
            if "entry_rsi_bull_divergence_blocked" in self.signals:
                self.entry_rsi_bull_divergence_blocked = self.signals[
                    "entry_rsi_bull_divergence_blocked"
                ].to_numpy(bool)
            else:
                self.entry_rsi_bull_divergence_blocked = spread_flags(
                    divergence > 0 if divergence is not None else np.zeros(len(bars), bool),
                    config.entry_rsi_divergence_lookback,
                )
            if "entry_rsi_bear_divergence_blocked" in self.signals:
                self.entry_rsi_bear_divergence_blocked = self.signals[
                    "entry_rsi_bear_divergence_blocked"
                ].to_numpy(bool)
            elif "entry_rsi_divergence_blocked" in self.signals:
                self.entry_rsi_bear_divergence_blocked = self.signals[
                    "entry_rsi_divergence_blocked"
                ].to_numpy(bool)
            else:
                self.entry_rsi_bear_divergence_blocked = spread_flags(
                    divergence < 0 if divergence is not None else np.zeros(len(bars), bool),
                    config.entry_rsi_divergence_lookback,
                )
        else:
            self.entry_rsi_bull_divergence_blocked = np.zeros(len(bars), dtype=bool)
            self.entry_rsi_bear_divergence_blocked = np.zeros(len(bars), dtype=bool)
        if "entry_gaussian_trend_dir" in self.signals:
            self.entry_gaussian_trend_dir = self.signals[
                "entry_gaussian_trend_dir"
            ].to_numpy(int)
        elif (config.entry_gaussian_trend_enabled
              or config.gaussian_trend_reversal_exit_enabled):
            gaussian_trend = gaussian_filter_trend(
                bars,
                config.entry_gaussian_length,
                config.entry_gaussian_poles,
                config.entry_gaussian_atr_length,
                config.entry_gaussian_fixed_multiplier,
                config.entry_gaussian_adaptive_width,
                config.entry_gaussian_efficiency_length,
                config.entry_gaussian_trend_multiplier,
                config.entry_gaussian_chop_multiplier,
                config.entry_gaussian_efficiency_smooth,
                config.entry_gaussian_source,
            )
            self.entry_gaussian_trend_dir = gaussian_trend["trend_dir"].to_numpy(int)
        else:
            self.entry_gaussian_trend_dir = np.zeros(len(bars), dtype=int)
        previous_gaussian_dir = np.zeros(
            len(self.entry_gaussian_trend_dir), dtype=int
        )
        if len(previous_gaussian_dir) > 1:
            previous_gaussian_dir[1:] = self.entry_gaussian_trend_dir[:-1]
        self.gaussian_trend_turned_bullish = (
            (self.entry_gaussian_trend_dir == 1) & (previous_gaussian_dir == -1)
        )
        self.gaussian_trend_turned_bearish = (
            (self.entry_gaussian_trend_dir == -1) & (previous_gaussian_dir == 1)
        )
        self.adx_exit = np.asarray(adx_exit, bool) if adx_exit is not None else np.zeros(len(bars), bool)
        self.weak_exit = np.asarray(weak_exit, bool) if weak_exit is not None else np.zeros(len(bars), bool)
        # Divergencia RSI: +1 alcista (cierra SHORT), -1 bajista (cierra LONG), 0 sin señal.
        self.rsi_div = np.asarray(rsi_div_exit, int) if rsi_div_exit is not None else np.zeros(len(bars), int)
        self.ema_slow_line = (
            self.signals["ema_slow"].to_numpy(float)
            if "ema_slow" in self.signals
            else ema(bars.close, config.ema_slow).to_numpy(float)
        )
        if len(self.adx_exit) != len(bars): self.adx_exit = np.zeros(len(bars), bool)
        if len(self.weak_exit) != len(bars): self.weak_exit = np.zeros(len(bars), bool)
        if len(self.rsi_div) != len(bars): self.rsi_div = np.zeros(len(bars), int)
        self.o, self.h, self.l, self.c = (bars[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        self.times = bars.index
        self.cash = config.initial_capital
        self.position: Optional[Position] = None
        self.pending_target: Optional[int] = None
        self.pending_exit: Optional[tuple[str, float]] = None
        self.trades: list[Trade] = []
        # Aperturas para publicar eventos en vivo.
        self.opened: list[dict] = []
        self.trade_id = self.position_id = 0
        self.eq_time, self.eq_cash, self.eq_value, self.eq_side, self.eq_qty = [], [], [], [], []
        self.eq_signal_time, self.eq_signal_target, self.eq_execution_rejection = [], [], []
        self.eq_execution_rejection_time, self.eq_execution_rejection_target = [], []
        self.eq_stop_price, self.eq_entry_price, self.eq_position_id = [], [], []
        self.eq_breakeven_activated, self.eq_breakeven_price = [], []
        self._breakeven_activated_this_bar = False
        self._breakeven_activation_price = np.nan
        self._stop_trigger_price_this_bar = np.nan
        # Estado del límite mensual y pnl acumulado de la posición abierta.
        self.halt_until: Optional[pd.Timestamp] = None
        self._bar_index = -1
        self.streak_in_loss = False
        self._position_pnl = 0.0

    def _set_intrabar_source(self, intrabar_bars: pd.DataFrame) -> pd.Timedelta:
        """Valida y carga la fuente intrabar declarada como velas de 1m."""
        if not isinstance(intrabar_bars.index, pd.DatetimeIndex):
            raise ValueError("intrabar_bars debe tener un DatetimeIndex.")
        if not intrabar_bars.index.is_monotonic_increasing or intrabar_bars.index.has_duplicates:
            raise ValueError("intrabar_bars debe estar ordenado y no contener timestamps duplicados.")
        missing = [column for column in ("open", "high", "low", "close") if column not in intrabar_bars]
        if missing:
            raise ValueError(f"Faltan columnas OHLC en intrabar_bars: {missing}.")

        # En live, el intervalo configurado es la fuente de verdad. La mediana
        # de diferencias no sirve para inferir resolución cuando hay huecos.
        source_delta = (
            pd.Timedelta(self._intrabar_timeframe)
            if self._intrabar_timeframe is not None
            else pd.Timedelta(minutes=source_minutes(intrabar_bars))
        )
        if source_delta != pd.Timedelta("1min"):
            raise ValueError(
                "intrabar_bars debe tener resolución de 1m "
                f"(resolución declarada/detectada: {source_delta})."
            )
        source_ns = intrabar_bars.index.as_unit("ns").asi8
        if np.any(source_ns % source_delta.value):
            raise ValueError("Los timestamps intrabar deben estar alineados al minuto.")

        self._intrabar_bars = intrabar_bars
        self._intrabar_o = intrabar_bars["open"].to_numpy(float)
        self._intrabar_h = intrabar_bars["high"].to_numpy(float)
        self._intrabar_l = intrabar_bars["low"].to_numpy(float)
        self._intrabar_c = intrabar_bars["close"].to_numpy(float)
        self._intrabar_times_ns = source_ns
        return source_delta

    def _prepare_intrabar_bars(self, intrabar_bars: pd.DataFrame) -> None:
        """Indexa velas 1m por vela de estrategia; nunca las usa para señales."""
        source_delta = self._set_intrabar_source(intrabar_bars)
        strategy_delta = pd.Timedelta(self.cfg.timeframe)
        if strategy_delta <= source_delta:
            # Si la estrategia ya corre en 1m (o menos), no hay timeframe
            # intrabar más fino que aplicar.
            self.release_intrabar_source()
            return
        if strategy_delta.value % source_delta.value:
            raise ValueError(
                "timeframe debe ser un múltiplo entero de 1m para la ejecución intrabar."
            )

        source_ns = self._intrabar_times_ns
        strategy_ns = self.bars.index.as_unit("ns").asi8
        source_minute_ns = source_delta.value
        expected = int(strategy_delta.value // source_minute_ns)
        self._intrabar_slices = []
        for start_ns in strategy_ns:
            start_ns = int(start_ns)
            end_ns = start_ns + strategy_delta.value
            left = int(np.searchsorted(source_ns, start_ns, side="left"))
            right = int(np.searchsorted(source_ns, end_ns, side="left"))
            # Una ventana incompleta usa el OHLC agregado de ``bars`` como
            # fallback. Así no se ignoran stops por falta de minutos fuente.
            complete = right - left == expected
            if complete:
                minute_times = source_ns[left:right]
                complete = (
                    minute_times[0] == start_ns
                    and minute_times[-1] == end_ns - source_minute_ns
                    and bool(np.all(np.diff(minute_times) == source_minute_ns))
                )
            self._intrabar_slices.append((left, right, complete))

    def release_intrabar_source(self) -> None:
        """Libera arrays 1m tras construir el baseline de velas cerradas."""
        self._intrabar_bars = None
        self._intrabar_o = self._intrabar_h = self._intrabar_l = None
        self._intrabar_c = None
        self._intrabar_times_ns = None
        self._intrabar_slices = []

    def fork_for_intrabar_tail(
        self,
        intrabar_bars: pd.DataFrame,
        intrabar_timeframe: Optional[str] = None,
    ) -> "Backtester":
        """Clona sólo el estado mutable para reproducir el tail 1m actual."""
        from copy import copy

        fork = copy(self)
        fork.position = replace(self.position) if self.position is not None else None
        for name in (
            "trades",
            "opened",
            "eq_time",
            "eq_cash",
            "eq_value",
            "eq_side",
            "eq_qty",
            "eq_signal_time",
            "eq_signal_target",
            "eq_execution_rejection",
            "eq_execution_rejection_time",
            "eq_execution_rejection_target",
            "eq_stop_price",
            "eq_entry_price",
            "eq_position_id",
            "eq_breakeven_activated",
            "eq_breakeven_price",
        ):
            setattr(fork, name, list(getattr(self, name)))
        fork.intrabar_exit_times = set(self.intrabar_exit_times)
        fork._intrabar_slices = []
        fork._intrabar_tail_last = None
        fork._breakeven_activated_this_bar = self._breakeven_activated_this_bar
        fork._breakeven_activation_price = self._breakeven_activation_price
        fork._stop_trigger_price_this_bar = self._stop_trigger_price_this_bar
        fork.process_intrabar_tail = True
        if intrabar_timeframe is not None:
            fork._intrabar_timeframe = intrabar_timeframe
        fork._set_intrabar_source(intrabar_bars)
        return fork

    def _price(self, price: float, side: int) -> float:
        slip = self.cfg.slippage_bps / 10_000
        return price * (1 + slip if side == 1 else 1 - slip)

    def _fee(self, notional: float) -> float:
        return abs(notional) * self.cfg.commission_bps / 10_000

    def _stop_price(self, side: int, entry: float, pct: float) -> float:
        return entry * (1 - pct if side == 1 else 1 + pct)

    def _risk_per_unit(self, side: int, entry: float, pct: float) -> float:
        stop_exec = self._price(self._stop_price(side, entry, pct), -side)
        return abs(stop_exec - entry) + (entry + abs(stop_exec)) * self.cfg.commission_bps / 10_000

    def open_position(self, side: int, raw_price: float, ts: pd.Timestamp):
        if self.position is not None or self.cash <= 0:
            return
        price = self._price(raw_price, side)
        if not np.isfinite(price) or price <= 0:
            return
        sizing_equity = self.cash
        pct_sl = self.cfg.stop_loss_pct
        risk_unit = self._risk_per_unit(side, price, pct_sl) if pct_sl > 0 else 0.0
        sizing = self.entry_sizing_context(
            side,
            sizing_equity=sizing_equity,
        )
        qty_by_risk = (
            self.cash * self.cfg.risk_pct / risk_unit
            if risk_unit > 0 else self.cash / price
        )
        # La estrategia es cash-only: limita siempre el nominal a su equity.
        # El leverage de Binance pertenece exclusivamente al executor.
        equity_quantity_cap = sizing_equity / price
        qty_total = min(qty_by_risk, equity_quantity_cap)
        if not np.isfinite(qty_total) or qty_total <= 0:
            return
        fee = self._fee(price * qty_total)
        self.cash -= fee
        self.position_id += 1
        self._position_pnl = 0.0
        self.position = Position(
            position_id=self.position_id,
            side=side,
            quantity=qty_total,
            initial_quantity=qty_total,  # cantidad completa para riesgo y prorrateo de fees
            entry_price=price,
            entry_time=ts,
            entry_fee=fee,
            stop_pct=pct_sl,
            risk_amount=qty_total * risk_unit if risk_unit > 0 else 0.0,
            breakeven=False,
            partial_done=False,
            partial2_done=False,
            partial3_done=False,
            partial4_done=False,
            partial5_done=False,
            partial6_done=False,
            partial7_done=False,
        )
        self.opened.append({
            "position_id": self.position_id, "time": ts, "price": price, "side": side,
            "quantity": qty_total,
            "sizing_equity": sizing_equity,
            "risk_unit": risk_unit,
            **sizing,
        })

    def close_position(self, percent: float, raw_price: float, ts: pd.Timestamp, reason: str):
        pos = self.position
        if pos is None:
            return
        if not 0 < percent <= 100:
            raise ValueError("El porcentaje de cierre debe estar en (0, 100].")
        qty = pos.quantity * percent / 100
        side = pos.side
        price = self._price(raw_price, -side)
        gross = (price - pos.entry_price) * qty * side
        exit_fee = self._fee(price * qty)
        # La comisión de entrada se distribuye proporcionalmente a la cantidad original restante.
        entry_fee = pos.entry_fee * (qty / pos.quantity)
        fees = entry_fee + exit_fee
        net = gross - fees
        self.cash += gross - exit_fee
        self._position_pnl += net
        self.trade_id += 1
        self.trades.append(Trade(self.trade_id, pos.position_id, "LONG" if side == 1 else "SHORT",
            pos.entry_time, ts, pos.entry_price, price, qty, gross, fees, net,
            net / (pos.entry_price * qty) * 100 if pos.entry_price * qty else 0.0,
            reason, pos.risk_amount,
            position_initial_quantity=pos.initial_quantity,
        ))
        remaining = pos.quantity - qty
        if percent >= 100 or remaining <= max(1e-12, pos.initial_quantity * 1e-12):
            self.position = None
            self._close_streak()
        else:
            pos.quantity = remaining
            pos.entry_fee -= entry_fee

    def entry_sizing_context(
        self,
        side: int,
        *,
        sizing_equity: float | None = None,
    ) -> dict[str, float]:
        """Devuelve los parámetros fijos de riesgo para una intención ENTRY."""
        if side not in (1, -1):
            raise ValueError("side debe ser 1 (LONG) o -1 (SHORT)")
        return {
            "risk_pct": float(self.cfg.risk_pct),
            "stop_loss_pct": float(self.cfg.stop_loss_pct),
            "commission_bps": float(self.cfg.commission_bps),
            "slippage_bps": float(self.cfg.slippage_bps),
            "sizing_equity": float(
                self.cash if sizing_equity is None else sizing_equity
            ),
        }

    def _close_streak(self) -> None:
        """Actualiza el filtro de tendencia tras una pérdida."""
        won = self._position_pnl >= 0
        self.streak_in_loss = not won
        self._position_pnl = 0.0

    def entries_blocked(self) -> bool:
        """``True`` si está prohibido abrir posiciones nuevas en esta vela."""
        return self.halt_until is not None and self.times[self._bar_index] < self.halt_until

    def _risk_unit(self, pos: Position) -> float:
        return pos.risk_amount / pos.initial_quantity if pos.initial_quantity else 0.0

    def _remember_intrabar_exits(self, first_trade: int) -> None:
        for trade in self.trades[first_trade:]:
            self.intrabar_exit_times.add(trade.exit_time)

    def _rsi_divergence_reason(self, i: int) -> Optional[str]:
        """Motivo de salida total si hay divergencia RSI en contra de la posición.

        La divergencia **bajista** de la vela ``i`` (``self.rsi_div[i] == -1``:
        precio con máximos más altos, RSI con máximos más bajos) cierra un LONG
        y la **alcista** (``== 1``: precio con mínimos más bajos, RSI con
        mínimos más altos) cierra un SHORT. Devuelve el motivo (``RSI_DIVERGENCE_*``)
        o ``None`` si no hay divergencia contra la posición abierta.
        """
        pos = self.position
        if pos is None or i >= len(self.rsi_div):
            return None
        flag = int(self.rsi_div[i])
        if flag < 0 and pos.side == 1:
            return RSI_DIVERGENCE_BEAR_REASON
        if flag > 0 and pos.side == -1:
            return RSI_DIVERGENCE_BULL_REASON
        return None

    def _tp3_slow_ema_touch_exit(self, i: int) -> bool:
        """Cierra el SHORT restante si la vela cierra sobre la EMA lenta tras TP3."""
        pos = self.position
        if (
            not self.cfg.tp3_ema_slow_exit_enabled
            or pos is None
            or pos.side != -1
            or not pos.partial3_done
            or i >= len(self.ema_slow_line)
        ):
            return False
        line = self.ema_slow_line[i]
        if not np.isfinite(line):
            return False
        trigger = line * (1.0 + self.cfg.tp3_ema_slow_penetration_pct / 100.0)
        if self.c[i] < trigger:
            return False
        self.close_position(
            100,
            self.c[i],
            self.times[i] + pd.Timedelta(self.cfg.timeframe),
            "EMA_SLOW_TOUCH_SHORT_AFTER_TP3",
        )
        return True

    def _manage(
        self,
        i: int,
        *,
        open_price: float | None = None,
        high: float | None = None,
        low: float | None = None,
        timestamp: pd.Timestamp | None = None,
    ) -> None:
        """Gestiona take-profits parciales y el stop a breakeven."""
        cfg, pos = self.cfg, self.position
        if pos is None:
            return
        if not cfg.partial_tp_enabled:
            return
        open_price = self.o[i] if open_price is None else open_price
        high = self.h[i] if high is None else high
        low = self.l[i] if low is None else low
        timestamp = self.times[i] if timestamp is None else timestamp
        risk_unit = self._risk_unit(pos)
        if risk_unit <= 0:
            return
        pos_after = pos
        # Objetivos de mayor distancia primero para conservar un solo parcial
        # por vela cuando el rango alcanza varios niveles.
        if cfg.partial_tp7_r > 0 and not pos.partial7_done:
            trigger7 = pos.entry_price + pos.side * cfg.partial_tp7_r * risk_unit
            hit7 = high >= trigger7 if pos.side == 1 else low <= trigger7
            if hit7:
                fill7 = max(trigger7, open_price) if pos.side == 1 else min(trigger7, open_price)
                self.close_position(cfg.partial_tp7_pct, fill7, timestamp, f"PARTIAL_TP7_{cfg.partial_tp7_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial7_done = True
                return
        if cfg.partial_tp6_r > 0 and not pos.partial6_done:
            trigger6 = pos.entry_price + pos.side * cfg.partial_tp6_r * risk_unit
            hit6 = high >= trigger6 if pos.side == 1 else low <= trigger6
            if hit6:
                fill6 = max(trigger6, open_price) if pos.side == 1 else min(trigger6, open_price)
                self.close_position(cfg.partial_tp6_pct, fill6, timestamp, f"PARTIAL_TP6_{cfg.partial_tp6_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial6_done = True
                return
        if cfg.partial_tp5_r > 0 and not pos.partial5_done:
            trigger5 = pos.entry_price + pos.side * cfg.partial_tp5_r * risk_unit
            hit5 = high >= trigger5 if pos.side == 1 else low <= trigger5
            if hit5:
                fill5 = max(trigger5, open_price) if pos.side == 1 else min(trigger5, open_price)
                self.close_position(cfg.partial_tp5_pct, fill5, timestamp, f"PARTIAL_TP5_{cfg.partial_tp5_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial5_done = True
                return
        if cfg.partial_tp4_r > 0 and not pos.partial4_done:
            trigger4 = pos.entry_price + pos.side * cfg.partial_tp4_r * risk_unit
            hit4 = high >= trigger4 if pos.side == 1 else low <= trigger4
            if hit4:
                fill4 = max(trigger4, open_price) if pos.side == 1 else min(trigger4, open_price)
                self.close_position(cfg.partial_tp4_pct, fill4, timestamp, f"PARTIAL_TP4_{cfg.partial_tp4_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial4_done = True
                return
        if cfg.partial_tp3_r > 0 and not pos.partial3_done:
            trigger3 = pos.entry_price + pos.side * cfg.partial_tp3_r * risk_unit
            hit3 = high >= trigger3 if pos.side == 1 else low <= trigger3
            if hit3:
                fill3 = max(trigger3, open_price) if pos.side == 1 else min(trigger3, open_price)
                self.close_position(cfg.partial_tp3_pct, fill3, timestamp, f"PARTIAL_TP3_{cfg.partial_tp3_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial3_done = True
                return
        # TP2 primero si no hecho y trigger alcanzado (mayor distancia)
        if cfg.partial_tp2_r > 0 and not pos.partial2_done:
            trigger2 = pos.entry_price + pos.side * cfg.partial_tp2_r * risk_unit
            hit2 = high >= trigger2 if pos.side == 1 else low <= trigger2
            if hit2:
                fill2 = max(trigger2, open_price) if pos.side == 1 else min(trigger2, open_price)
                self.close_position(cfg.partial_tp2_pct, fill2, timestamp, f"PARTIAL_TP2_{cfg.partial_tp2_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial2_done = True
                return
        # TP1
        if not pos.partial_done:
            trigger = pos.entry_price + pos.side * cfg.partial_tp_r * risk_unit
            hit = high >= trigger if pos.side == 1 else low <= trigger
            if hit:
                fill = max(trigger, open_price) if pos.side == 1 else min(trigger, open_price)
                self.close_position(cfg.partial_tp_pct, fill, timestamp, f"PARTIAL_TP_{cfg.partial_tp_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial_done = True
                    if cfg.move_stop_to_breakeven:
                        if not pos_after.breakeven:
                            pos_after.breakeven = True
                            self._breakeven_activated_this_bar = True
                            self._breakeven_activation_price = pos_after.entry_price
    def _stop(
        self,
        i: int,
        *,
        open_price: float | None = None,
        high: float | None = None,
        low: float | None = None,
        timestamp: pd.Timestamp | None = None,
    ) -> bool:
        pos = self.position
        if pos is None or pos.stop_pct <= 0: return False
        open_price = self.o[i] if open_price is None else open_price
        high = self.h[i] if high is None else high
        low = self.l[i] if low is None else low
        timestamp = self.times[i] if timestamp is None else timestamp
        stop = pos.entry_price if pos.breakeven else self._stop_price(pos.side, pos.entry_price, pos.stop_pct)
        hit = low <= stop if pos.side == 1 else high >= stop
        if not hit: return False
        self._stop_trigger_price_this_bar = stop
        fill = min(open_price, stop) if pos.side == 1 else max(open_price, stop)
        reason = "BREAKEVEN" if pos.breakeven else f"STOP_LOSS_{pos.stop_pct*100:.2f}%"
        self.close_position(100, fill, timestamp, reason)
        return True

    def _execute_target(
        self,
        i: int,
        target: int,
        signal_bias: int = 0,
        *,
        open_price: float | None = None,
        timestamp: pd.Timestamp | None = None,
    ) -> str | None:
        if self.position is not None and self.position.side != target:
            self.close_position(
                100,
                self.o[i] if open_price is None else open_price,
                self.times[i] if timestamp is None else timestamp,
                CROSS_REASON,
            )
        if self.position is None:
            # Cerrar la posición revertida puede convertir esta señal en la
            # primera entrada tras una pérdida. En ese caso el filtro debe
            # aplicarse con el sesgo conocido al cierre que generó la señal.
            if (self.cfg.streak_trend_only and self.streak_in_loss
                    and signal_bias != target):
                return f"filtro de tendencia (sesgo {signal_bias:+d})"
            self.open_position(
                target,
                self.o[i] if open_price is None else open_price,
                self.times[i] if timestamp is None else timestamp,
            )
        return None

    def _replay_intrabar_tail(self) -> None:
        """Procesa los minutos cerrados posteriores a la última vela completa.

        En live, el bucket actual del timeframe de estrategia todavía no está
        en ``bars``. Se recorren los minutos disponibles sin esperar a completar
        el bucket y sin usar su OHLC parcial como fallback.
        """
        if (
            not self.process_intrabar_tail
            or self._intrabar_bars is None
            or self._intrabar_times_ns is None
            or not len(self.bars)
        ):
            return

        tail_start = self.times[-1] + pd.Timedelta(self.cfg.timeframe)
        left = int(
            np.searchsorted(
                self._intrabar_times_ns,
                pd.Timestamp(tail_start).as_unit("ns").value,
                side="left",
            )
        )
        right = len(self._intrabar_times_ns)
        if left >= right:
            return

        self._bar_index = len(self.bars) - 1
        first_time = self._intrabar_bars.index[left]
        if self.pending_target is not None:
            signal_bias = int(self.signals["trend_bias"].iloc[-1])
            self._execute_target(
                self._bar_index,
                self.pending_target,
                signal_bias,
                open_price=self._intrabar_o[left],
                timestamp=first_time,
            )
            self.pending_target = None
            self.pending_exit = None
        elif self.pending_exit is not None:
            reason, percent = self.pending_exit
            self.close_position(
                percent,
                self._intrabar_o[left],
                first_time,
                reason,
            )
            self.pending_exit = None

        for minute in range(left, right):
            minute_time = self._intrabar_bars.index[minute]
            first_trade = len(self.trades)
            stop_hit = self._stop(
                self._bar_index,
                open_price=self._intrabar_o[minute],
                high=self._intrabar_h[minute],
                low=self._intrabar_l[minute],
                timestamp=minute_time,
            )
            if stop_hit:
                self._remember_intrabar_exits(first_trade)
                break
            self._manage(
                self._bar_index,
                open_price=self._intrabar_o[minute],
                high=self._intrabar_h[minute],
                low=self._intrabar_l[minute],
                timestamp=minute_time,
            )
            self._remember_intrabar_exits(first_trade)
        self._intrabar_tail_last = (
            self._intrabar_bars.index[right - 1],
            float(self._intrabar_c[right - 1]),
        )
        if not self.cfg.force_close_at_end:
            last_time, last_close = self._intrabar_tail_last
            self._mark(
                last_time + pd.Timedelta("1min"),
                last_close,
                signal_time=last_time,
            )

    def run_intrabar_tail(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Ejecuta sólo los minutos posteriores al checkpoint de barras cerradas."""
        self._replay_intrabar_tail()
        trades = (
            pd.DataFrame([asdict(trade) for trade in self.trades])
            if self.trades else empty_trades()
        )
        equity = pd.DataFrame({
            "timestamp": self.eq_time,
            "cash": self.eq_cash,
            "equity": self.eq_value,
            "position_side": self.eq_side,
            "position_quantity": self.eq_qty,
            "signal_time": self.eq_signal_time,
            "signal_target": self.eq_signal_target,
            "execution_rejection": self.eq_execution_rejection,
            "execution_rejection_time": self.eq_execution_rejection_time,
            "execution_rejection_target": self.eq_execution_rejection_target,
            "effective_stop_price": self.eq_stop_price,
            "position_entry_price": self.eq_entry_price,
            "position_id": self.eq_position_id,
            "breakeven_activated": self.eq_breakeven_activated,
            "breakeven_price": self.eq_breakeven_price,
        })
        return trades, equity

    def _equity_value(self, close: float) -> float:
        """Equity mark-to-market al precio ``close`` (caja + posición a precio de liquidación)."""
        pos = self.position
        value = self.cash
        if pos is not None:
            liq = self._price(close, -pos.side)
            value += (liq - pos.entry_price) * pos.quantity * pos.side - self._fee(liq * pos.quantity)
        return value

    def _mark(self, ts: pd.Timestamp, close: float, signal_time: pd.Timestamp | None = None,
              signal_target: int = 0, execution_rejection: str = "",
              execution_rejection_time: pd.Timestamp | None = None,
              execution_rejection_target: int = 0):
        pos = self.position
        value = self._equity_value(close)
        self.eq_time.append(ts); self.eq_cash.append(self.cash); self.eq_value.append(value)
        self.eq_side.append("FLAT" if pos is None else ("LONG" if pos.side == 1 else "SHORT"))
        self.eq_qty.append(0.0 if pos is None else pos.quantity)
        self.eq_signal_time.append(signal_time)
        self.eq_signal_target.append(signal_target)
        self.eq_execution_rejection.append(execution_rejection)
        self.eq_execution_rejection_time.append(execution_rejection_time)
        self.eq_execution_rejection_target.append(execution_rejection_target)
        effective_stop = (
            self._stop_price(pos.side, pos.entry_price, pos.stop_pct)
            if pos is not None and pos.stop_pct > 0 and not pos.breakeven
            else pos.entry_price if pos is not None and pos.breakeven else np.nan
        )
        self.eq_stop_price.append(
            self._stop_trigger_price_this_bar
            if np.isfinite(self._stop_trigger_price_this_bar)
            else effective_stop
        )
        self.eq_entry_price.append(pos.entry_price if pos is not None else np.nan)
        self.eq_position_id.append(pos.position_id if pos is not None else np.nan)
        self.eq_breakeven_activated.append(self._breakeven_activated_this_bar)
        self.eq_breakeven_price.append(self._breakeven_activation_price)
        self._breakeven_activated_this_bar = False
        self._breakeven_activation_price = np.nan
        self._stop_trigger_price_this_bar = np.nan

    def run(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        n, cfg = len(self.bars), self.cfg
        if not n:
            return empty_trades(), pd.DataFrame(columns=["timestamp", "cash", "equity", "position_side", "position_quantity"])
        adx_labels = "_".join(f"{int(pd.Timedelta(tf).total_seconds() // 60)}M"
                              for tf in cfg.adx_timeframes)
        adx_base = f"ADX_{adx_labels}_REVERSAL"
        adx_reason = adx_base if cfg.adx_exit_pct >= 100 else f"{adx_base}_{cfg.adx_exit_pct:g}%"
        weak_tfs = cfg.ema_weakening_timeframes or (cfg.timeframe,)
        weak_fast, weak_slow = cfg.ema_weakening_lengths
        weak_base = "EMA_WEAKENING_" + "_".join(tf.upper() for tf in weak_tfs)
        weak_len = f"EMA{weak_fast}/{weak_slow}"
        if (weak_fast, weak_slow) != (cfg.ema_fast, cfg.ema_slow):
            weak_base = f"{weak_base}_{weak_len}"
        weak_reason = weak_base if cfg.ema_weakening_exit_pct >= 100 else f"{weak_base}_{cfg.ema_weakening_exit_pct:g}%"
        started = time.perf_counter()
        print(f"[BACKTEST] {n:,} velas {cfg.timeframe}", flush=True)
        month_key: Optional[tuple[int, int]] = None
        month_start_equity = cfg.initial_capital
        for i in range(n):
            self._bar_index = i
            execution_reasons: list[str] = []
            rejection_time: pd.Timestamp | None = None
            rejection_target = 0
            # Reinicio del límite mensual en cada mes calendario.
            key = (self.times[i].year, self.times[i].month)
            if key != month_key:
                month_key = key
                self.halt_until = None
                month_start_equity = self._equity_value(self.c[i - 1]) if i else cfg.initial_capital
            # 1) Ejecutar target/exit pendientes (decididos al cierre anterior)
            if self.pending_target is not None:
                pending_target = self.pending_target
                signal_bias = (
                    int(self.signals["trend_bias"].iloc[i - 1]) if i > 0 else 0
                )
                rejection = self._execute_target(i, pending_target, signal_bias)
                if rejection is not None:
                    execution_reasons.append(rejection)
                    rejection_time = self.times[i - 1] if i > 0 else self.times[i]
                    rejection_target = pending_target
                self.pending_target = None
                self.pending_exit = None
            elif self.pending_exit is not None:
                reason, pct = self.pending_exit
                self.close_position(pct, self.o[i], self.times[i], reason)
                self.pending_exit = None

            # 2) Gestión de stops y salidas parciales
            intrabar_slice = (
                self._intrabar_slices[i]
                if i < len(self._intrabar_slices)
                else (0, 0, False)
            )
            if self._intrabar_bars is not None and intrabar_slice[2]:
                # La salida EMA tras TP3 sigue siendo de cierre de vela de
                # estrategia y no debe activarse en la misma vela que confirma
                # TP3, aunque el TP3 se alcance en uno de sus minutos.
                tp3_previously_done = (
                    self.position is not None and self.position.partial3_done
                )
                stop_hit = False
                left, right, _ = intrabar_slice
                intrabar_times = self._intrabar_bars.index
                for minute in range(left, right):
                    minute_time = intrabar_times[minute]
                    first_trade = len(self.trades)
                    stop_hit = self._stop(
                        i,
                        open_price=self._intrabar_o[minute],
                        high=self._intrabar_h[minute],
                        low=self._intrabar_l[minute],
                        timestamp=minute_time,
                    )
                    if stop_hit:
                        self._remember_intrabar_exits(first_trade)
                        break
                    self._manage(
                        i,
                        open_price=self._intrabar_o[minute],
                        high=self._intrabar_h[minute],
                        low=self._intrabar_l[minute],
                        timestamp=minute_time,
                    )
                    self._remember_intrabar_exits(first_trade)
                slow_ema_touch_exit = False
                if not stop_hit and tp3_previously_done:
                    slow_ema_touch_exit = self._tp3_slow_ema_touch_exit(i)
            else:
                # Compatibilidad/fallback: sin una ventana completa de 1m se
                # conserva la ejecución OHLC original de la vela estrategia.
                stop_hit = self._stop(i)
                slow_ema_touch_exit = False
                if not stop_hit:
                    # Si TP3 se activa ahora, el toque a EMA lenta sólo cuenta
                    # desde la vela de estrategia siguiente.
                    slow_ema_touch_exit = self._tp3_slow_ema_touch_exit(i)
                if not stop_hit and not slow_ema_touch_exit:
                    self._manage(i)

            # Equity mark-to-market para el límite de pérdida mensual.
            equity_now = self._equity_value(self.c[i])

            # Límite de pérdida mensual: si la equity cae por debajo del umbral
            # respecto al inicio del mes, se protege el mes completo (se cierra
            # la posición, si la hay, y no se abren nuevas hasta el próximo mes).
            if cfg.monthly_loss_limit_pct > 0 and self.halt_until is None:
                if equity_now <= month_start_equity * (1 - cfg.monthly_loss_limit_pct / 100):
                    if cfg.monthly_loss_limit_days > 0:
                        self.halt_until = self.times[i] + pd.Timedelta(days=cfg.monthly_loss_limit_days)
                        scope = f"{cfg.monthly_loss_limit_days} días"
                    else:
                        self.halt_until = pd.Timestamp.max.tz_localize(self.times[i].tz) if self.times[i].tz else pd.Timestamp.max
                        scope = "resto del mes"
                    if cfg.monthly_loss_limit_close and self.position is not None:
                        reason = f"{MONTHLY_LIMIT_REASON}_{cfg.monthly_loss_limit_pct:g}%"
                        self.close_position(
                            100,
                            self.c[i],
                            self.times[i] + pd.Timedelta(cfg.timeframe),
                            reason,
                        )
                    print(f"[GUARD] {self.times[i]:%Y-%m-%d} límite "
                          f"-{cfg.monthly_loss_limit_pct:g}% alcanzado; sin entradas hasta "
                          f"{scope}", flush=True)

            signal_valid = bool(self.signals["valid"].iloc[i])
            signal_target = int(self.signals["target"].iloc[i]) if signal_valid else 0
            target = signal_target
            if stop_hit and signal_target:
                execution_reasons.append("stop prioritario en la misma vela")
                rejection_time = self.times[i]
                rejection_target = signal_target
            elif not stop_hit and signal_valid:
                rejected_before_execution = False
                if cfg.entry_atr_enabled and not self.entry_atr_allowed[i]:
                    execution_reasons.append("filtro ATR")
                    target = 0
                    rejected_before_execution = True
                if cfg.entry_gaussian_trend_enabled and target == 1 \
                        and self.entry_gaussian_trend_dir[i] != 1:
                    execution_reasons.append("Gaussian Filter Trend")
                    target = 0
                    rejected_before_execution = True
                if cfg.entry_gaussian_trend_enabled and target == -1 \
                        and self.entry_gaussian_trend_dir[i] != -1:
                    execution_reasons.append("Gaussian Filter Trend")
                    target = 0
                    rejected_before_execution = True
                if target == 1 and cfg.entry_rsi_divergence_enabled \
                        and self.entry_rsi_bear_divergence_blocked[i]:
                    execution_reasons.append("divergencia RSI")
                    target = 0
                    rejected_before_execution = True
                if target == -1 and cfg.entry_rsi_divergence_enabled \
                        and self.entry_rsi_bull_divergence_blocked[i]:
                    execution_reasons.append("divergencia RSI")
                    target = 0
                    rejected_before_execution = True
                if target == 1 and not cfg.allow_long:
                    execution_reasons.append("LONG deshabilitado")
                    target = 0
                    rejected_before_execution = True
                if target == -1 and not cfg.allow_short:
                    execution_reasons.append("SHORT deshabilitado")
                    target = 0
                    rejected_before_execution = True
                if rejected_before_execution:
                    rejection_time = self.times[i]
                    rejection_target = signal_target
                current = self.position.side if self.position else 0
                skip_exit_checks = False
                if target and target != current and self.entries_blocked():
                    execution_reasons.append("protección mensual")
                    rejection_time = self.times[i]
                    rejection_target = signal_target
                elif target and target != current:
                    bias_val = int(self.signals["trend_bias"].iloc[i])
                    if cfg.streak_trend_only and self.streak_in_loss and bias_val != target:
                        # Tras una pérdida sólo se entra con la tendencia mayor.
                        execution_reasons.append(
                            f"filtro de tendencia (sesgo {bias_val:+d})"
                        )
                        rejection_time = self.times[i]
                        rejection_target = signal_target
                        skip_exit_checks = True
                    else:
                        self.pending_target = target
                        skip_exit_checks = True
                if not skip_exit_checks and self.position is not None and self.weak_exit[i]:
                    self.pending_exit = (weak_reason, cfg.ema_weakening_exit_pct)
                elif not skip_exit_checks and self.position is not None and self.adx_exit[i]:
                    self.pending_exit = (adx_reason, cfg.adx_exit_pct)
                elif not skip_exit_checks and self.position is not None:
                    # Primero se atiende la divergencia RSI confirmada.
                    div_reason = self._rsi_divergence_reason(i)
                    if div_reason is not None:
                        div_pct = cfg.rsi_divergence_exit_pct
                        if div_pct < 100:
                            div_reason = f"{div_reason}_{div_pct:g}%"
                        self.pending_exit = (div_reason, div_pct)
                    else:
                        if (cfg.gaussian_trend_reversal_exit_enabled
                              and self.position.side == 1
                              and i < len(self.gaussian_trend_turned_bearish)
                              and self.gaussian_trend_turned_bearish[i]):
                            reason = GAUSSIAN_TREND_BEAR_REASON
                            if cfg.gaussian_trend_reversal_exit_pct < 100:
                                reason += f"_{cfg.gaussian_trend_reversal_exit_pct:g}%"
                            self.pending_exit = (
                                reason, cfg.gaussian_trend_reversal_exit_pct
                            )
                        elif (cfg.gaussian_trend_reversal_exit_enabled
                              and self.position.side == -1
                              and i < len(self.gaussian_trend_turned_bullish)
                              and self.gaussian_trend_turned_bullish[i]):
                            reason = GAUSSIAN_TREND_BULL_REASON
                            if cfg.gaussian_trend_reversal_exit_pct < 100:
                                reason += f"_{cfg.gaussian_trend_reversal_exit_pct:g}%"
                            self.pending_exit = (
                                reason, cfg.gaussian_trend_reversal_exit_pct
                            )

            self._mark(
                self.times[i] + pd.Timedelta(cfg.timeframe), self.c[i],
                signal_time=self.times[i], signal_target=signal_target,
                execution_rejection="; ".join(execution_reasons),
                execution_rejection_time=rejection_time,
                execution_rejection_target=rejection_target,
            )
            if (i + 1) % max(1, n // 10) == 0 or i == n - 1:
                print(f"[BACKTEST] {(i+1)/n:6.1%} | {i+1:,}/{n:,} velas | trades={len(self.trades)}", flush=True)

        self._replay_intrabar_tail()

        if self.position is not None and cfg.force_close_at_end:
            if self._intrabar_tail_last is not None:
                last_minute, last_close = self._intrabar_tail_last
                ts = last_minute + pd.Timedelta("1min")
                close_price = last_close
            else:
                ts = self.times[-1] + pd.Timedelta(cfg.timeframe)
                close_price = self.c[-1]
            self.close_position(100, close_price, ts, "END_OF_DATA")
            self._mark(ts, close_price)
        trades = pd.DataFrame([asdict(t) for t in self.trades]) if self.trades else empty_trades()
        equity = pd.DataFrame({
            "timestamp": self.eq_time,
            "cash": self.eq_cash,
            "equity": self.eq_value,
            "position_side": self.eq_side,
            "position_quantity": self.eq_qty,
            "signal_time": self.eq_signal_time,
            "signal_target": self.eq_signal_target,
            "execution_rejection": self.eq_execution_rejection,
            "execution_rejection_time": self.eq_execution_rejection_time,
            "execution_rejection_target": self.eq_execution_rejection_target,
            "effective_stop_price": self.eq_stop_price,
            "position_entry_price": self.eq_entry_price,
            "position_id": self.eq_position_id,
            "breakeven_activated": self.eq_breakeven_activated,
            "breakeven_price": self.eq_breakeven_price,
        })
        print(f"[BACKTEST] Finalizado en {time.perf_counter()-started:.2f}s | tramos={len(trades):,}", flush=True)
        return trades, equity


# ---------------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------------

#: Dataset OHLCV en Parquet; las rutas relativas cuelgan de la raíz del proyecto.
DATASET = "dataset/binance/futures/um/BTCUSDT/merged/BTCUSDT-1m.parquet"
#: Directorio donde se escriben los informes del backtest.
OUTPUT_DIR = DEFAULT_OUTPUT_DIR
#: Escribir los archivos del informe.
WRITE_OUTPUTS = True
#: Generar el gráfico de equity.
PLOT_ENABLED = True
#: Ruta del gráfico; ``None`` usa la ruta por defecto del informe.
PLOT_PATH: str | None = None
#: Resolución del gráfico en puntos por pulgada.
PLOT_DPI = 150
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    """Ejecuta el backtest con ``Config()`` y las constantes de este módulo."""
    cfg = Config()

    dataset = Path(DATASET)
    if not dataset.is_absolute():
        dataset = PROJECT_ROOT / dataset
    if not dataset.is_file():
        raise FileNotFoundError(f"No se encontró el dataset del backtest: {dataset}")

    raw = load_parquet(dataset)
    bars = to_timeframe(raw, cfg)
    if len(bars) < cfg.ema_slow + 2:
        raise ValueError(f"Dataset insuficiente para EMA{cfg.ema_slow}: {len(bars)} velas.")

    strategy = EMACrossStrategy(cfg)
    signals = strategy.generate_signals(bars, raw)
    adx_exit, weak_exit, rsi_div_exit = strategy.prepare_exits(raw, bars)
    if cfg.adx_exit_enabled:
        print(f"[ADX] {'disponible' if adx_exit is not None else 'sin datos suficientes/timeframe incompatible'}", flush=True)
    if cfg.ema_weakening_exit_enabled:
        print(f"[WEAKENING] {'disponible' if weak_exit is not None else 'sin datos suficientes'}", flush=True)
    if cfg.rsi_divergence_exit_enabled:
        print(f"[RSI DIV] {'disponible' if rsi_div_exit is not None else 'sin datos suficientes/timeframe incompatible'}", flush=True)

    engine = Backtester(
        bars,
        cfg,
        signals,
        adx_exit,
        weak_exit,
        rsi_div_exit,
        intrabar_bars=raw,
        intrabar_timeframe="1min",
    )
    trades, equity = engine.run()
    generate_report(
        trades, equity, cfg, bars, str(dataset),
        output_dir=OUTPUT_DIR,
        write_outputs_enabled=WRITE_OUTPUTS,
        plot_enabled=PLOT_ENABLED,
        plot_path=PLOT_PATH,
        plot_dpi=PLOT_DPI,
        raw=raw,
        signals=signals,
        adx_exit=adx_exit,
        weak_exit=weak_exit,
        rsi_div_exit=rsi_div_exit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
