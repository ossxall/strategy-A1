from __future__ import annotations
from pathlib import Path
from typing import Optional
from strategy.config import Config
import time
import numpy as np
import pandas as pd


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normaliza aliases OHLCV y devuelve un DataFrame indexado por timestamp UTC."""
    df = df.rename(columns={c: str(c).strip().lower() for c in df.columns})
    aliases = {
        "timestamp": ("timestamp", "open_time", "datetime", "date", "time", "ts"),
        "open": ("open", "o"), "high": ("high", "h"), "low": ("low", "l"),
        "close": ("close", "c"), "volume": ("volume", "vol", "v"),
    }
    rename = {}
    for canonical, choices in aliases.items():
        found = next((c for c in choices if c in df.columns), None)
        if found is not None:
            rename[found] = canonical
    missing = [c for c in ("timestamp", "open", "high", "low", "close") if c not in rename.values()]
    if missing:
        raise ValueError(f"Faltan columnas {missing}; disponibles: {list(df.columns)}")
    out = df.rename(columns=rename).copy()
    if "volume" not in out:
        out["volume"] = 0.0

    ts = out["timestamp"]
    if pd.api.types.is_numeric_dtype(ts):
        nums = pd.to_numeric(ts, errors="coerce")
        finite = nums.dropna()
        magnitude = float(finite.abs().median()) if len(finite) else 0
        unit = "ns" if magnitude >= 1e17 else "us" if magnitude >= 1e14 else "ms" if magnitude >= 1e11 else "s" if magnitude >= 1e9 else None
        ts = pd.to_datetime(nums, unit=unit, utc=True, errors="coerce") if unit else pd.to_datetime(ts, utc=True, errors="coerce")
    else:
        ts = pd.to_datetime(ts, utc=True, errors="coerce")
    if ts.isna().any():
        raise ValueError("Hay timestamps inválidos.")
    out["timestamp"] = ts
    for c in ("open", "high", "low", "close", "volume"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["timestamp", "open", "high", "low", "close"])
    out = out.sort_values("timestamp").drop_duplicates("timestamp", keep="last").set_index("timestamp")
    return out[["open", "high", "low", "close", "volume"]]


def load_parquet(path: str | Path) -> pd.DataFrame:
    start = time.perf_counter()
    print(f"[LOAD] {path}", flush=True)
    data = normalize_columns(pd.read_parquet(path))
    print(f"[LOAD] {len(data):,} velas en {time.perf_counter()-start:.2f}s | {data.index.min()} -> {data.index.max()}", flush=True)
    return data


def source_minutes(data: pd.DataFrame) -> float:
    delta = data.index.to_series().diff().dropna().dt.total_seconds() / 60
    value = float(delta.median()) if len(delta) else 1.0
    return value if np.isfinite(value) and value > 0 else 1.0


def resample_ohlcv(data: pd.DataFrame, rule: str, source_interval_minutes: Optional[float] = None) -> pd.DataFrame:
    """Resamplea OHLCV y descarta buckets con menos barras que las esperadas."""
    if data.empty:
        return data.copy()
    src = source_interval_minutes or source_minutes(data)
    td = pd.Timedelta(rule)
    target_minutes = td.total_seconds() / 60
    if target_minutes < src:
        raise ValueError(f"No se puede remuestrear de {src:g}m a un timeframe menor ({rule}).")
    grouped = data.resample(rule, label="left", closed="left", origin="start_day")
    out = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    counts = grouped["close"].count()
    expected = max(1, int(round(target_minutes / src)))
    out = out[counts.reindex(out.index, fill_value=0) >= expected]
    return out.dropna(subset=["open", "high", "low", "close"])


def to_timeframe(data: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    if data.empty or pd.Timedelta(minutes=source_minutes(data)) >= pd.Timedelta(cfg.timeframe):
        return data.copy()
    out = resample_ohlcv(data, cfg.timeframe)
    print(f"[RESAMPLE] {source_minutes(data):g}m -> {cfg.timeframe}: {len(out):,} velas", flush=True)
    return out
