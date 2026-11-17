import importlib.util
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1] / "live" / "ssh" / "start_webssh.py"
)
SPEC = importlib.util.spec_from_file_location("start_webssh", SCRIPT)
start_webssh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(start_webssh)


def test_launcher_uses_webssh_public_path_and_module(monkeypatch, tmp_path, capsys):
    python = str(tmp_path / "python")
    monkeypatch.setattr(start_webssh, "ensure_webssh", lambda: python)
    monkeypatch.setenv("WEBSSH_HOST", "127.0.0.1")
    monkeypatch.setenv("WEBSSH_PORT", "9000")
    monkeypatch.setenv("WEBSSH_POLICY", "reject")
    monkeypatch.setenv("WEBSSH_PUBLIC_URL", "https://example.test/webssh/")

    exec_call = []

    monkeypatch.setattr(
        start_webssh.os,
        "execv",
        lambda executable, args: exec_call.append((executable, args)),
    )

    start_webssh.main()

    assert "https://example.test/webssh/" in capsys.readouterr().out
    assert exec_call == [
        (
            python,
            [
                python,
                "-m",
                "webssh.main",
                "--address=127.0.0.1",
                "--port=9000",
                "--policy=reject",
            ],
        )
    ]


def test_webssh_dependency_comes_from_shared_requirements():
    assert start_webssh._webssh_requirement() == "webssh==1.6.3"


def test_launcher_reuses_python_that_already_has_webssh(monkeypatch):
    monkeypatch.setattr(
        start_webssh,
        "_can_import_webssh",
        lambda python: python == start_webssh.sys.executable,
    )

    assert start_webssh.ensure_webssh() == start_webssh.sys.executable
