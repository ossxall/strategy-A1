import json

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from strategy import report
from strategy.config import Config


def test_monthly_backtest_html_renders_isolated_panels_per_page(tmp_path, monkeypatch):
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
    assert document.count('id="chart-panels"') == 1
    assert 'id="chart"' not in document
    assert 'id="chart-title"' in document
    assert "max-width:none" in document
    assert "padding:0" in document
    assert "padding:16px" in document
    assert "background:#000" in document
    assert "gap:12px" in document
    assert ".panel-card" in document
    assert "padding:0px" in document
    assert "padding:8px; border-radius:.55rem" in document
    assert "border-radius:.75rem" in document
    assert "function splitFigure(figure)" in document
    assert 'panel.title !== "precio · ADX"' in document
    assert 'trace.name === "reversal ADX"' in document
    assert 'symbol:"circle"' in document
    assert "Plotly.newPlot(plot, panel.data, panel.layout, config)" in document
    assert "plotly-4.1.1.min.js" in document
    assert "plotly-latest.min.js" not in document
    assert "fetch(url, {cache:\"no-store\"})" in document
    assert "const manifestUrl = new URL(\"data/manifest.json\"" in document
    assert "chartHeight = Math.max(" in document
    assert "max-width:100% !important" in document
    assert "Plotly.Plots.resize(plot)" in document
    assert "domain:[0, 1]" in document
    assert 'id="previous"' in document
    assert 'id="next"' in document
    assert "function requestedPage(pageCount)" in document
    assert 'url.searchParams.set("page", String(pageNumber))' in document
    assert 'await showPage(requestedPage(manifest.pages.length) - 1, "replace")' in document
    assert 'id="page-select" aria-label="Seleccionar página" disabled' in document
    assert 'pageSelect.addEventListener("change"' in document
    assert 'showPage(Number(pageSelect.value) - 1)' in document
    assert 'id="monthly-data"' not in document
    assert "2024-01" not in document
    manifest = json.loads(
        (tmp_path / "html" / "data" / "manifest.json").read_text(encoding="utf-8")
    )
    assert [page["label"] for page in manifest["pages"]] == ["2024-01", "2024-02"]
    assert len(manifest["pages"]) == 2
    assert all("figure_height" in page for page in manifest["pages"])
    first_page = json.loads(
        (tmp_path / "html" / "data" / manifest["pages"][0]["file"]).read_text(
            encoding="utf-8"
        )
    )
    assert first_page["label"] == "2024-01"
    assert first_page["figure"]["data"]


def test_entry_adx_timeframes_have_separate_panels_without_reversal_markers():
    index = pd.date_range("2024-01-01", periods=4, freq="30min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0],
            "high": [102.0, 103.0, 104.0, 105.0],
            "low": [99.0, 100.0, 101.0, 102.0],
            "close": [101.0, 102.0, 103.0, 104.0],
            "volume": [100.0, 110.0, 120.0, 130.0],
        },
        index=index,
    )
    signals = pd.DataFrame(
        {
            "adx_entry_30min": [18.0, 22.0, 24.0, 23.0],
            "adx_entry_1h": [np.nan, 22.0, 25.0, 24.0],
            "entry_gaussian_trend_line": [100.0, 101.0, 102.0, 103.0],
            "entry_gaussian_trend_dir": [1, 1, -1, -1],
        },
        index=index,
    )
    no_flags = np.zeros(len(index), dtype=bool)
    figure = report.build_backtest_figure(
        bars,
        Config(
            entry_adx_timeframe=("30min", "1h"),
            entry_atr_enabled=False,
            entry_gaussian_trend_enabled=False,
            gaussian_trend_reversal_exit_enabled=False,
            adx_exit_enabled=False,
            entry_rsi_divergence_enabled=False,
            exit_rsi_divergence_enabled=False,
            streak_trend_only=False,
        ),
        signals=signals,
        trades=pd.DataFrame(),
        adx_exit=no_flags,
        rsi_div_exit=np.zeros(len(index), dtype=int),
    )

    traces = {trace.name: trace for trace in figure.data}
    assert "ADX entrada 30min" in traces
    assert "ADX entrada 1h" in traces
    assert not any("reversión ADX entrada" in name for name in traces)
    assert traces["ADX entrada 30min"].xaxis != traces["ADX entrada 1h"].xaxis


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
            "entry_gaussian_trend_line": [10.0, 10.1, 10.2, 10.3],
            "entry_gaussian_trend_dir": [1, 1, -1, -1],
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
            rsi_div_exit=np.zeros(len(index), dtype=int),
        )

    figure = build(Config(entry_atr_enabled=True, entry_adx_enabled=False))
    trace_names = {trace.name for trace in figure.data}
    assert figure.layout.title.x == 0.5
    assert figure.layout.margin.l == figure.layout.margin.r
    assert figure.layout.legend.x == 0.5
    assert "ATR ratio · permitido" in trace_names
    assert "ATR ratio · bloquea entrada" in trace_names
    assert "ATR warm-up · permitido" in trace_names
    assert "Gaussian Filter Trend ↑" in trace_names
    assert "Gaussian Filter Trend ↓" in trace_names
    assert len(figure.layout.shapes) >= 3
    assert figure.to_json()

    disabled_figure = build(Config(entry_atr_enabled=False, entry_adx_enabled=False))
    disabled_trace_names = {trace.name for trace in disabled_figure.data}
    assert "ATR" not in disabled_trace_names
    assert "ATR ratio · permitido" not in disabled_trace_names
    assert "Gaussian Filter Trend ↑" in disabled_trace_names
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
            "cross_down_rejected": [False, False, True],
            "entry_rejection_reasons_short": ["", "", "separación EMA insuficiente"],
            "ema_cross_gap_atr": [np.nan, np.nan, 0.08],
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
                "pnl_gross": 102.0,
                "fees": 2.0,
                "pnl_net": 100.0,
                "return_pct": 10.0,
                "reason": "TAKE_PROFIT",
                "position_risk": 100.0,
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
    cfg = Config(
        entry_atr_enabled=False,
        entry_adx_enabled=False,
        adx_exit_enabled=False,
        streak_trend_only=True,
    )
    figure = report.build_backtest_figure(
        bars,
        cfg,
        signals=signals,
        trades=trades,
        equity=equity,
        adx_exit=no_flags,
        rsi_div_exit=np.zeros(len(index), dtype=int),
    )

    trace_names = {trace.name for trace in figure.data}
    assert "cruce LONG rechazado" in trace_names
    assert "cruce SHORT rechazado · separación EMA insuficiente" in trace_names
    assert "entrada bloqueada" in trace_names
    assert "sesgo tendencia (tras pérdida)" not in trace_names
    assert "posición real" not in trace_names
    assert "señal objetivo (filtrada)" not in trace_names
    assert "stop efectivo" in trace_names
    assert "activación breakeven" in trace_names
    assert "drawdown" in trace_names
    assert "PnL neto" in trace_names
    assert "ADX entrada" not in trace_names
    assert figure.to_json()
    rejected = next(trace for trace in figure.data if trace.name == "cruce LONG rechazado")
    assert "divergencia RSI; ATR fuera de rango" in rejected.text[0]
    gap_rejected = next(
        trace for trace in figure.data
        if trace.name == "cruce SHORT rechazado · separación EMA insuficiente"
    )
    assert "separación EMA insuficiente" in gap_rejected.text[0]
    assert "Brecha: 0.080 ATR" in gap_rejected.text[0]
    assert f"Mínimo: {cfg.ema_cross_min_gap_atr:.2f} ATR" in gap_rejected.text[0]
    runtime_rejected = next(trace for trace in figure.data if trace.name == "entrada bloqueada")
    assert "filtro de tendencia" in runtime_rejected.text[0]
    summary = next(trace for trace in figure.data if trace.name == "PnL neto")
    assert any("LONG" in label for label in summary.y)
    assert any("TAKE_PROFIT" in label for label in summary.y)
    summary_axis_name = f"xaxis{summary.xaxis[1:]}" if summary.xaxis != "x" else "xaxis"
    assert getattr(figure.layout, summary_axis_name).type == "linear"
    entry = next(trace for trace in figure.data if trace.name == "entrada LONG")
    assert "Riesgo inicial" in entry.text[0]
    exit_trace = next(trace for trace in figure.data if trace.name == "salida +")
    assert "Cantidad del tramo" in exit_trace.text[0]
    assert "R tramo" in exit_trace.text[0]
    assert "R posición" in exit_trace.text[0]
