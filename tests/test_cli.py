from autodev.cli import main


def test_live_run_requires_confirmation(monkeypatch, tmp_path):
    assert main(["run-live", "--workspace", str(tmp_path)]) == 2
