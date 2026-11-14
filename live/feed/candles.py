"""Validación y normalización de velas OHLCV."""

import math


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
    values = [
        open_price,
        high,
        low,
        close_price,
        volume,
        quote_volume,
        taker_buy_base,
        taker_buy_quote,
    ]
    values = [float(v) for v in values]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("La vela contiene valores no finitos")
    op, hi, lo, cl, vol = values[:5]
    if min(op, hi, lo, cl) <= 0 or vol < 0:
        raise ValueError("Precio o volumen inválido")
    if hi < max(op, cl, lo) or lo > min(op, cl, hi):
        raise ValueError("Rango high/low inválido")
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
        "quote_volume": values[5],
        "trades": int(trades),
        "taker_buy_base": values[6],
        "taker_buy_quote": values[7],
        "is_closed": int(bool(is_closed)),
        "source": source,
    }
