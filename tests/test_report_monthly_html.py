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
    assert "ATR ratio · permitido" in trace_names
    assert "ATR ratio · bloquea entrada" in trace_names
    assert "ATR warm-up · permitido" in trace_names
    assert len(figure.layout.shapes) >= 3
    assert figure.to_json()

    disabled_figure = build(Config(entry_atr_enabled=False, entry_adx_enabled=False))
    disabled_trace_names = {trace.name for trace in disabled_figure.data}
    assert "ATR" not in disabled_trace_names
    assert "ATR ratio · permitido" not in disabled_trace_names
