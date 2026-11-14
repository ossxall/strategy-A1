import json
import re

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
