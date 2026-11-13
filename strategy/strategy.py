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

Además, una **caída de volumen en las últimas `volume_exit_bars` velas** cierra
`volume_exit_pct` % de la posición (una vez por posición): se compara la media
de volumen de la mitad reciente del rango con la de la mitad anterior y se exige
una caída mínima de `volume_exit_min_drop_pct` % (`volume_exit_enabled`).

Archivo único con API reutilizable para backtesting y bots.

Protecciones de riesgo (ver `Config`):

* `entry_price_ema_filter_enabled` (default True): tras confirmarse un cruce
  alcista no se entra si el cierre queda por debajo de las EMAs rápida y lenta;
  tras un cruce bajista no se entra si queda por encima de ambas. Evita
  entradas contra el precio cuando el cruce ya se ha girado.
* `streak_trend_only` (default True): tras una posición cerrada en pérdida,
  la siguiente entrada sólo se abre si la señal coincide con el sesgo de
  tendencia de `trend_timeframe` (vela superior ya cerrada, sin lookahead).
  Corta las rachas de -1R del whipsaw sin recortar los cruces en tendencia.
* `monthly_loss_limit_pct` (default 5.0): si la equity cae más de X% por
  debajo de la equity al inicio del mes calendario se cierra la posición (opcional)
  y no se abren nuevas hasta el siguiente mes (o `monthly_loss_limit_days` días).
* `adaptive_risk_window` (default 20) + `adaptive_risk_k`: escala `risk_pct`
  con el win rate de las últimas n posiciones: penaliza las rachas frías
  (donde se concentran los meses rojos) y reutiliza el tamaño en las cálidas.

Reducción de meses rojos (BTCUSDT 2020-2026, 30min, 82 meses). Partiendo del
uso previo (`streak_trend_only`, `monthly_loss_limit_pct=5`, `adaptive_risk_window=0`):

| variante                                   | meses en rojo | peor mes | suma meses rojos | retorno | max DD | PF   |
|--------------------------------------------|---------------|----------|------------------|---------|--------|------|
| sin tamaño adaptativo                      | 25 (30,5 %)   | -3,15 %  | -28,6 %          | 477,9 % | -8,70% | 2,43 |
| `adaptive_risk_window=20` (**default**)    | 25 (30,5 %)   | -2,49 %  | -26,1 %          | 439,8 % | -7,96% | 2,49 |

El mismo recorte se repite en train (2020-2023) y test (2024-2026) y con costes
realistas (comisión 4 bps + slippage 2 bps), donde el tamaño adaptativo también
mejora el drawdown (-10,1 % -> -8,2 %).

Uso:
    python run_backtest.py

Los parámetros de la estrategia se toman de los defaults de
`strategy.config.Config`; los de ejecución (dataset, salida, gráfico y logs)
están hardcodeados como constantes al final de este módulo.

API para bots:
    cfg = Config()
    bars = load_parquet("dataset/binance/BTCUSDT/merged/BTCUSDT-1m.parquet")
    strategy = EMACrossStrategy(cfg)
    signals = strategy.generate_signals(bars)
    # signals contiene señales conocidas al cierre de cada vela.
    # En ejecución real, actuar en la siguiente apertura/iteración.

Dependencias:
    pip install "pandas>=2" pyarrow numpy matplotlib TA-Lib
"""
from __future__ import annotations
from dataclasses import asdict
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
    VOLUME_DECLINE_REASON,
    empty_trades,
)
from strategy.indicators import (
    ema,
    ema_band_touch_flags,
    efficiency_ratio,
    adx,
    atr_volatility_filter,
    confirmed_crosses,
    align_closed_values,
    build_adx_exit,
    build_weakening_exit,
    build_rsi_divergence_flags,
    build_rsi_divergence_exit,
    spread_flags,
    volume_decline_flags,
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
        fast = ema(bars_upper.close, cfg.ema_fast).to_numpy(float)
        slow = ema(bars_upper.close, cfg.ema_slow).to_numpy(float)
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
        up = (fast > slow) & (prev_fast <= prev_slow)
        down = (fast < slow) & (prev_fast >= prev_slow)
        valid = np.isfinite(fast) & np.isfinite(slow) & np.isfinite(prev_fast) & np.isfinite(prev_slow)
        if cfg.confirm_bars:
            up = confirmed_crosses(up, fast > slow, cfg.confirm_bars)
            down = confirmed_crosses(down, fast < slow, cfg.confirm_bars)
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
        entry_mfi = None
        if cfg.entry_mfi_risk_scaling_enabled and "volume" in bars:
            mfi_inputs = tuple(
                np.ascontiguousarray(bars[column].to_numpy(dtype=np.float64))
                for column in ("high", "low", "close", "volume")
            )
            entry_mfi = talib.MFI(*mfi_inputs, timeperiod=cfg.entry_mfi_len)
        entry_er = (
            efficiency_ratio(bars.close, cfg.entry_er_len).to_numpy(float)
            if cfg.entry_er_risk_scaling_enabled else None
        )
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
            up &= ~entry_rsi_bear_divergence_blocked
            down &= ~entry_rsi_bull_divergence_blocked
        else:
            entry_rsi_bull_divergence_blocked = np.zeros(len(bars), dtype=bool)
            entry_rsi_bear_divergence_blocked = np.zeros(len(bars), dtype=bool)
        if cfg.entry_adx_enabled:
            allowed = np.ones(len(bars), dtype=bool)
            for values in entry_adx_by_timeframe.values():
                allowed &= np.isfinite(values) & (values > cfg.entry_adx_min)
            up, down = up & allowed, down & allowed
        up, down = up & atr_allowed, down & atr_allowed
        up, down = up & (macd_line > 0), down & (macd_line < 0)
        if cfg.entry_price_ema_filter_enabled:
            # Tras un cruce alcista no se entra si el precio queda por debajo
            # de ambas EMAs; tras un bajista, no se entra si queda por encima.
            price = bars.close.to_numpy(float)
            up &= ~((price < fast) & (price < slow))
            down &= ~((price > fast) & (price > slow))
        bias = self.trend_bias(bars) if cfg.streak_trend_only else None
        up &= valid
        down &= valid
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
            "cross_up": up, "cross_down": down, "valid": valid,
            "trend_bias": bias if bias is not None else np.zeros(len(bars), int),
            "target": np.select([up, down], [1, -1], default=0),
        }
        for timeframe, values in entry_adx_by_timeframe.items():
            output[f"adx_entry_{timeframe}"] = values
        if entry_mfi is not None:
            output["entry_mfi"] = np.asarray(entry_mfi, dtype=float)
        if entry_er is not None:
            output["entry_er"] = entry_er
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
    """Motor de ejecución. Las señales se evalúan al cierre y se ejecutan en la apertura siguiente."""

    def __init__(self, bars: pd.DataFrame, config: Config, signals: Optional[pd.DataFrame] = None,
                 adx_exit: Optional[np.ndarray] = None, weak_exit: Optional[np.ndarray] = None,
                 rsi_div_exit: Optional[np.ndarray] = None, verbose: bool = False):
        self.bars, self.cfg, self.verbose = bars, config, verbose
        self.signals = signals if signals is not None else EMACrossStrategy(config).generate_signals(bars)
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
        self.entry_mfi = (
            self.signals["entry_mfi"].to_numpy(float)
            if "entry_mfi" in self.signals else None
        )
        self.entry_er = (
            self.signals["entry_er"].to_numpy(float)
            if "entry_er" in self.signals else None
        )
        self.adx_exit = np.asarray(adx_exit, bool) if adx_exit is not None else np.zeros(len(bars), bool)
        self.weak_exit = np.asarray(weak_exit, bool) if weak_exit is not None else np.zeros(len(bars), bool)
        # Divergencia RSI: +1 alcista (cierra SHORT), -1 bajista (cierra LONG), 0 sin señal.
        self.rsi_div = np.asarray(rsi_div_exit, int) if rsi_div_exit is not None else np.zeros(len(bars), int)
        self.volume_exit = (
            volume_decline_flags(
                bars["volume"].to_numpy(float),
                config.volume_exit_bars,
                config.volume_exit_min_drop_pct,
            )
            if config.volume_exit_enabled and "volume" in bars
            else np.zeros(len(bars), dtype=bool)
        )
        self.ema55_touch_exit_active = (
            config.ema55_touch_exit_enabled
            and pd.Timedelta(config.timeframe) == pd.Timedelta("30min")
        )
        if self.ema55_touch_exit_active:
            self.ema55_line, self.ema55_touches = ema_band_touch_flags(
                bars, 55, config.ema55_touch_tolerance_pct
            )
        else:
            self.ema55_line = np.full(len(bars), np.nan, dtype=float)
            self.ema55_touches = np.zeros(len(bars), dtype=bool)
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
        # Estado del límite mensual y pnl acumulado de la posición abierta.
        self.halt_until: Optional[pd.Timestamp] = None
        self._bar_index = -1
        self.streak_in_loss = False
        self._position_pnl = 0.0
        self._recent_wins: list[bool] = []

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
        pct_sl = self.cfg.stop_loss_pct
        risk_unit = self._risk_per_unit(side, price, pct_sl) if pct_sl > 0 else 0.0
        by_risk = (
            self.cash * self.cfg.risk_pct * self._risk_multiplier()
            * self._entry_mfi_risk_multiplier(side) * self._entry_er_risk_multiplier()
            / risk_unit
            if risk_unit > 0 else np.inf
        )
        by_lev = self.cash * self.cfg.max_leverage / price
        qty_total = min(by_risk, by_lev)
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
            entry_bar_index=self._bar_index,
        )
        self.opened.append({
            "position_id": self.position_id, "time": ts, "price": price, "side": side,
            "quantity": qty_total,
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
            reason, pos.risk_amount))
        remaining = pos.quantity - qty
        if percent >= 100 or remaining <= max(1e-12, pos.initial_quantity * 1e-12):
            self.position = None
            self._close_streak()
        else:
            pos.quantity = remaining
            pos.entry_fee -= entry_fee

    def _risk_multiplier(self) -> float:
        """Multiplicador de `risk_pct` según el resultado de las últimas posiciones.

        Con `adaptive_risk_window` a 0 devuelve 1 (tamaño fijo). Si no,
        ``1 + k * (win_rate de las últimas n posiciones - 0.5)`` recortado a
        ``[adaptive_risk_min, adaptive_risk_max]``: penaliza las rachas frías
        (donde salen los meses rojos) y reutiliza el tamaño en las cálidas.
        """
        cfg = self.cfg
        if cfg.adaptive_risk_window <= 0 or not self._recent_wins:
            return 1.0
        wins = self._recent_wins[-cfg.adaptive_risk_window:]
        win_rate = sum(wins) / len(wins)
        factor = 1 + cfg.adaptive_risk_k * (win_rate - 0.5)
        return float(min(max(factor, cfg.adaptive_risk_min), cfg.adaptive_risk_max))

    def _entry_mfi_risk_multiplier(self, side: int) -> float:
        """Escala tamaño por flujo MFI de la última vela de señal ya cerrada."""
        cfg = self.cfg
        if (not cfg.entry_mfi_risk_scaling_enabled or self.entry_mfi is None
                or side not in (1, -1)):
            return 1.0
        signal_index = self._bar_index - 1
        if signal_index < 0 or signal_index >= len(self.entry_mfi):
            return 1.0
        value = self.entry_mfi[signal_index]
        if not np.isfinite(value):
            return 1.0
        directional_flow = (value - 50.0) * side
        factor = cfg.entry_mfi_risk_base + (
            directional_flow / cfg.entry_mfi_risk_spread_scale
        )
        return float(np.clip(factor, cfg.entry_mfi_risk_min, cfg.entry_mfi_risk_max))

    def _entry_er_risk_multiplier(self) -> float:
        """Escala riesgo por eficiencia direccional de los cierres previos."""
        cfg = self.cfg
        if not cfg.entry_er_risk_scaling_enabled or self.entry_er is None:
            return 1.0
        signal_index = self._bar_index - 1
        if signal_index < 0 or signal_index >= len(self.entry_er):
            return 1.0
        value = self.entry_er[signal_index]
        if not np.isfinite(value):
            return 1.0
        factor = cfg.entry_er_risk_base + cfg.entry_er_risk_slope * value
        return float(np.clip(factor, cfg.entry_er_risk_min, cfg.entry_er_risk_max))

    def _close_streak(self) -> None:
        """Actualiza el historial adaptativo de resultados y el filtro de racha."""
        cfg = self.cfg
        won = self._position_pnl >= 0
        if cfg.adaptive_risk_window > 0:
            self._recent_wins.append(won)
            del self._recent_wins[:-cfg.adaptive_risk_window]
        self.streak_in_loss = not won
        self._position_pnl = 0.0

    def entries_blocked(self) -> bool:
        """``True`` si está prohibido abrir posiciones nuevas en esta vela."""
        return self.halt_until is not None and self.times[self._bar_index] < self.halt_until

    def _risk_unit(self, pos: Position) -> float:
        return pos.risk_amount / pos.initial_quantity if pos.initial_quantity else 0.0

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

    def _ema55_touch_weakness_exit_reason(self, i: int) -> Optional[str]:
        """Cierra LONG tras toques repetidos a EMA55 sin ruptura confirmada.

        El toque se cuenta cuando el rango de la vela roza/cruza EMA55 ± banda.
        Se exige una secuencia de toques o varios dentro de la ventana y una
        pérdida abierta mínima. Para LONG, el cierre no debe confirmar ruptura
        arriba; para SHORT, no debe confirmar ruptura abajo. Sólo corre en 30m.
        """
        pos = self.position
        cfg = self.cfg
        if (pos is None or pos.side not in (1, -1) or not self.ema55_touch_exit_active
                or i >= len(self.ema55_touches)):
            return None
        line = self.ema55_line[i]
        if not np.isfinite(line):
            return None
        open_pnl_pct = (self.c[i] / pos.entry_price - 1.0) * pos.side * 100.0
        loss_threshold = cfg.ema55_touch_exit_loss_pct if pos.side == 1 \
            else cfg.ema55_touch_short_exit_loss_pct
        if open_pnl_pct > -loss_threshold:
            return None

        start = max(0, i - cfg.ema55_touch_lookback_bars + 1, pos.entry_bar_index)
        window = self.ema55_touches[start : i + 1]
        repeated = int(window.sum()) >= cfg.ema55_touch_min_touches
        consecutive = 0
        for touched in self.ema55_touches[i : start - 1 if start else None : -1]:
            if not touched:
                break
            consecutive += 1
        if not (repeated or consecutive >= cfg.ema55_touch_consecutive):
            return None

        failed_break_level = line * (1.0 + cfg.ema55_touch_breakout_buffer_pct / 100.0)
        if pos.side == 1:
            if self.c[i] > failed_break_level:
                return None
            return "EMA55_REPEATED_TOUCH_WEAKNESS"
        failed_downside_break_level = line * (
            1.0 - cfg.ema55_touch_breakout_buffer_pct / 100.0
        )
        if self.c[i] < failed_downside_break_level:
            return None
        return "EMA55_REPEATED_TOUCH_WEAKNESS_SHORT"

    def _tp3_slow_ema_touch_exit(self, i: int) -> bool:
        """Cierra el SHORT restante si supera la EMA lenta tras TP3."""
        pos = self.position
        if pos is None or pos.side != -1 or not pos.partial3_done or i >= len(self.ema_slow_line):
            return False
        line = self.ema_slow_line[i]
        if not np.isfinite(line):
            return False
        trigger = line * (1.0 + self.cfg.tp3_ema_slow_penetration_pct / 100.0)
        if self.h[i] < trigger:
            return False
        # Si abre más allá del nivel, el fill no puede ser mejor que la apertura.
        fill = max(trigger, self.o[i])
        self.close_position(100, fill, self.times[i], "EMA_SLOW_TOUCH_SHORT_AFTER_TP3")
        return True

    def _manage(self, i: int) -> None:
        """Gestiona take-profits parciales y el stop a breakeven."""
        cfg, pos = self.cfg, self.position
        if pos is None:
            return
        if not cfg.partial_tp_enabled:
            return
        risk_unit = self._risk_unit(pos)
        if risk_unit <= 0:
            return
        pos_after = pos
        # Objetivos de mayor distancia primero para conservar un solo parcial
        # por vela cuando el rango alcanza varios niveles.
        if cfg.partial_tp7_r > 0 and not pos.partial7_done:
            trigger7 = pos.entry_price + pos.side * cfg.partial_tp7_r * risk_unit
            hit7 = self.h[i] >= trigger7 if pos.side == 1 else self.l[i] <= trigger7
            if hit7:
                fill7 = max(trigger7, self.o[i]) if pos.side == 1 else min(trigger7, self.o[i])
                self.close_position(cfg.partial_tp7_pct, fill7, self.times[i], f"PARTIAL_TP7_{cfg.partial_tp7_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial7_done = True
                return
        if cfg.partial_tp6_r > 0 and not pos.partial6_done:
            trigger6 = pos.entry_price + pos.side * cfg.partial_tp6_r * risk_unit
            hit6 = self.h[i] >= trigger6 if pos.side == 1 else self.l[i] <= trigger6
            if hit6:
                fill6 = max(trigger6, self.o[i]) if pos.side == 1 else min(trigger6, self.o[i])
                self.close_position(cfg.partial_tp6_pct, fill6, self.times[i], f"PARTIAL_TP6_{cfg.partial_tp6_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial6_done = True
                return
        if cfg.partial_tp5_r > 0 and not pos.partial5_done:
            trigger5 = pos.entry_price + pos.side * cfg.partial_tp5_r * risk_unit
            hit5 = self.h[i] >= trigger5 if pos.side == 1 else self.l[i] <= trigger5
            if hit5:
                fill5 = max(trigger5, self.o[i]) if pos.side == 1 else min(trigger5, self.o[i])
                self.close_position(cfg.partial_tp5_pct, fill5, self.times[i], f"PARTIAL_TP5_{cfg.partial_tp5_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial5_done = True
                return
        if cfg.partial_tp4_r > 0 and not pos.partial4_done:
            trigger4 = pos.entry_price + pos.side * cfg.partial_tp4_r * risk_unit
            hit4 = self.h[i] >= trigger4 if pos.side == 1 else self.l[i] <= trigger4
            if hit4:
                fill4 = max(trigger4, self.o[i]) if pos.side == 1 else min(trigger4, self.o[i])
                self.close_position(cfg.partial_tp4_pct, fill4, self.times[i], f"PARTIAL_TP4_{cfg.partial_tp4_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial4_done = True
                return
        if cfg.partial_tp3_r > 0 and not pos.partial3_done:
            trigger3 = pos.entry_price + pos.side * cfg.partial_tp3_r * risk_unit
            hit3 = self.h[i] >= trigger3 if pos.side == 1 else self.l[i] <= trigger3
            if hit3:
                fill3 = max(trigger3, self.o[i]) if pos.side == 1 else min(trigger3, self.o[i])
                self.close_position(cfg.partial_tp3_pct, fill3, self.times[i], f"PARTIAL_TP3_{cfg.partial_tp3_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial3_done = True
                return
        # TP2 primero si no hecho y trigger alcanzado (mayor distancia)
        if cfg.partial_tp2_r > 0 and not pos.partial2_done:
            trigger2 = pos.entry_price + pos.side * cfg.partial_tp2_r * risk_unit
            hit2 = self.h[i] >= trigger2 if pos.side == 1 else self.l[i] <= trigger2
            if hit2:
                fill2 = max(trigger2, self.o[i]) if pos.side == 1 else min(trigger2, self.o[i])
                self.close_position(cfg.partial_tp2_pct, fill2, self.times[i], f"PARTIAL_TP2_{cfg.partial_tp2_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial2_done = True
                return
        # TP1
        if not pos.partial_done:
            trigger = pos.entry_price + pos.side * cfg.partial_tp_r * risk_unit
            hit = self.h[i] >= trigger if pos.side == 1 else self.l[i] <= trigger
            if hit:
                fill = max(trigger, self.o[i]) if pos.side == 1 else min(trigger, self.o[i])
                self.close_position(cfg.partial_tp_pct, fill, self.times[i], f"PARTIAL_TP_{cfg.partial_tp_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial_done = True
                    if cfg.move_stop_to_breakeven:
                        pos_after.breakeven = True
    def _stop(self, i: int) -> bool:
        pos = self.position
        if pos is None or pos.stop_pct <= 0: return False
        stop = pos.entry_price if pos.breakeven else self._stop_price(pos.side, pos.entry_price, pos.stop_pct)
        hit = self.l[i] <= stop if pos.side == 1 else self.h[i] >= stop
        if not hit: return False
        fill = min(self.o[i], stop) if pos.side == 1 else max(self.o[i], stop)
        reason = "BREAKEVEN" if pos.breakeven else f"STOP_LOSS_{pos.stop_pct*100:.2f}%"
        self.close_position(100, fill, self.times[i], reason)
        if self.verbose: print(f"[STOP] {self.times[i]} fill={fill:.8f}", flush=True)
        return True

    def _execute_target(self, i: int, target: int):
        if self.position is not None and self.position.side != target:
            self.close_position(100, self.o[i], self.times[i], CROSS_REASON)
        if self.position is None:
            self.open_position(target, self.o[i], self.times[i])

    def _equity_value(self, close: float) -> float:
        """Equity mark-to-market al precio ``close`` (caja + posición a precio de liquidación)."""
        pos = self.position
        value = self.cash
        if pos is not None:
            liq = self._price(close, -pos.side)
            value += (liq - pos.entry_price) * pos.quantity * pos.side - self._fee(liq * pos.quantity)
        return value

    def _mark(self, ts: pd.Timestamp, close: float):
        pos = self.position
        value = self._equity_value(close)
        self.eq_time.append(ts); self.eq_cash.append(self.cash); self.eq_value.append(value)
        self.eq_side.append("FLAT" if pos is None else ("LONG" if pos.side == 1 else "SHORT"))
        self.eq_qty.append(0.0 if pos is None else pos.quantity)

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
        volume_base = f"{VOLUME_DECLINE_REASON}_{cfg.volume_exit_bars}B"
        volume_reason = (
            volume_base if cfg.volume_exit_pct >= 100
            else f"{volume_base}_{cfg.volume_exit_pct:g}%"
        )
        started = time.perf_counter()
        print(f"[BACKTEST] {n:,} velas {cfg.timeframe}", flush=True)
        month_key: Optional[tuple[int, int]] = None
        month_start_equity = cfg.initial_capital
        for i in range(n):
            self._bar_index = i
            # Reinicio del límite mensual en cada mes calendario.
            key = (self.times[i].year, self.times[i].month)
            if key != month_key:
                month_key = key
                self.halt_until = None
                month_start_equity = self._equity_value(self.c[i - 1]) if i else cfg.initial_capital
            # 1) Ejecutar target/exit pendientes (decididos al cierre anterior)
            if self.pending_target is not None:
                self._execute_target(i, self.pending_target)
                self.pending_target = None
                self.pending_exit = None
            elif self.pending_exit is not None:
                reason, pct = self.pending_exit
                self.close_position(pct, self.o[i], self.times[i], reason)
                self.pending_exit = None

            # 2) Gestión de stops y salidas parciales
            stop_hit = self._stop(i)
            slow_ema_touch_exit = False
            if not stop_hit:
                # Se comprueba antes de gestionar TP en esta vela: si TP3 se
                # activa ahora, el toque a EMA lenta sólo cuenta desde la vela
                # siguiente.
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
                        self.close_position(100, self.c[i], self.times[i], reason)
                    print(f"[GUARD] {self.times[i]:%Y-%m-%d} límite "
                          f"-{cfg.monthly_loss_limit_pct:g}% alcanzado; sin entradas hasta "
                          f"{scope}", flush=True)

            if not stop_hit and bool(self.signals["valid"].iloc[i]):
                target = int(self.signals["target"].iloc[i])
                if cfg.entry_atr_enabled and not self.entry_atr_allowed[i]:
                    target = 0
                if target == 1 and cfg.entry_rsi_divergence_enabled \
                        and self.entry_rsi_bear_divergence_blocked[i]:
                    target = 0
                if target == -1 and cfg.entry_rsi_divergence_enabled \
                        and self.entry_rsi_bull_divergence_blocked[i]:
                    target = 0
                if target == 1 and not cfg.allow_long: target = 0
                if target == -1 and not cfg.allow_short: target = 0
                current = self.position.side if self.position else 0
                if target and target != current and not self.entries_blocked():
                    bias_val = int(self.signals["trend_bias"].iloc[i])
                    if cfg.streak_trend_only and self.streak_in_loss and bias_val != target:
                        # Tras una pérdida sólo se entra con la tendencia mayor.
                        if self.verbose:
                            print(f"[RACHA] {self.times[i]} señal {target:+d} "
                                  f"contra tendencia {int(self.signals['trend_bias'].iloc[i]):+d}: ignorada",
                                  flush=True)
                    else:
                        self.pending_target = target
                elif self.position is not None and self.weak_exit[i]:
                    self.pending_exit = (weak_reason, cfg.ema_weakening_exit_pct)
                elif self.position is not None and self.adx_exit[i]:
                    self.pending_exit = (adx_reason, cfg.adx_exit_pct)
                elif self.position is not None:
                    # Primero se atiende la divergencia RSI confirmada.
                    div_reason = self._rsi_divergence_reason(i)
                    if div_reason is not None:
                        div_pct = cfg.rsi_divergence_exit_pct
                        if div_pct < 100:
                            div_reason = f"{div_reason}_{div_pct:g}%"
                        self.pending_exit = (div_reason, div_pct)
                    else:
                        ema55_reason = self._ema55_touch_weakness_exit_reason(i)
                        if ema55_reason is not None:
                            self.pending_exit = (ema55_reason, 100.0)
                        elif (cfg.volume_exit_enabled and not self.position.volume_exit_done
                              and i < len(self.volume_exit) and self.volume_exit[i]):
                            self.position.volume_exit_done = True
                            self.pending_exit = (volume_reason, cfg.volume_exit_pct)

            self._mark(self.times[i] + pd.Timedelta(cfg.timeframe), self.c[i])
            if (i + 1) % max(1, n // 10) == 0 or i == n - 1:
                print(f"[BACKTEST] {(i+1)/n:6.1%} | {i+1:,}/{n:,} velas | trades={len(self.trades)}", flush=True)

        if self.position is not None and cfg.force_close_at_end:
            ts = self.times[-1] + pd.Timedelta(cfg.timeframe)
            self.close_position(100, self.c[-1], ts, "END_OF_DATA")
            self._mark(ts, self.c[-1])
        trades = pd.DataFrame([asdict(t) for t in self.trades]) if self.trades else empty_trades()
        equity = pd.DataFrame({"timestamp": self.eq_time, "cash": self.eq_cash, "equity": self.eq_value,
                               "position_side": self.eq_side, "position_quantity": self.eq_qty})
        print(f"[BACKTEST] Finalizado en {time.perf_counter()-started:.2f}s | tramos={len(trades):,}", flush=True)
        return trades, equity


# ---------------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------------

#: Dataset OHLCV en Parquet; las rutas relativas cuelgan de la raíz del proyecto.
DATASET = "dataset/binance/BTCUSDT/merged/BTCUSDT-1m.parquet"
#: Directorio donde se escriben los informes del backtest.
OUTPUT_DIR = "report/backtest_out"
#: Escribir los archivos del informe.
WRITE_OUTPUTS = True
#: Generar además el CSV con la curva de equity.
EQUITY_CSV = False
#: Generar el gráfico de equity.
PLOT_ENABLED = True
#: Ruta del gráfico; ``None`` usa la ruta por defecto del informe.
PLOT_PATH: str | None = None
#: Resolución del gráfico en puntos por pulgada.
PLOT_DPI = 150
#: Mostrar el log detallado del motor de backtest.
VERBOSE = False

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

    engine = Backtester(bars, cfg, signals, adx_exit, weak_exit, rsi_div_exit,
                        verbose=VERBOSE)
    trades, equity = engine.run()
    generate_report(
        trades, equity, cfg, bars, str(dataset),
        output_dir=OUTPUT_DIR,
        write_outputs_enabled=WRITE_OUTPUTS,
        write_equity_csv=EQUITY_CSV,
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
