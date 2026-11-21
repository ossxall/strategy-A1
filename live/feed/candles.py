"""Validación y normalización de velas OHLCV."""

import math

from strategy.ohlcv import normalize_ohlcv_values


def normalize_candle(
    open_time,
    close_time,
    open_price,
    high,
    low,
    close_price,
    volume,
    quote_volume=0,
    trades=0,
    taker_buy_base=0,
    taker_buy_quote=0,
    is_closed=False,
    source="rest",
):
    op, hi, lo, cl, vol = normalize_ohlcv_values(
        open_price, high, low, close_price, volume
    )
    extra_values = [
        float(quote_volume),
        float(taker_buy_base),
        float(taker_buy_quote),
    ]
    if not all(math.isfinite(value) for value in extra_values):
        raise ValueError("La vela contiene valores no finitos")
    if int(close_time) < int(open_time):
        raise ValueError("close_time anterior a open_time")
    return {
        "open_time": int(open_time),
        "close_time": int(close_time),
        "open": op,
        "high": hi,
        "low": lo,
        "close": cl,
        "volume": vol,
        "quote_volume": extra_values[0],
        "trades": int(trades),
        "taker_buy_base": extra_values[1],
        "taker_buy_quote": extra_values[2],
        "is_closed": int(bool(is_closed)),
        "source": source,
    }
