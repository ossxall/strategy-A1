from pathlib import Path

import pandas as pd

from strategy.models import DEFAULT_OUTPUT_DIR
from strategy.report import write_outputs


def test_write_outputs_copies_report_template_inside_strategy_without_moving_it(
    tmp_path, monkeypatch
):
    template = Path(__file__).parents[1] / "strategy" / "report.html"
    original_content = template.read_bytes()
    monkeypatch.chdir(tmp_path)

    write_outputs(
        DEFAULT_OUTPUT_DIR,
        trades=pd.DataFrame(),
        equity=pd.DataFrame(),
        curve=pd.DataFrame(),
        report={"metrics": {}, "config": {}},
    )

    copied_report = tmp_path / "strategy" / "report" / "index.html"
    assert copied_report.read_bytes() == original_content
    assert template.is_file()
    assert template.read_bytes() == original_content
