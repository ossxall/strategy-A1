from pathlib import Path

import pandas as pd

from strategy.report import write_outputs


def test_write_outputs_copies_report_template_without_moving_it(tmp_path):
    template = Path(__file__).parents[1] / "strategy" / "report.html"
    original_content = template.read_bytes()
    output_dir = tmp_path / "report" / "backtest_out"

    write_outputs(
        output_dir,
        trades=pd.DataFrame(),
        equity=pd.DataFrame(),
        curve=pd.DataFrame(),
        report={"metrics": {}, "config": {}},
    )

    copied_report = output_dir.parent / "index.html"
    assert copied_report.read_bytes() == original_content
    assert template.is_file()
    assert template.read_bytes() == original_content
