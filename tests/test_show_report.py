import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

import show_report


def test_report_server_disables_browser_caching(monkeypatch, tmp_path):
    (tmp_path / "report.html").write_text("<h1>reporte</h1>", encoding="utf-8")
    monkeypatch.setattr(show_report, "REPORT_DIR", tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), show_report.ReportHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/report/report.html"
        with urlopen(url, timeout=2) as response:
            assert response.read() == b"<h1>reporte</h1>"
            assert response.headers["Cache-Control"] == "no-store"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
