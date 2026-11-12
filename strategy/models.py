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
    # Índice de barra de entrada, usado por salidas que evalúan velas desde la apertura.
    entry_bar_index: int = -1


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


TRADE_COLUMNS = list(Trade.__dataclass_fields__)
CROSS_REASON = "EMA_CROSS_EXIT"
#: Motivo de salida al superar el límite de pérdida mensual.
MONTHLY_LIMIT_REASON = "MONTHLY_LOSS_LIMIT"
#: Salida total por divergencia RSI en contra de la posición: la bajista cierra
#: un LONG (`RSI_DIVERGENCE_BEAR`) y la alcista cierra un SHORT.
RSI_DIVERGENCE_BULL_REASON = "RSI_DIVERGENCE_BULL"
RSI_DIVERGENCE_BEAR_REASON = "RSI_DIVERGENCE_BEAR"
DEFAULT_OUTPUT_DIR = "report/backtest_out"


def empty_trades() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in TRADE_COLUMNS})
