from __future__ import annotations

import json
import os
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator, Protocol
from urllib.error import URLError
from urllib.request import urlopen

from .config import Settings
from .workspace import LoopbackDockerWorkspace, available_loopback_port, normalize_permissions


class RunFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class VerificationCommand:
    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class CheckResult:
    command: VerificationCommand
    exit_code: int
    output: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


def redact(text: str, secrets: list[str]) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    return re.sub(r"\b(?:sk|rk|api)[-_][A-Za-z0-9_-]{12,}\b", "[REDACTED]", result)


def load_verification_manifest(app: Path) -> list[VerificationCommand]:
    path = app / ".autodev" / "verification.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RunFailure("Missing or invalid .autodev/verification.json") from exc
    if not isinstance(data, dict):
        raise RunFailure("Verification manifest must be a JSON object")
    commands: list[VerificationCommand] = []
    for name in ("install", "test", "build"):
        argv = data.get(name)
        if argv is None and name == "build":
            continue
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise RunFailure(f"Verification manifest field '{name}' must be a non-empty token array")
        commands.append(VerificationCommand(name, tuple(argv)))
    if [item.name for item in commands[:2]] != ["install", "test"]:
        raise RunFailure("Verification manifest must define install and test commands")
    readme = app / "README.md"
    test_line = shlex.join(commands[1].argv)
    if not readme.is_file() or test_line not in readme.read_text(errors="replace"):
        raise RunFailure(f"README.md must document the test command: {test_line}")
    return commands


def verify_in_workspace(workspace, app: Path, commands: list[VerificationCommand], *, command_timeout: float = 600.0) -> list[CheckResult]:
    results: list[CheckResult] = []
    for command in commands:
        result = workspace.execute_command(
            f"cd /workspace && {shlex.join(command.argv)}", timeout=command_timeout
        )
        output = (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")
        check = CheckResult(command, int(getattr(result, "exit_code", 1)), output[-12000:])
        results.append(check)
        if not check.passed:
            break
    return results


def new_workspace(root: Path, requirement: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", requirement.lower()).strip("-")[:40] or "application"
    path = root / f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{slug}"
    path.mkdir(parents=True, exist_ok=False)
    path.chmod(0o777)  # sandbox user differs from the host user; normalized afterward
    return path


def safe_workspace(path: Path, *, allow_plan_only: bool = False, allow_existing: bool = False) -> Path:
    path = path.resolve()
    if not path.exists():
        path.mkdir(parents=True)
        path.chmod(0o777)
        return path
    entries = {item.name for item in path.iterdir()}
    allowed = {"TASK.md", ".autodev", ".agents_tmp", ".git", "conversations"} if allow_plan_only else set()
    if entries - allowed and not allow_existing:
        raise RunFailure(f"Refusing to alter existing non-empty workspace: {path}")
    path.chmod(0o777)
    return path
