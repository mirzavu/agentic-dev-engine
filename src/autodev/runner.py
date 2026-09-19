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
GIT_RUNTIME_EXCLUDES = (
    ".agents_tmp/",
    "conversations/",
    "bash_events/",
    ".autodev/visual-review.json",
    ".autodev/visual-review-history.json",
    ".autodev/visual/",
    "node_modules/",
    "dist/",
    "playwright-report/",
    "test-results/",
)
CONTAINER_OPENHANDS_STATE_DIR = "/home/openhands/.openhands"
CONTAINER_OPENHANDS_UID = 10001
CONTAINER_OPENHANDS_GID = 10001


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


class AgentDriver(Protocol):
    def plan(self, workspace, requirement: str, timeout: float) -> None: ...
    def develop(self, workspace, prompt: str, timeout: float) -> None: ...
    def review_visual(self, workspace, prompt: str, timeout: float) -> None: ...


class OpenHandsDriver:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def model(self) -> str:
        return self.settings.codex_model or "gpt-5.5"

    def _codex_auth_json(self) -> str:
        try:
            content = self.settings.codex_auth_path.read_text()
            if not isinstance(json.loads(content), dict):
                raise ValueError("credential root is not an object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RunFailure(
                "Codex subscription credentials are unavailable; run `codex login` on the host"
            ) from exc
        return content

    def secrets_to_redact(self) -> list[str]:
        if self.settings.agent_kind == "agent":
            return []
        try:
            content = self._codex_auth_json()
            values = json.loads(content)
        except RunFailure:
            return []
        return [content, *[value for value in values.values() if isinstance(value, str)]]

    def _agent(self, prompt_timeout: float, *, planning: bool = False):
        if self.settings.agent_kind == "agent":
            from openhands.sdk import LLM
            from openhands.tools.preset.default import get_default_agent
            from openhands.tools.preset.planning import get_planning_agent

            llm = LLM.subscription_login(
                vendor="openai",
                model=self.model,
                open_browser=False,
                timeout=int(max(1, prompt_timeout)),
            )
            return get_planning_agent(llm) if planning else get_default_agent(llm=llm)

        from openhands.sdk.settings.model import ACPAgentSettings
        return ACPAgentSettings(
            acp_server="codex",
            acp_model=self.model,
            acp_prompt_timeout=max(1, prompt_timeout),
            acp_startup_timeout=self.settings.model_timeout_seconds,
            acp_isolate_data_dir=True,
        ).create_agent()


def new_workspace(root: Path, requirement: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", requirement.lower()).strip("-")[:40] or "application"
    path = root / f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{slug}"
    path.mkdir(parents=True, exist_ok=False)
    path.chmod(0o777)  # sandbox user differs from the host user; normalized afterward
    return path


def initialize_git_repository(app: Path) -> None:
    """Create an application-local Git history without using host-global config."""
    inside = subprocess.run(
        ["git", "-C", str(app), "rev-parse", "--is-inside-work-tree"],
        text=True, capture_output=True, check=False,
    )
    if inside.returncode:
        result = subprocess.run(["git", "-C", str(app), "init"], text=True, capture_output=True, check=False)
        if result.returncode:
            raise RunFailure(f"Could not initialize application Git repository: {result.stderr.strip()}")
    for key, value in (("user.name", "Autodev"), ("user.email", "autodev@local")):
        result = subprocess.run(
            ["git", "-C", str(app), "config", key, value], text=True, capture_output=True, check=False,
        )
        if result.returncode:
            raise RunFailure(f"Could not configure application Git repository: {result.stderr.strip()}")
    exclude_file = app / ".git" / "info" / "exclude"
    existing = exclude_file.read_text() if exclude_file.is_file() else ""
    additions = [pattern for pattern in GIT_RUNTIME_EXCLUDES if pattern not in existing.splitlines()]
    if additions:
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        exclude_file.write_text(existing.rstrip() + "\n" + "\n".join(additions) + "\n")


def git_checkpoint(app: Path, message: str) -> str | None:
    """Commit source changes after an autonomous stage, excluding runtime state."""
    initialize_git_repository(app)
    stage = subprocess.run(
        ["git", "-C", str(app), "add", "-A", "--", "."],
        text=True, capture_output=True, check=False,
    )
    if stage.returncode:
        raise RunFailure(f"Could not stage application checkpoint: {stage.stderr.strip()}")
    changed = subprocess.run(
        ["git", "-C", str(app), "diff", "--cached", "--quiet"],
        text=True, capture_output=True, check=False,
    )
    if changed.returncode == 0:
        return None
    if changed.returncode != 1:
        raise RunFailure(f"Could not inspect application checkpoint: {changed.stderr.strip()}")
    commit = subprocess.run(
        ["git", "-C", str(app), "commit", "-m", message], text=True, capture_output=True, check=False,
    )
    if commit.returncode:
        raise RunFailure(f"Could not commit application checkpoint: {commit.stderr.strip()}")
    return subprocess.run(
        ["git", "-C", str(app), "rev-parse", "--short", "HEAD"],
        text=True, capture_output=True, check=True,
    ).stdout.strip()


def git_status_snapshot(app: Path) -> str | None:
    if not (app / ".git").is_dir():
        return None
    result = subprocess.run(
        ["git", "-C", str(app), "status", "--porcelain", "--untracked-files=normal"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        return None
    return result.stdout


def agent_turn_timeout(driver: AgentDriver, deadline: float) -> float:
    remaining = max(1, deadline - time.monotonic())
    configured = getattr(getattr(driver, "settings", None), "model_timeout_seconds", None)
    if isinstance(configured, int | float) and configured > 0:
        return min(remaining, float(configured))
    return remaining


def _chown_with_docker(path: Path, uid: int, gid: int) -> None:
    mount = f"{path.resolve()}:/target:rw"
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--user", "0:0", "-v", mount,
         "alpine:3.20", "chown", "-R", f"{uid}:{gid}", "/target"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RunFailure("Could not prepare temporary OpenHands subscription credentials")


@contextmanager
def openhands_auth_volumes(driver: AgentDriver) -> Iterator[list[str]]:
    settings = getattr(driver, "settings", None)
    if getattr(settings, "agent_kind", None) != "agent":
        yield []
        return
    source = Path.home() / ".openhands" / "auth" / "openai_oauth.json"
    try:
        content = source.read_text()
        if not isinstance(json.loads(content), dict):
            raise ValueError("credential root is not an object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RunFailure("OpenHands subscription credentials are unavailable; run `uv run autodev login`") from exc
    temp = tempfile.TemporaryDirectory(prefix="autodev-openhands-auth-")
    temp_path = Path(temp.name)
    try:
        auth_dir = temp_path / "auth"
        auth_dir.mkdir()
        temp_path.chmod(0o700)
        auth_dir.chmod(0o700)
        copied = auth_dir / "openai_oauth.json"
        copied.write_text(content)
        copied.chmod(0o600)
        _chown_with_docker(temp_path, CONTAINER_OPENHANDS_UID, CONTAINER_OPENHANDS_GID)
        yield [f"{temp_path.resolve()}:{CONTAINER_OPENHANDS_STATE_DIR}:rw"]
    finally:
        try:
            _chown_with_docker(temp_path, os.getuid(), os.getgid())
        finally:
            temp.cleanup()


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


def dependency_file_needs_execute(path: Path) -> bool:
    try:
        header = path.read_bytes()[:4]
    except OSError:
        return False
    return header.startswith(b"#!") or header.startswith(b"\x7fELF") or header.startswith(b"MZ")


def grant_sandbox_access(app: Path) -> None:
    """Temporarily permit the non-root agent-server user to access this mount."""
    for path in [app, *app.rglob("*")]:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            continue
        if stat.S_ISDIR(mode):
            path.chmod(0o777)
        elif stat.S_ISREG(mode):
            path.chmod(stat.S_IMODE(mode) | 0o666)
    # Package-manager command shims resolve into dependency `bin` files. The
    # sandbox user is not the host owner, so those specific scripts must remain
    # executable even though ordinary application files remain non-executable.
    dependencies = app / "node_modules"
    if dependencies.is_dir():
        for path in dependencies.rglob("bin"):
            if not path.is_dir() or path.is_symlink():
                continue
            for script in path.iterdir():
                if script.is_symlink() or not script.is_file():
                    continue
                script.chmod(stat.S_IMODE(script.stat().st_mode) | 0o111)
        for executable in dependencies.rglob("*"):
            if executable.is_symlink() or not executable.is_file():
                continue
            if dependency_file_needs_execute(executable):
                executable.chmod(stat.S_IMODE(executable.stat().st_mode) | 0o111)
        package_bins = dependencies / ".bin"
        if package_bins.is_dir():
            for shim in package_bins.iterdir():
                if not shim.is_symlink():
                    continue
                try:
                    target = shim.resolve(strict=True)
                except OSError:
                    continue
                if target.is_file() and dependencies.resolve() in target.parents:
                    target.chmod(stat.S_IMODE(target.stat().st_mode) | 0o111)


def secrets_to_redact(driver: AgentDriver) -> list[str]:
    getter = getattr(driver, "secrets_to_redact", None)
    return getter() if callable(getter) else []
