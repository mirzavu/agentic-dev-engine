import json
import subprocess
from pathlib import Path

import pytest

from autodev.config import Settings

from autodev.runner import (CheckResult, RunFailure, VerificationCommand, redact)
from autodev.workspace import workspace_mount


def command(name: str, code: int) -> CheckResult:
    return CheckResult(VerificationCommand(name, ("tool", name)), code, "failure sk-abcdefghijklmnop")


def settings(*, fallback: bool = False) -> Settings:
    return Settings("agent", None, Path("/missing/auth.json"), 2, 3, 60, 10, 10, 30, 3, 8, fallback)


def test_mount_contains_only_workspace(tmp_path):
    assert workspace_mount(tmp_path).endswith(":/workspace:rw")
    assert str(tmp_path.resolve()) in workspace_mount(tmp_path)


def test_permission_normalization_is_networkless_and_symlink_safe(monkeypatch, tmp_path):
    captured = {}
    class Result:
        returncode = 0
        stderr = ""
    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return Result()
    monkeypatch.setattr("autodev.workspace.subprocess.run", fake_run)
    from autodev.workspace import normalize_permissions
    normalize_permissions(tmp_path)
    command = captured["argv"]
    assert "--network" in command and "none" in command
    assert "find -P /workspace" in command[command.index("sh") + 2]
    assert "chown -h" in command[command.index("sh") + 2]
    assert "/workspace/node_modules" in command[command.index("sh") + 2]
