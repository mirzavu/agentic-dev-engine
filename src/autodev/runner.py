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

SAMPLE_REQUIREMENT = (
    "Build a self contained task management web application with a browser UI. "
    "Users must be able to create, edit, complete, and delete tasks. Include "
    "automated tests and a documented command for running them. Avoid external services. "
    "Treat the browser experience as a finished consumer product: establish a deliberate "
    "visual direction, make the task flow feel natural, and design responsive layouts rather "
    "than applying a generic card-and-buttons template."
)


class RunFailure(RuntimeError):
    pass


VISUAL_DIMENSIONS = (
    "visual_hierarchy",
    "composition_density",
    "design_coherence",
    "task_flow_ux",
    "responsive_design",
    "product_character",
)
MIN_VISUAL_DIMENSION_SCORE = 7.0
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
VISUAL_ARTIFACTS_DIR = ".autodev/visual"
CONTAINER_OPENHANDS_STATE_DIR = "/home/openhands/.openhands"
CONTAINER_OPENHANDS_UID = 10001
CONTAINER_OPENHANDS_GID = 10001
OPENHANDS_DEVELOPMENT_TOOL_GUIDANCE = (
    "Tool-use constraint: when using the terminal tool, execute one command per action. "
    "Do not create multiple files by pasting a large multi-heredoc shell script or a batch of "
    "`cat > file` commands. Use the file editor for file contents, or create/edit files one at "
    "a time, then run install, test, and build commands as separate terminal actions."
)


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


@dataclass(frozen=True)
class VisualIssue:
    severity: str
    viewport: str
    area: str
    evidence: str
    recommendation: str


@dataclass(frozen=True)
class VisualReview:
    score: float
    issues: tuple[VisualIssue, ...]
    dimensions: tuple[tuple[str, float], ...] = field(
        default_factory=lambda: tuple((name, 10.0) for name in VISUAL_DIMENSIONS)
    )
    production_ready: bool = True

    @property
    def approved(self) -> bool:
        return (
            self.production_ready
            and not any(issue.severity in {"high", "medium"} for issue in self.issues)
            and all(score >= MIN_VISUAL_DIMENSION_SCORE for _, score in self.dimensions)
        )

    @property
    def dimension_summary(self) -> str:
        return ", ".join(f"{name.replace('_', ' ')}: {score:g}/10" for name, score in self.dimensions)


@dataclass(frozen=True)
class VisualArtifact:
    name: str
    path: Path
    viewport: str


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


def chrome_executable() -> str:
    for candidate in ("google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(candidate)
        if path:
            return path
    raise RunFailure("Chrome or Chromium is required for mechanical visual screenshot capture")


def _wait_for_http(url: str, deadline: float) -> None:
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except (OSError, URLError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise RunFailure(f"Timed out waiting for visual preview server: {last_error}")


def capture_visual_artifacts(app: Path, *, timeout: float = 60.0) -> tuple[VisualArtifact, ...]:
    dist = app / "dist"
    if not dist.is_dir():
        raise RunFailure("Visual QA requires a built dist directory from mechanical verification")
    artifacts_dir = app / VISUAL_ARTIFACTS_DIR
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    browser = chrome_executable()
    port = available_loopback_port()
    deadline = time.monotonic() + timeout
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(dist)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if server.poll() is not None:
            _, stderr = server.communicate(timeout=1)
            raise RunFailure(f"Visual preview server exited early: {stderr.strip()}")
        base_url = f"http://127.0.0.1:{port}/"
        _wait_for_http(base_url, deadline)
        captures = (
            ("desktop", "1440,1000", artifacts_dir / "desktop.png"),
            ("mobile", "390,844", artifacts_dir / "mobile.png"),
        )
        artifacts: list[VisualArtifact] = []
        for name, viewport, output in captures:
            result = subprocess.run(
                [
                    browser,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    f"--window-size={viewport}",
                    f"--screenshot={output}",
                    base_url,
                ],
                text=True,
                capture_output=True,
                timeout=max(1, deadline - time.monotonic()),
                check=False,
            )
            if result.returncode or not output.is_file() or output.stat().st_size == 0:
                message = (result.stderr or result.stdout or "no output").strip()
                raise RunFailure(f"Failed to capture {name} screenshot: {message}")
            artifacts.append(VisualArtifact(name, output, viewport))
        layout_metrics = capture_layout_metrics(app, base_url, browser, artifacts_dir, deadline)
        manifest = {
            "url": base_url,
            "layout_metrics": f"/workspace/{layout_metrics.relative_to(app)}",
            "artifacts": [
                {"name": item.name, "path": f"/workspace/{item.path.relative_to(app)}", "viewport": item.viewport}
                for item in artifacts
            ],
        }
        (artifacts_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        return tuple(artifacts)
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


def capture_layout_metrics(app: Path, base_url: str, browser: str, artifacts_dir: Path, deadline: float) -> Path:
    output = artifacts_dir / "layout.json"
    if not (app / "node_modules" / "playwright").is_dir() or not shutil.which("node"):
        output.write_text(json.dumps({"available": False, "reason": "playwright package not available"}, indent=2))
        return output
    script = r"""
const { chromium } = require('playwright');
const browserPath = process.argv[2];
const url = process.argv[3];
const selectors = ['html','body','main','.intro','.capture','.capture-row','.capture input','.capture-row > button','.list-heading','.workspace'];
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: browserPath, args: ['--no-sandbox']});
  const results = {};
  for (const [name, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
    const page = await browser.newPage({viewport: {width, height}});
    await page.goto(url, {waitUntil: 'networkidle'});
    results[name] = await page.evaluate((selectors) => {
      const rects = {};
      for (const selector of selectors) {
        const element = selector === 'html' ? document.documentElement : selector === 'body' ? document.body : document.querySelector(selector);
        if (!element) { rects[selector] = null; continue; }
        const rect = element.getBoundingClientRect();
        rects[selector] = {left: rect.left, right: rect.right, width: rect.width};
      }
      return {
        innerWidth,
        documentClientWidth: document.documentElement.clientWidth,
        documentScrollWidth: document.documentElement.scrollWidth,
        bodyScrollWidth: document.body.scrollWidth,
        horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
        rects
      };
    }, selectors);
    await page.close();
  }
  await browser.close();
  console.log(JSON.stringify({available: true, viewports: results}));
})().catch(async (error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""
    result = subprocess.run(
        ["node", "-", browser, base_url],
        input=script,
        text=True,
        capture_output=True,
        cwd=app,
        timeout=max(1, deadline - time.monotonic()),
        check=False,
    )
    if result.returncode:
        output.write_text(json.dumps({"available": False, "reason": (result.stderr or result.stdout).strip()[:1000]}, indent=2))
        return output
    try:
        metrics = json.loads(result.stdout)
    except json.JSONDecodeError:
        metrics = {"available": False, "reason": "layout metrics command did not return JSON"}
    output.write_text(json.dumps(metrics, indent=2))
    return output


def visual_review_schema() -> dict:
    issue_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["severity", "viewport", "area", "evidence", "recommendation"],
        "properties": {
            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
            "viewport": {"type": "string"},
            "area": {"type": "string"},
            "evidence": {"type": "string"},
            "recommendation": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["score", "dimensions", "production_ready", "issues"],
        "properties": {
            "score": {"type": "number", "minimum": 0, "maximum": 10},
            "dimensions": {
                "type": "object",
                "additionalProperties": False,
                "required": list(VISUAL_DIMENSIONS),
                "properties": {
                    name: {"type": "number", "minimum": 0, "maximum": 10}
                    for name in VISUAL_DIMENSIONS
                },
            },
            "production_ready": {"type": "boolean"},
            "issues": {"type": "array", "items": issue_schema},
        },
    }


def load_visual_review(app: Path) -> VisualReview:
    path = app / ".autodev" / "visual-review.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RunFailure("Missing or invalid .autodev/visual-review.json") from exc
    score = data.get("score") if isinstance(data, dict) else None
    issues = data.get("issues") if isinstance(data, dict) else None
    dimensions = data.get("dimensions") if isinstance(data, dict) else None
    production_ready = data.get("production_ready") if isinstance(data, dict) else None
    if (isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 10
            or not isinstance(issues, list) or not isinstance(dimensions, dict)
            or not isinstance(production_ready, bool)):
        raise RunFailure("Visual review must contain score, dimensions, production_ready, and issues")
    if set(dimensions) != set(VISUAL_DIMENSIONS) or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10
        for value in dimensions.values()
    ):
        raise RunFailure("Visual review dimensions must score every required dimension from 0-10")
    parsed: list[VisualIssue] = []
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("severity") not in {"high", "medium", "low"}:
            raise RunFailure("Each visual issue must declare high, medium, or low severity")
        fields = [issue.get(key) for key in ("viewport", "area", "evidence", "recommendation")]
        if not all(isinstance(value, str) and value.strip() for value in fields):
            raise RunFailure("Each visual issue must include viewport, area, evidence, and recommendation")
        parsed.append(VisualIssue(issue["severity"], *fields))
    return VisualReview(
        float(score),
        tuple(parsed),
        tuple((name, float(dimensions[name])) for name in VISUAL_DIMENSIONS),
        production_ready,
    )


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

    def _run(self, agent, workspace, message: str, timeout: float) -> None:
        from openhands.sdk import Conversation
        from openhands.sdk.conversation.exceptions import ConversationRunError
        from openhands.sdk.event.conversation_error import ConversationErrorEvent

        errors: list[ConversationErrorEvent] = []

        def on_event(event) -> None:
            if isinstance(event, ConversationErrorEvent):
                errors.append(event)

        conversation = Conversation(
            agent=agent,
            workspace=workspace,
            persistence_dir=None,
            callbacks=[on_event],
            max_iteration_per_run=self.settings.max_iterations,
            secrets={} if self.settings.agent_kind == "agent" else {"CODEX_AUTH_JSON": self._codex_auth_json()},
        )
        try:
            conversation.send_message(message)
            conversation.run(timeout=max(1, timeout))
        except ConversationRunError as exc:
            error = exc.conversation_error or (errors[-1] if errors else None)
            if error:
                raise RunFailure(f"OpenHands {self.settings.agent_kind} failed: {error.code}: {error.detail}") from exc
            raise RunFailure(f"OpenHands {self.settings.agent_kind} failed: {exc}") from exc
        finally:
            conversation.close()

    def plan(self, workspace, requirement: str, timeout: float) -> None:
        self._run(
            self._agent(timeout, planning=True), workspace,
            "Create the implementation plan for this requirement. Write it to /workspace/.agents_tmp/PLAN.md "
            "and ensure it explicitly includes application goal, chosen stack, required features, "
            "implementation steps, acceptance criteria, and testing approach. Do not implement code. "
            "IMPORTANT: The UI/UX design is a core requirement, not an afterthought. Treat the user interface like a custom home renovation—do not settle for generic default templates. Ensure the implementation steps in the plan explicitly define a comprehensive design system (theme, fonts, custom CSS, layout, spacing, animations, responsive design, and product character/identity) and detail how each UI component will be crafted with high aesthetic standards.\n\n"
            f"Requirement:\n{requirement}", timeout,
        )

    def develop(self, workspace, prompt: str, timeout: float) -> None:
        self._run(self._agent(timeout), workspace, prompt, timeout)

    def review_visual(self, workspace, prompt: str, timeout: float) -> None:
        self._run(self._agent(timeout), workspace, prompt, timeout)


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


def promote_plan(app: Path) -> str:
    candidate = app / ".agents_tmp" / "PLAN.md"
    if not candidate.is_file() or not candidate.read_text().strip():
        raise RunFailure("OpenHands planning did not produce a plan")
    content = candidate.read_text()
    replacement = app / ".TASK.md.tmp"
    replacement.write_text(content)
    os.replace(replacement, app / "TASK.md")
    return content


def planning_session(app: Path, requirement: str, driver: AgentDriver, deadline: float) -> str:
    grant_sandbox_access(app)
    with openhands_auth_volumes(driver) as auth_volumes:
        workspace = LoopbackDockerWorkspace.create(app, extra_volumes=auth_volumes)
        try:
            with workspace:
                driver.plan(workspace, requirement, agent_turn_timeout(driver, deadline))
        finally:
            normalize_permissions(app)
    return promote_plan(app)


def development_session(app: Path, prompt: str, driver: AgentDriver, deadline: float) -> list[CheckResult]:
    grant_sandbox_access(app)
    with openhands_auth_volumes(driver) as auth_volumes:
        workspace = LoopbackDockerWorkspace.create(app, extra_volumes=auth_volumes)
        try:
            with workspace:
                workspace.execute_command('git config --global --add safe.directory /workspace')
                driver.develop(workspace, prompt, agent_turn_timeout(driver, deadline))
        finally:
            normalize_permissions(app)
    commands = load_verification_manifest(app)
    grant_sandbox_access(app)
    verifier = LoopbackDockerWorkspace.create(app)
    try:
        with verifier:
            timeout = getattr(getattr(driver, 'settings', None), 'command_timeout_seconds', 600)
            return verify_in_workspace(verifier, app, commands, command_timeout=timeout)
    finally:
        normalize_permissions(app)


def visual_review_session(app: Path, driver: AgentDriver, deadline: float) -> VisualReview:
    review_path = app / ".autodev" / "visual-review.json"
    review_path.unlink(missing_ok=True)
    remaining = max(1, deadline - time.monotonic())
    limit = getattr(getattr(driver, "settings", None), "visual_review_seconds", 180)
    timeout = min(remaining, limit)
    artifacts = capture_visual_artifacts(app, timeout=min(timeout, 60))
    grant_sandbox_access(app)
    with openhands_auth_volumes(driver) as auth_volumes:
        workspace = LoopbackDockerWorkspace.create(app, extra_volumes=auth_volumes)
        try:
            with workspace:
                driver.review_visual(workspace, visual_review_prompt(artifacts, app), timeout)
        finally:
            normalize_permissions(app)
    return load_visual_review(app)


def implementation_prompt() -> str:
    return f"""Implement the application described in TASK.md in /workspace. 
IMPORTANT: Pay extraordinary attention to the UI/UX design. Treat the interface design as a premium custom product—avoid generic cards-and-buttons templates. Craft custom styling, deliberate color palettes, spacing, smooth micro-interactions, responsive structures, and strong product character. 
{OPENHANDS_DEVELOPMENT_TOOL_GUIDANCE}
You may install dependencies and run checks. Create automated tests and README.md documenting their exact command. Create .autodev/verification.json with token arrays named install and test, plus build when the chosen stack has a build step. Do not claim success until those commands have been run."""


def repair_prompt(failure: CheckResult) -> str:
    return """Mechanical verification failed. Diagnose and fix the application in /workspace, then rerun relevant checks. Do not change TASK.md to weaken requirements.
{tool_guidance}
The real failure was:

Stage: {stage}
Command: {command}
Exit code: {code}
Output:
{output}
""".format(
        tool_guidance=OPENHANDS_DEVELOPMENT_TOOL_GUIDANCE,
        stage=failure.command.name,
        command=shlex.join(failure.command.argv),
        code=failure.exit_code,
        output=failure.output,
    )


def visual_review_prompt(artifacts: tuple[VisualArtifact, ...], app: Path) -> str:
    sections = []
    sections.append("Act as a senior product-design reviewer, not an implementer.")
    sections.append("Do not modify application source code or inspect unrelated files.")
    sections.append("Review these pre-captured screenshots:")
    sections.append(_visual_artifact_rows(artifacts, app))
    sections.append(_visual_review_instructions())
    sections.append("Write .autodev/visual-review.json using this schema:")
    sections.append(json.dumps(visual_review_schema(), indent=2))
    return "\n\n".join(sections)


def visual_fix_prompt(review: VisualReview) -> str:
    lines = [
        "A senior product-design reviewer inspected the application.",
        "Address the underlying design feedback while preserving working behavior.",
        "Do not weaken required features or tests; preserve the verification manifest.",
        "Rerun relevant checks after changing the implementation.",
        OPENHANDS_DEVELOPMENT_TOOL_GUIDANCE,
        f"Overall score: {review.score}/10",
        f"Dimension scores: {review.dimension_summary}",
        f"Production-ready: {review.production_ready}",
        "Findings:",
    ]
    for issue in review.issues:
        location = f"{issue.viewport}, {issue.area}"
        finding = f"- [{issue.severity}] {location}: {issue.evidence}"
        correction = f"Intent: {issue.recommendation}"
        lines.append(f"{finding}. {correction}")
    if not review.issues:
        lines.append("- Improve visual quality while preserving working behavior.")
    return "\n".join(lines)


def secrets_to_redact(driver: AgentDriver) -> list[str]:
    getter = getattr(driver, "secrets_to_redact", None)
    return getter() if callable(getter) else []


def _visual_artifact_rows(artifacts: tuple[VisualArtifact, ...], app: Path) -> str:
    rows = []
    for artifact in artifacts:
        relative = artifact.path.relative_to(app)
        path = f"/workspace/{relative}"
        description = f"- {artifact.name}: {path} ({artifact.viewport})"
        rows.append(description)
    return "\n".join(rows)


def _visual_review_instructions() -> str:
    instructions = [
        "Do not start a browser or server; evidence has already been captured.",
        "Use screenshot pixels as the primary evidence, rather than source code.",
        "When pixels cannot be inspected, use score 0 and production_ready false.",
        "Assess hierarchy, composition, coherence, task flow, responsiveness, and character.",
        "Use the captured desktop and mobile viewports to assess the application.",
        "Record concrete findings with severity, viewport, area, evidence, and recommendation.",
        "An intentional product design is required; aligned generic controls are insufficient.",
        "Inspect .autodev/visual/layout.json for mechanical clipping evidence when available.",
        "Do not report horizontal overflow when layout metrics show the viewport contains it.",
        "Set production_ready false for any dimension below seven or medium/high issue.",
        "Finish after writing the structured visual review response.",
    ]
    return "\n".join(instructions)
