from __future__ import annotations
from dataclasses import dataclass
import pandas as pd


def timeframe_tuple(raw: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Normalize one or several timeframes into a tuple.

    Accepts a comma-separated ``str`` (``"1D,30min"``) or a sequence
    (``["1D", "30min"]``) and returns a tuple with empty entries removed.
    """
    if isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        parts = str(raw).split(",")
    return tuple(part.strip() for part in parts if part.strip())


@dataclass
class Config:
    """Configuration for the EMA/MACD crossover strategy and its exits.

    All timeframe strings follow pandas offset aliases (e.g. "30min", "1h", "1D").
    Validation of ranges and consistency is performed in ``__post_init__``.
    """
    #
    # Fast EMA period used for the entry crossover.
    #
    ema_fast: int = 55
    #
    # Slow EMA period used for the entry crossover.
    #
    ema_slow: int = 200
    #
    # MACD periods for entry signals: the signal line is compared against zero.
    #
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    #
    # None = use the strategy's main timeframe.
    #
    macd_timeframe: str | None = "1h"
    #
    # Main strategy timeframe.
    #
    timeframe: str = "30min"
    #
    # Whether long positions are allowed.
    #
    allow_long: bool = True
    #
    # Whether short positions are allowed.
    #
    allow_short: bool = True
    #
    # Initial account equity.
    #
    initial_capital: float = 10_000.0
    #
    # Commission cost in basis points per trade.
    #
    commission_bps: float = 0.0
    #
    # Slippage cost in basis points per trade.
    #
    slippage_bps: float = 0.0
    #
    # Fraction of equity risked per trade.
    #
    risk_pct: float = 0.01
    #
    # Maximum leverage applied to position sizing.
    #
    max_leverage: float = 1.0
    #
    # Stop-loss distance as a fraction of entry price.
    #
    stop_loss_pct: float = 0.01
    #
    # Bars required to confirm an EMA crossover before acting.
    #
    confirm_bars: int = 4
    #
    # Block EMA crossovers when the entry ADX is below the minimum.
    #
    entry_adx_enabled: bool = True
    entry_adx_len: int = 14
    entry_adx_min: float = 20.0
    #
    # All listed timeframes must clear the threshold to allow an entry.
    #
    entry_adx_timeframe: str | list[str] | tuple[str, ...] = ("30min")
    #
    # Block entries when ATR(14) is abnormally low/high versus its mean over the
    # previous candles. Does not affect exits of open positions.
    #
    entry_atr_enabled: bool = True
    entry_atr_len: int = 14
    entry_atr_lookback: int = 50
    entry_atr_min_ratio: float = 0.5
    entry_atr_max_ratio: float = 2.5
    #
    # Block entries when, at crossover confirmation, price sits on the wrong side
    # of both EMAs of the crossover: a LONG is not opened if the close is below
    # both fast and slow EMA; a SHORT is not opened if the close is above both.
    # Uses `ema_fast`/`ema_slow` of the strategy timeframe (does not affect exits).
    #
    entry_price_ema_filter_enabled: bool = True
    #
    # Block LONG entries after a bullish RSI divergence and SHORT entries after a
    # bearish one, within the last N bars of the strategy timeframe.
    #
    entry_rsi_divergence_enabled: bool = True
    entry_rsi_divergence_lookback: int = 20
    #
    # MFI additionally scales position size according to money flow in favor.
    #
    entry_mfi_risk_scaling_enabled: bool = True
    entry_mfi_len: int = 14
    entry_mfi_risk_base: float = 1.0
    entry_mfi_risk_spread_scale: float = 100.0
    entry_mfi_risk_min: float = 0.5
    entry_mfi_risk_max: float = 1.25
    #
    # Efficiency Ratio smooths position size between ranging and trending regimes.
    #
    entry_er_risk_scaling_enabled: bool = True
    entry_er_len: int = 20
    entry_er_risk_base: float = 0.75
    entry_er_risk_slope: float = 1.0
    entry_er_risk_min: float = 0.5
    entry_er_risk_max: float = 1.25
    #
    # ADX-based exit (reversal) configuration.
    #
    adx_exit_enabled: bool = True
    #
    # One or several timeframes (combined with OR). Accepts "1D", "1D,30min" or
    # ["1D", "30min"]; the ADX exit triggers if a reversal occurs in any of them.
    #
    adx_timeframe: str | list[str] | tuple[str, ...] = ("1D")
    #
    # Window (in strategy-timeframe bars) during which an ADX reversal keeps the
    # exit active.
    #
    adx_exit_range: int = 9
    adx_dilen: int = 14
    adx_adxlen: int = 14
    adx_key_level: float = 23.0
    adx_exit_pct: float = 100.0
    #
    # EMA-weakening exit configuration.
    #
    ema_weakening_exit_enabled: bool = True
    #
    # One or several timeframes (combined with OR). Accepts "1D", "1D,30min" or
    # ["1D", "30min"]; the weakening exit triggers if weakening occurs in any.
    #
    ema_weakening_timeframe: str | list[str] | tuple[str, ...] = ("4h", "1D")
    #
    # Lengths of the EMAs used by the weakening exit. If > 0 they override
    # `ema_fast`/`ema_slow` (allowing a different period without touching the
    # entry crossover); with 0 the crossover EMAs are reused.
    #
    ema_weakening_fast: int = 21
    ema_weakening_slow: int = 55
    ema_weakening_gap_pct: float = 0.0001
    ema_weakening_exit_pct: float = 100.0
    #
    # --- RSI divergence exit -----------------------------------------------
    # Closes 100% of the position when an RSI divergence appears against it:
    # a **bearish** one (price higher highs, RSI lower highs) closes a LONG and
    # a **bullish** one (price lower lows, RSI higher lows) closes a SHORT. A
    # price pivot is confirmed after `rsi_divergence_pivot` bars on each side
    # and is compared with the previous pivot of the same type within
    # `rsi_divergence_lookback` bars; the signal is emitted on pivot
    # confirmation, without lookahead.
    #
    rsi_divergence_exit_enabled: bool = True
    rsi_divergence_exit_pct: float = 100.0  
    #
    # One or several timeframes (combined with OR), like the other exits.
    #
    rsi_divergence_timeframe: str | list[str] | tuple[str, ...] = "1D"
    rsi_divergence_len: int = 14       # RSI period (Wilder)
    rsi_divergence_pivot: int = 5      # Bars on each side to confirm a pivot
    rsi_divergence_lookback: int = 60  # Lookback window for the previous pivot
    #
    # --- EMA55 touch weakness exit (30m only) ------------------------------
    # A touch is a candle range intersecting EMA55 ± tolerance. Exit triggers
    # after 2 consecutive touches or 3 within 5 bars, without a breakout and
    # with an open loss.
    #
    ema55_touch_exit_enabled: bool = True
    ema55_touch_tolerance_pct: float = 0.1
    ema55_touch_lookback_bars: int = 5
    ema55_touch_min_touches: int = 3
    ema55_touch_consecutive: int = 2
    ema55_touch_breakout_buffer_pct: float = 0.1
    ema55_touch_exit_loss_pct: float = 0.7        # LONG
    ema55_touch_short_exit_loss_pct: float = 0.9  # SHORT, stricter threshold
    #
    # --- Partial exit on decreasing volume ---------------------------------
    # Closes `volume_exit_pct` % of the position (once per position) when the
    # average volume of the recent half of the last `volume_exit_bars` candles
    # drops by at least `volume_exit_min_drop_pct` % versus the earlier half.
    # E.g. with 10 bars and 20%, the mean of the last 5 must be <= 80% of the
    # mean of the previous 5.
    #
    volume_exit_enabled: bool = True
    volume_exit_bars: int = 20
    volume_exit_pct: float = 20.0
    volume_exit_min_drop_pct: float = 20.0
    #
    # Exit management aimed at reducing whipsaw loss streaks: when price reaches
    # `partial_tp_r` times the initial risk, profit is locked by closing
    # `partial_tp_pct` of the position and, optionally, moving the stop of the
    # remainder to breakeven. Raises the real win rate (breaks consecutive
    # stop-loss streaks) at the cost of trimming part of the return.
    #
    partial_tp_enabled: bool = True
    partial_tp_r: float = 1.0
    partial_tp_pct: float = 10.0
    #
    # Second partial take-profit (optional). If enabled (>0), it triggers at
    # partial_tp2_r (R-multiple of initial risk) closing partial_tp2_pct of the
    # position. Partial 2 is intended to be at a larger distance than TP1.
    #
    partial_tp2_r: float = 1.5
    partial_tp2_pct: float = 10.0
    #
    # Third partial take-profit, at a larger distance than TP2.
    #
    partial_tp3_r: float = 2.0
    partial_tp3_pct: float = 10.0
    #
    # Fourth partial take-profit, at a larger distance than TP3.
    #
    partial_tp4_r: float = 2.5
    partial_tp4_pct: float = 10.0
    #
    # Fifth partial take-profit, at a larger distance than TP4.
    #
    partial_tp5_r: float = 3.0
    partial_tp5_pct: float = 10.0
    #
    # Sixth partial take-profit, at a larger distance than TP5.
    #
    partial_tp6_r: float = 3.5
    partial_tp6_pct: float = 10.0
    #
    # Seventh partial take-profit, at a larger distance than TP6.
    #
    partial_tp7_r: float = 4.0
    partial_tp7_pct: float = 10.0
    #
    # After TP3, the remaining SHORT exits if price exceeds the slow EMA by
    # this %. 0 = touching it is enough.
    #
    tp3_ema_slow_penetration_pct: float = 1.75
    #
    # Whether to move the stop to breakeven after a partial take-profit.
    #
    move_stop_to_breakeven: bool = True
    #
    # Higher-timeframe trend bias, used only after a loss.
    #
    trend_timeframe: str = "1D"
    #
    # --- Monthly protection -----------------------------------------------
    # If equity falls more than `monthly_loss_limit_pct` % below the equity at
    # the start of the calendar month, new positions stop being opened (and, with
    # monthly_loss_limit_close, the open one is closed) until the next month or,
    # if `monthly_loss_limit_days` > 0, for that many days. Safety net for the
    # worst month. With defaults it barely triggers (1 time in 82 months), so it
    # starts loose at 5.0. Adaptive sizing also trims red months; lowering the
    # limit (e.g. 2) improves the worst month but trims cumulative negative
    # months. 0 = disabled.
    #
    monthly_loss_limit_pct: float = 5.0
    monthly_loss_limit_close: bool = True
    monthly_loss_limit_days: int = 0
    #
    # After a position closed at a loss, the next entry only opens if it aligns
    # with the trend bias of `trend_timeframe` (already-closed higher bar, no
    # lookahead); a win resets the requirement. Targets loss streaks: in
    # whipsaw the crossover signal often runs against the larger trend. Only
    # demands the bias after a loss, not always, to avoid trimming returns of
    # crossovers in trend. On BTCUSDT 2020-2026 reduces the max streak from 7
    # to 5 (train and test), raises PF from 1.76 to 2.07 and trims the worst
    # month from -4.75% to -2.65%, with total return above baseline
    # (328% -> 363%). False = disabled.
    #
    streak_trend_only: bool = True
    #
    # --- Adaptive position sizing ------------------------------------------
    # `risk_pct` is multiplied by `clamp(1 + k * (win rate of last n positions
    # - 0.5), min, max)`: less risk in loss streaks and more in winning streaks
    # (trending market). Red months are usually cold streaks, so risk is trimmed
    # exactly when losses occur: with `adaptive_risk_window=20` and `k=2` the
    # worst month goes from -3.15% to -2.49%, cumulative negative months from
    # -28.6% to -26.1% and PF from 2.43 to 2.50, at the cost of ~38 points of
    # total return. 0 = disabled (fixed size).
    #
    adaptive_risk_window: int = 20
    adaptive_risk_k: float = 2.0
    adaptive_risk_min: float = 0.5
    adaptive_risk_max: float = 1.5

    force_close_at_end: bool = True

    @property
    def adx_timeframes(self) -> tuple[str, ...]:
        """ADX timeframes normalized (one or several); exit uses OR across them."""
        return timeframe_tuple(self.adx_timeframe)

    @property
    def entry_adx_timeframes(self) -> tuple[str, ...]:
        """Entry ADX timeframes normalized; all of them must pass the threshold."""
        return timeframe_tuple(self.entry_adx_timeframe)

    @property
    def ema_weakening_timeframes(self) -> tuple[str, ...]:
        """Weakening timeframes normalized (one or several); exit uses OR across them."""
        return timeframe_tuple(self.ema_weakening_timeframe)

    @property
    def ema_weakening_lengths(self) -> tuple[int, int]:
        """Lengths (fast, slow) of the EMAs used by the weakening exit.

        Uses `ema_weakening_fast`/`ema_weakening_slow` when defined (> 0);
        otherwise falls back to the crossover EMAs (`ema_fast`/`ema_slow`).
        """
        fast = self.ema_weakening_fast if self.ema_weakening_fast > 0 else self.ema_fast
        slow = self.ema_weakening_slow if self.ema_weakening_slow > 0 else self.ema_slow
        return fast, slow

    @property
    def rsi_divergence_timeframes(self) -> tuple[str, ...]:
        """RSI divergence timeframes normalized; exit uses OR across them."""
        return timeframe_tuple(self.rsi_divergence_timeframe)

    def __post_init__(self):
        """Validate ranges and internal consistency of the configuration."""
        if min(self.ema_fast, self.ema_slow) <= 0 or self.ema_fast >= self.ema_slow:
            raise ValueError("Requires 0 < ema_fast < ema_slow.")
        if min(self.macd_fast, self.macd_slow, self.macd_signal) <= 0 or self.macd_fast >= self.macd_slow:
            raise ValueError("Requires 0 < macd_fast < macd_slow and macd_signal > 0.")
        if self.macd_timeframe is not None and pd.Timedelta(self.macd_timeframe) <= pd.Timedelta(0):
            raise ValueError("macd_timeframe must be positive or None to use timeframe.")
        if pd.Timedelta(self.timeframe) <= pd.Timedelta(0):
            raise ValueError("timeframe must be positive.")
        for timeframe in self.adx_timeframes:
            if pd.Timedelta(timeframe) <= pd.Timedelta(0):
                raise ValueError(f"invalid adx_timeframe: {timeframe!r}.")
        if self.adx_exit_enabled and not self.adx_timeframes:
            raise ValueError("adx_timeframe cannot be empty.")
        for timeframe in self.entry_adx_timeframes:
            if pd.Timedelta(timeframe) <= pd.Timedelta(0):
                raise ValueError(f"invalid entry_adx_timeframe: {timeframe!r}.")
        if self.entry_adx_enabled and not self.entry_adx_timeframes:
            raise ValueError("entry_adx_timeframe cannot be empty.")
        if self.ema_weakening_exit_enabled and not self.ema_weakening_timeframes:
            raise ValueError("ema_weakening_timeframe cannot be empty.")
        for timeframe in self.ema_weakening_timeframes:
            if pd.Timedelta(timeframe) <= pd.Timedelta(0):
                raise ValueError(f"invalid ema_weakening_timeframe: {timeframe!r}.")
        if (self.rsi_divergence_exit_enabled or self.entry_rsi_divergence_enabled) \
                and not self.rsi_divergence_timeframes:
            raise ValueError("rsi_divergence_timeframe cannot be empty.")
        for timeframe in self.rsi_divergence_timeframes:
            if pd.Timedelta(timeframe) <= pd.Timedelta(0):
                raise ValueError(f"invalid rsi_divergence_timeframe: {timeframe!r}.")
        if self.initial_capital <= 0 or self.max_leverage <= 0:
            raise ValueError("Capital and leverage must be positive.")
        if not 0 < self.risk_pct <= 0.2:
            raise ValueError("risk_pct must be in (0, 0.2].")
        if min(self.commission_bps, self.slippage_bps, self.stop_loss_pct) < 0:
            raise ValueError("Commissions, slippage and stop cannot be negative.")
        if self.confirm_bars < 0:
            raise ValueError("confirm_bars cannot be negative.")
        if self.entry_adx_enabled and (self.entry_adx_len <= 0 or self.entry_adx_min < 0):
            raise ValueError("Invalid entry ADX configuration.")
        if self.entry_atr_enabled and (
            self.entry_atr_len <= 0 or self.entry_atr_lookback <= 0
            or self.entry_atr_min_ratio < 0
            or self.entry_atr_max_ratio <= self.entry_atr_min_ratio
        ):
            raise ValueError("Invalid entry ATR filter configuration.")
        if self.entry_rsi_divergence_enabled and self.entry_rsi_divergence_lookback < 1:
            raise ValueError("entry_rsi_divergence_lookback must be >= 1.")
        if self.entry_mfi_risk_scaling_enabled and (
            self.entry_mfi_len <= 0 or self.entry_mfi_risk_spread_scale <= 0
            or self.entry_mfi_risk_min <= 0
            or self.entry_mfi_risk_max < self.entry_mfi_risk_min
        ):
            raise ValueError("Invalid MFI risk scaling configuration.")
        if self.entry_er_risk_scaling_enabled and (
            self.entry_er_len <= 0 or self.entry_er_risk_slope < 0
            or self.entry_er_risk_min <= 0
            or self.entry_er_risk_max < self.entry_er_risk_min
        ):
            raise ValueError("Invalid Efficiency Ratio scaling configuration.")
        if self.adx_exit_enabled:
            if min(self.adx_dilen, self.adx_adxlen) <= 0 or self.adx_key_level < 0:
                raise ValueError("Invalid exit ADX configuration.")
            if not 0 < self.adx_exit_pct <= 100:
                raise ValueError("adx_exit_pct must be in (0, 100].")
            if self.adx_exit_range < 1:
                raise ValueError("adx_exit_range must be >= 1.")
        if self.ema_weakening_exit_enabled:
            if self.ema_weakening_gap_pct <= 0 or not 0 < self.ema_weakening_exit_pct <= 100:
                raise ValueError("Invalid weakening configuration.")
            if min(self.ema_weakening_fast, self.ema_weakening_slow) < 0:
                raise ValueError("ema_weakening_fast/ema_weakening_slow cannot be negative.")
            if self.ema_weakening_slow > 0 and self.ema_weakening_fast > 0 \
                    and self.ema_weakening_fast >= self.ema_weakening_slow:
                raise ValueError("Requires ema_weakening_fast < ema_weakening_slow (or 0 to use crossover ones).")
        if self.rsi_divergence_exit_enabled or self.entry_rsi_divergence_enabled:
            if self.rsi_divergence_len <= 0:
                raise ValueError("rsi_divergence_len must be positive.")
            if self.rsi_divergence_pivot < 1:
                raise ValueError("rsi_divergence_pivot must be >= 1.")
            if self.rsi_divergence_lookback < 1:
                raise ValueError("rsi_divergence_lookback must be >= 1.")
        if self.rsi_divergence_exit_enabled and not 0 < self.rsi_divergence_exit_pct <= 100:
            raise ValueError("rsi_divergence_exit_pct must be in (0, 100].")
        if (self.ema55_touch_tolerance_pct < 0 or self.ema55_touch_breakout_buffer_pct < 0
                or self.ema55_touch_exit_loss_pct < 0
                or self.ema55_touch_short_exit_loss_pct < 0):
            raise ValueError("EMA55 tolerances and minimum loss cannot be negative.")
        if self.tp3_ema_slow_penetration_pct < 0:
            raise ValueError("tp3_ema_slow_penetration_pct cannot be negative.")
        if min(self.ema55_touch_lookback_bars, self.ema55_touch_min_touches,
               self.ema55_touch_consecutive) < 1:
            raise ValueError("EMA55 touch window and counts must be >= 1.")
        if self.volume_exit_enabled:
            if self.volume_exit_bars < 2:
                raise ValueError("volume_exit_bars must be >= 2.")
            if not 0 < self.volume_exit_pct <= 100:
                raise ValueError("volume_exit_pct must be in (0, 100].")
            if not 0 <= self.volume_exit_min_drop_pct < 100:
                raise ValueError("volume_exit_min_drop_pct must be in [0, 100).")
        if self.partial_tp_enabled:
            if self.partial_tp_r <= 0 or not 0 < self.partial_tp_pct <= 100:
                raise ValueError("Invalid partial take-profit configuration.")
        if self.partial_tp2_r > 0 or self.partial_tp2_pct > 0:
            if self.partial_tp2_r <= 0 or not 0 < self.partial_tp2_pct <= 100:
                raise ValueError("Invalid partial take-profit 2 configuration.")
            if self.partial_tp2_r <= self.partial_tp_r:
                raise ValueError("partial_tp2_r must be greater than partial_tp_r.")
        if self.partial_tp3_r > 0 or self.partial_tp3_pct > 0:
            if self.partial_tp3_r <= 0 or not 0 < self.partial_tp3_pct <= 100:
                raise ValueError("Invalid partial take-profit 3 configuration.")
            if self.partial_tp3_r <= self.partial_tp2_r:
                raise ValueError("partial_tp3_r must be greater than TP2.")
        if self.partial_tp4_r > 0 or self.partial_tp4_pct > 0:
            if self.partial_tp4_r <= 0 or not 0 < self.partial_tp4_pct <= 100:
                raise ValueError("Invalid partial take-profit 4 configuration.")
            if self.partial_tp4_r <= self.partial_tp3_r:
                raise ValueError("partial_tp4_r must be greater than TP3.")
        if self.partial_tp5_r > 0 or self.partial_tp5_pct > 0:
            if self.partial_tp5_r <= 0 or not 0 < self.partial_tp5_pct <= 100:
                raise ValueError("Invalid partial take-profit 5 configuration.")
            if self.partial_tp5_r <= self.partial_tp4_r:
                raise ValueError("partial_tp5_r must be greater than TP4.")
        if self.partial_tp6_r > 0 or self.partial_tp6_pct > 0:
            if self.partial_tp6_r <= 0 or not 0 < self.partial_tp6_pct <= 100:
                raise ValueError("Invalid partial take-profit 6 configuration.")
            if self.partial_tp6_r <= self.partial_tp5_r:
                raise ValueError("partial_tp6_r must be greater than TP5.")
        if self.partial_tp7_r > 0 or self.partial_tp7_pct > 0:
            if self.partial_tp7_r <= 0 or not 0 < self.partial_tp7_pct <= 100:
                raise ValueError("Invalid partial take-profit 7 configuration.")
            if self.partial_tp7_r <= self.partial_tp6_r:
                raise ValueError("partial_tp7_r must be greater than TP6.")
        if not (self.allow_long or self.allow_short):
            raise ValueError("LONG, SHORT or both must be enabled.")
        if self.streak_trend_only and pd.Timedelta(self.trend_timeframe) <= pd.Timedelta(0):
            raise ValueError("trend_timeframe must be positive.")
        if self.monthly_loss_limit_pct < 0:
            raise ValueError("monthly_loss_limit_pct cannot be negative.")
        if self.monthly_loss_limit_days < 0:
            raise ValueError("monthly_loss_limit_days cannot be negative.")
        if self.adaptive_risk_window < 0:
            raise ValueError("adaptive_risk_window cannot be negative.")
        if self.adaptive_risk_window and not (0 < self.adaptive_risk_min <= self.adaptive_risk_max):
            raise ValueError("Requires 0 < adaptive_risk_min <= adaptive_risk_max.")
