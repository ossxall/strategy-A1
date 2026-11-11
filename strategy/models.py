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
    # --- Entrada parcial -----------------------------------------------------
    # target_quantity = 0 -> modo clásico (entrada completa)
    target_quantity: float = 0.0        # Cantidad total objetivo planeada
    partial_entry_done: bool = False    # True si el añadido se ejecutó o se canceló
    entry_partial_pct: float = 100.0    # % del objetivo cubierto en 1ª entrada
    entry_remaining_quantity: float = 0.0  # tramo aún pendiente, independiente de cierres parciales
    entry_bar_index: int = -1           # Índice de barra donde se hizo la 1ª entrada
    first_entry_price: float | None = None  # Precio ejecutado del primer tramo, sin promediar adds
    entry_signal_high: float | None = None  # Máximo de la vela que generó la entrada
    entry_signal_low: float | None = None   # Mínimo de la vela que generó la entrada


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
RSI_SHORT_EXIT_REASON = "RSI_SHORT_EXIT"
DEFAULT_OUTPUT_DIR = "report/backtest_out"


def empty_trades() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in TRADE_COLUMNS})
