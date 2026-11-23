from __future__ import annotations
from dataclasses import dataclass
import pandas as pd

@dataclass
class Position:
    position_id: int
    side: int
    quantity: float
    initial_quantity: float
    entry_price: float
    entry_time: pd.Timestamp
    entry_fee: float
    stop_pct: float
    risk_amount: float
    breakeven: bool = False
    partial_done: bool = False
    partial2_done: bool = False
    partial3_done: bool = False
    partial4_done: bool = False
    partial5_done: bool = False
    partial6_done: bool = False
    partial7_done: bool = False
    partial8_done: bool = False
    partial9_done: bool = False
    partial10_done: bool = False
    partial11_done: bool = False
    partial12_done: bool = False
    partial13_done: bool = False
    partial14_done: bool = False
    partial15_done: bool = False
    partial16_done: bool = False
    partial17_done: bool = False
    partial18_done: bool = False
    partial19_done: bool = False
    partial20_done: bool = False
    partial21_done: bool = False
    partial22_done: bool = False
    partial23_done: bool = False
    partial24_done: bool = False
    partial25_done: bool = False
    partial26_done: bool = False
    partial27_done: bool = False
    partial28_done: bool = False
    partial29_done: bool = False
    partial30_done: bool = False
    partial31_done: bool = False
    partial32_done: bool = False
    partial33_done: bool = False
    partial34_done: bool = False
    partial35_done: bool = False
    partial36_done: bool = False
    partial37_done: bool = False
    partial38_done: bool = False
    partial39_done: bool = False
    partial40_done: bool = False


@dataclass
class Trade:
    trade_id: int
    position_id: int
    side: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    quantity: float
    pnl_gross: float
    fees: float
    pnl_net: float
    return_pct: float
    reason: str
    position_risk: float
    position_initial_quantity: float = 0.0


TRADE_COLUMNS = list(Trade.__dataclass_fields__)
CROSS_REASON = "EMA_CROSS_EXIT"
#: Motivo de salida al superar el límite de pérdida mensual.
MONTHLY_LIMIT_REASON = "MONTHLY_LOSS_LIMIT"
#: Salida total por divergencia RSI en contra de la posición: la bajista cierra
#: un LONG (`RSI_DIVERGENCE_BEAR`) y la alcista cierra un SHORT.
RSI_DIVERGENCE_BULL_REASON = "RSI_DIVERGENCE_BULL"
RSI_DIVERGENCE_BEAR_REASON = "RSI_DIVERGENCE_BEAR"
#: Salida parcial al girar el Gaussian Filter Trend contra una posición.
GAUSSIAN_TREND_BULL_REASON = "GAUSSIAN_TREND_BULL"
GAUSSIAN_TREND_BEAR_REASON = "GAUSSIAN_TREND_BEAR"
DEFAULT_OUTPUT_DIR = "strategy/report/backtest_out"


def empty_trades() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in TRADE_COLUMNS})
