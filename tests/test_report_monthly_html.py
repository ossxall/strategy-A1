import json
import re

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from strategy import report
from strategy.config import Config


def test_monthly_backtest_html_uses_one_paginated_chart(tmp_path, monkeypatch):
    bars = pd.DataFrame(
        index=pd.DatetimeIndex(
            ["2024-01-15T00:00:00Z", "2024-02-15T00:00:00Z"],
            name="timestamp",
        )
    )
    windows = []

    def build_figure(_bars, _cfg, **kwargs):
        windows.append(kwargs["window"])
        return go.Figure(
            data=[go.Scatter(x=[len(windows)], y=[len(windows)])],
            layout={"title": kwargs["title"]},
        )

    monkeypatch.setattr(report, "build_backtest_figure", build_figure)
    paths = report.build_monthly_backtest_htmls(
        bars,
        Config(),
        source="BTC",
        output_dir=tmp_path,
    )

    expected_path = tmp_path / "html" / "backtest.html"
    assert paths == [expected_path]
    assert expected_path.is_file()
    assert len(windows) == 2
    assert not (tmp_path / "html" / "backtest_2024-01.html").exists()

    document = expected_path.read_text(encoding="utf-8")
    assert document.count('id="chart"') == 1
    assert "justify-content:center" in document
    assert "max-width:1800px" in document
    assert "const chartHeight = Math.max(" in document
    assert "height:chartHeight || page.figure.layout.height" in document
    assert 'id="previous"' in document
    assert 'id="next"' in document
    assert "Plotly.react(chart" in document

    payload = re.search(
        r'<script id="monthly-data" type="application/json">(.*?)</script>',
        document,
        re.DOTALL,
    ).group(1)
    pages = json.loads(payload)
    assert [page["label"] for page in pages] == ["2024-01", "2024-02"]
    assert len(pages) == 2


def test_backtest_figure_visualizes_atr_filter_and_hides_it_when_disabled():
    index = pd.date_range("2024-01-01", periods=4, freq="30min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [10.0, 10.1, 10.2, 10.3],
            "high": [10.2, 10.3, 10.4, 10.5],
            "low": [9.9, 10.0, 10.1, 10.2],
            "close": [10.1, 10.2, 10.3, 10.4],
            "volume": [100.0, 110.0, 120.0, 130.0],
        },
        index=index,
    )
    signals = pd.DataFrame(
        {
            "ema_fast": [10.0, 10.1, 10.2, 10.3],
            "ema_slow": [9.9, 10.0, 10.1, 10.2],
            "atr": [0.2, 0.3, 0.1, 0.2],
            "atr_mean": [0.2, 0.2, 0.2, 0.2],
            "atr_ratio": [np.nan, 1.5, 0.4, 1.0],
            "atr_filter_ready": [False, True, True, True],
            "atr_filter_allowed": [True, True, False, True],
            "target": [0, 1, 0, -1],
        },
        index=index,
    )
    exit_flags = np.zeros(len(index), dtype=bool)

    def build(cfg):
        return report.build_backtest_figure(
            bars,
            cfg,
            signals=signals,
            trades=pd.DataFrame(),
            adx_exit=exit_flags,
            weak_exit=exit_flags,
            rsi_div_exit=np.zeros(len(index), dtype=int),
            volume_exit=exit_flags,
        )

    figure = build(Config(entry_atr_enabled=True, entry_adx_enabled=False))
    trace_names = {trace.name for trace in figure.data}
    assert figure.layout.title.x == 0.5
    assert figure.layout.margin.l == figure.layout.margin.r
    assert figure.layout.legend.x == 0.5
    assert "ATR ratio · permitido" in trace_names
    assert "ATR ratio · bloquea entrada" in trace_names
    assert "ATR warm-up · permitido" in trace_names
    assert len(figure.layout.shapes) >= 3
    assert figure.to_json()

    disabled_figure = build(Config(entry_atr_enabled=False, entry_adx_enabled=False))
    disabled_trace_names = {trace.name for trace in disabled_figure.data}
    assert "ATR" not in disabled_trace_names
    assert "ATR ratio · permitido" not in disabled_trace_names
    empty_adx_figure = build(Config(entry_atr_enabled=False, entry_adx_enabled=True))
    assert "ADX entrada" not in {trace.name for trace in empty_adx_figure.data}


def test_backtest_figure_shows_rejections_position_risk_and_drawdown():
    index = pd.date_range("2024-01-01", periods=3, freq="30min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [102.0, 103.0, 104.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [100.0, 110.0, 120.0],
        },
        index=index,
    )
    signals = pd.DataFrame(
        {
            "ema_fast": [100.0, 101.0, 102.0],
            "ema_slow": [99.0, 100.0, 101.0],
            "macd_line": [1.0, 1.5, -0.5],
            "target": [0, 1, 0],
            "trend_bias": [1, 1, -1],
            "cross_up_rejected": [False, True, False],
            "entry_rejection_reasons_long": ["", "divergencia RSI; ATR fuera de rango", ""],
            "adx_entry": [np.nan, np.nan, np.nan],
        },
        index=index,
    )
    trades = pd.DataFrame(
        [
            {
                "trade_id": 1,
                "position_id": 1,
                "side": "LONG",
                "entry_time": index[0],
                "exit_time": index[2],
                "entry_price": 100.0,
                "exit_price": 110.0,
                "quantity": 1.0,
                "position_initial_quantity": 2.0,
                "quantity_without_entry_scaling": 1.5,
                "pnl_gross": 102.0,
                "fees": 2.0,
                "pnl_net": 100.0,
                "return_pct": 10.0,
                "reason": "TAKE_PROFIT",
                "position_risk": 100.0,
                "entry_mfi_multiplier": 1.2,
                "entry_er_multiplier": 1.1,
                "entry_adaptive_multiplier": 0.9,
                "entry_size_multiplier": 1.188,
            }
        ]
    )
    equity = pd.DataFrame(
        {
            "timestamp": index + pd.Timedelta("30min"),
            "equity": [10_000.0, 9_900.0, 10_200.0],
            "position_side": ["LONG", "FLAT", "FLAT"],
            "position_quantity": [2.0, 0.0, 0.0],
            "effective_stop_price": [99.0, np.nan, np.nan],
            "position_entry_price": [100.0, np.nan, np.nan],
            "position_id": [1.0, np.nan, np.nan],
            "breakeven_activated": [False, True, False],
            "breakeven_price": [np.nan, 100.0, np.nan],
            "execution_rejection": ["", "filtro de tendencia (sesgo -1)", ""],
            "execution_rejection_time": [pd.NaT, index[1], pd.NaT],
            "execution_rejection_target": [0, 1, 0],
        }
    )
    no_flags = np.zeros(len(index), dtype=bool)
    figure = report.build_backtest_figure(
        bars,
        Config(
            entry_atr_enabled=False,
            entry_adx_enabled=False,
            volume_exit_enabled=False,
            adx_exit_enabled=False,
            ema_weakening_exit_enabled=False,
            streak_trend_only=True,
        ),
        signals=signals,
        trades=trades,
        equity=equity,
        adx_exit=no_flags,
        weak_exit=no_flags,
        rsi_div_exit=np.zeros(len(index), dtype=int),
        volume_exit=no_flags,
    )

    trace_names = {trace.name for trace in figure.data}
    assert "cruce LONG rechazado" in trace_names
    assert "entrada bloqueada" in trace_names
    assert "sesgo tendencia (tras pérdida)" in trace_names
    assert "posición real" in trace_names
    assert "stop efectivo" in trace_names
    assert "activación breakeven" in trace_names
    assert "drawdown" in trace_names
    assert "PnL neto" in trace_names
    assert "ADX entrada" not in trace_names
    assert figure.to_json()
    rejected = next(trace for trace in figure.data if trace.name == "cruce LONG rechazado")
    assert "divergencia RSI; ATR fuera de rango" in rejected.text[0]
    runtime_rejected = next(trace for trace in figure.data if trace.name == "entrada bloqueada")
    assert "filtro de tendencia" in runtime_rejected.text[0]
    summary = next(trace for trace in figure.data if trace.name == "PnL neto")
    assert any("LONG" in label for label in summary.y)
    assert any("TAKE_PROFIT" in label for label in summary.y)
    summary_axis_name = f"xaxis{summary.xaxis[1:]}" if summary.xaxis != "x" else "xaxis"
    assert getattr(figure.layout, summary_axis_name).type == "linear"
    entry = next(trace for trace in figure.data if trace.name == "entrada LONG")
    assert "Multiplicadores MFI" in entry.text[0]
    exit_trace = next(trace for trace in figure.data if trace.name == "salida +")
    assert "Cantidad del tramo" in exit_trace.text[0]
    assert "R tramo" in exit_trace.text[0]
    assert "R posición" in exit_trace.text[0]
