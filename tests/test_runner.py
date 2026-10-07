import json
import subprocess
from pathlib import Path

import pytest

from autodev.config import Settings
from autodev.runner import (CheckResult, RunFailure, VerificationCommand, VisualArtifact, VisualIssue,
                            VisualReview, append_visual_review_history, capture_visual_artifacts,
                            codex_cli_task, codex_cli_visual_review, development_session, git_checkpoint,
                            implementation_prompt, load_verification_manifest, load_visual_review, redact,
                            repair_prompt, report_text, run_live, safe_workspace, visual_fix_prompt,
                            visual_review_prompt, visual_review_session)
from autodev.workspace import workspace_mount


def command(name: str, code: int) -> CheckResult:
    return CheckResult(VerificationCommand(name, ("tool", name)), code, "failure sk-abcdefghijklmnop")


def settings(*, fallback: bool = False) -> Settings:
    return Settings("agent", None, Path("/missing/auth.json"), 2, 3, 60, 10, 10, 30, 3, 8, fallback)


def test_manifest_requires_documented_test_command(tmp_path):
    (tmp_path / ".autodev").mkdir()
    (tmp_path / ".autodev" / "verification.json").write_text(json.dumps({
        "install": ["npm", "install"], "test": ["npm", "test"], "build": ["npm", "run", "build"],
    }))
    (tmp_path / "README.md").write_text("Run `npm test`.")
    commands = load_verification_manifest(tmp_path)
    assert [item.name for item in commands] == ["install", "test", "build"]


def test_manifest_rejects_missing_install(tmp_path):
    (tmp_path / ".autodev").mkdir()
    (tmp_path / ".autodev" / "verification.json").write_text('{"test": ["pytest"]}')
    (tmp_path / "README.md").write_text("pytest")
    with pytest.raises(RunFailure):
        load_verification_manifest(tmp_path)


def test_visual_review_accepts_fractional_score(tmp_path):
    (tmp_path / ".autodev").mkdir()
    (tmp_path / ".autodev" / "visual-review.json").write_text(json.dumps({
        "score": 7.5,
        "dimensions": {name: 7.5 for name in ("visual_hierarchy", "composition_density", "design_coherence", "task_flow_ux", "responsive_design", "product_character")},
        "production_ready": False,
        "issues": [],
    }))
    assert load_visual_review(tmp_path).score == 7.5


def test_visual_artifact_capture_is_host_side_and_bounded(monkeypatch, tmp_path):
    (tmp_path / "dist").mkdir()
    server_state = {"terminated": False}

    class FakeServer:
        stdout = None
        stderr = None

        def poll(self):
            return None

        def terminate(self):
            server_state["terminated"] = True

        def wait(self, timeout):
            return 0

    def fake_run(argv, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        screenshot = next((item.removeprefix("--screenshot=") for item in argv if item.startswith("--screenshot=")), None)
        assert screenshot is not None
        Path(screenshot).write_bytes(b"png")
        assert "--headless=new" in argv
        assert "--no-sandbox" in argv
        return Result()

    monkeypatch.setattr("autodev.runner.chrome_executable", lambda: "/usr/bin/google-chrome")
    monkeypatch.setattr("autodev.runner.available_loopback_port", lambda: 45678)
    monkeypatch.setattr("autodev.runner._wait_for_http", lambda url, deadline: None)
    monkeypatch.setattr("autodev.runner.subprocess.Popen", lambda *args, **kwargs: FakeServer())
    monkeypatch.setattr("autodev.runner.subprocess.run", fake_run)

    artifacts = capture_visual_artifacts(tmp_path)

    assert [artifact.name for artifact in artifacts] == ["desktop", "mobile"]
    assert (tmp_path / ".autodev" / "visual" / "desktop.png").is_file()
    assert (tmp_path / ".autodev" / "visual" / "mobile.png").is_file()
    manifest = json.loads((tmp_path / ".autodev" / "visual" / "manifest.json").read_text())
    assert manifest["url"] == "http://127.0.0.1:45678/"
    assert server_state["terminated"]


def test_visual_review_prompt_uses_captured_artifacts(tmp_path):
    (tmp_path / "TASK.md").write_text("Build a desktop-only app. Mobile responsiveness is not required.")
    artifact = VisualArtifact("desktop", tmp_path / ".autodev" / "visual" / "desktop.png", "1440,1000")
    append_visual_review_history(
        tmp_path,
        VisualReview(7, (VisualIssue("medium", "mobile", "task row", "Crowded controls", "Reduce action weight"),)),
    )

    prompt = visual_review_prompt((artifact,), tmp_path)

    assert "/workspace/.autodev/visual/desktop.png" in prompt
    assert "Do not start a browser or server" in prompt
    assert "Previous visual QA rounds from this run" in prompt
    assert "Crowded controls" in prompt
    assert "Reduce action weight" in prompt
    assert "Do not penalize the app for mobile layout quality" in prompt
    assert "score 0" in prompt


def test_codex_cli_visual_review_writes_validated_json(monkeypatch, tmp_path):
    visual_dir = tmp_path / ".autodev" / "visual"
    visual_dir.mkdir(parents=True)
    desktop = visual_dir / "desktop.png"
    desktop.write_bytes(b"png")
    artifact = VisualArtifact("desktop", desktop, "1440,1000")
    append_visual_review_history(
        tmp_path,
        VisualReview(7, (VisualIssue("medium", "desktop", "hero", "Generic layout", "Add product character"),)),
    )
    captured = {}

    def fake_run(argv, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        captured["argv"] = argv
        captured["input"] = kwargs["input"]
        output = Path(argv[argv.index("-o") + 1])
        output.write_text(json.dumps({
            "score": 8,
            "dimensions": {name: 8 for name in ("visual_hierarchy", "composition_density", "design_coherence", "task_flow_ux", "responsive_design", "product_character")},
            "production_ready": True,
            "issues": [],
        }))
        return Result()

    monkeypatch.setattr("autodev.runner.shutil.which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr("autodev.runner.subprocess.run", fake_run)

    review = codex_cli_visual_review(tmp_path, (artifact,), timeout=10)

    assert review.approved
    assert "--image" in captured["argv"]
    assert captured["argv"][-1] == "-"
    assert "senior product-design reviewer" in captured["input"]
    assert "Generic layout" in captured["input"]
    assert "Add product character" in captured["input"]
    assert str(desktop) in captured["argv"]
    assert (tmp_path / ".autodev" / "visual-review.json").is_file()


def test_codex_cli_task_uses_workspace_sandbox(monkeypatch, tmp_path):
    captured = {"commands": []}

    def fake_run(argv, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        captured["commands"].append(argv)
        if argv[:2] == ["/usr/bin/codex", "exec"]:
            captured["input"] = kwargs["input"]
            output = Path(argv[argv.index("-o") + 1])
            output.write_text(json.dumps({"files": [{"path": "README.md", "content": "changed\n"}]}))
        return Result()

    monkeypatch.setattr("autodev.runner.shutil.which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr("autodev.runner.subprocess.run", fake_run)

    codex_cli_task(tmp_path, "Make a small change.", timeout=10)

    codex_command = next(command for command in captured["commands"] if command[:2] == ["/usr/bin/codex", "exec"])
    assert codex_command[:2] == ["/usr/bin/codex", "exec"]
    assert "--sandbox" in codex_command
    assert codex_command[codex_command.index("--sandbox") + 1] == "read-only"
    assert codex_command[codex_command.index("-C") + 1] == str(tmp_path)
    assert codex_command[-1] == "-"
    assert "--output-schema" in codex_command
    assert "Return JSON only" in captured["input"]
    assert (tmp_path / "README.md").read_text() == "changed\n"


def test_codex_cli_task_failure_is_reported(monkeypatch, tmp_path):
    def fake_run(argv, **kwargs):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "boom"

        return Result()

    monkeypatch.setattr("autodev.runner.shutil.which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr("autodev.runner.subprocess.run", fake_run)

    with pytest.raises(RunFailure, match="Codex CLI fallback failed"):
        codex_cli_task(tmp_path, "Change code.", timeout=10)


def test_development_prompts_forbid_multi_heredoc_file_batches():
    guidance = "Do not create multiple files by pasting a large multi-heredoc shell script"

    assert guidance in implementation_prompt()
    assert guidance in repair_prompt(command("test", 1))
    assert guidance in visual_fix_prompt(
        VisualReview(7, (VisualIssue("medium", "desktop", "controls", "Crowded", "Improve spacing"),))
    )


def test_development_session_falls_back_after_no_op_agent(monkeypatch, tmp_path):
    subprocess.run(["git", "-C", str(tmp_path), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    (tmp_path / "README.md").write_text("start\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "start"], check=True, capture_output=True)

    class FakeWorkspace:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute_command(self, *args, **kwargs):
            class Result:
                exit_code = 0
                stdout = ""
                stderr = ""

            return Result()

    class NoOpDriver:
        settings = settings(fallback=True)

        def develop(self, workspace, prompt, timeout):
            return None

    def fake_fallback(app, prompt, *, timeout):
        (app / "README.md").write_text("changed\n")

    monkeypatch.setattr("autodev.runner.LoopbackDockerWorkspace.create", lambda app, **kwargs: FakeWorkspace())
    monkeypatch.setattr("autodev.runner.normalize_permissions", lambda app: None)
    monkeypatch.setattr("autodev.runner.codex_cli_task", fake_fallback)
    monkeypatch.setattr("autodev.runner.load_verification_manifest", lambda app: [VerificationCommand("test", ("true",))])
    monkeypatch.setattr("autodev.runner.verify_in_workspace", lambda *args, **kwargs: [command("test", 0)])

    results = development_session(tmp_path, "change files", NoOpDriver(), 9999999999)

    assert results[-1].passed
    assert (tmp_path / "README.md").read_text() == "changed\n"


def test_visual_review_session_falls_back_when_openhands_crashes(monkeypatch, tmp_path):
    artifact_dir = tmp_path / ".autodev" / "visual"
    artifact_dir.mkdir(parents=True)
    artifact = VisualArtifact("desktop", artifact_dir / "desktop.png", "1440,1000")
    artifact.path.write_bytes(b"png")

    class FakeWorkspace:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeDriver:
        settings = settings(fallback=True)

        def review_visual(self, workspace, prompt, timeout):
            raise RuntimeError("ACP down")

    monkeypatch.setattr("autodev.runner.capture_visual_artifacts", lambda *args, **kwargs: (artifact,))
    monkeypatch.setattr("autodev.runner.LoopbackDockerWorkspace.create", lambda app, **kwargs: FakeWorkspace())
    monkeypatch.setattr("autodev.runner.normalize_permissions", lambda app: None)
    monkeypatch.setattr("autodev.runner.codex_cli_visual_review", lambda *args, **kwargs: VisualReview(9, ()))

    review = visual_review_session(tmp_path, FakeDriver(), 9999999999)

    assert review.score == 9


def test_visual_review_session_does_not_reuse_stale_review(monkeypatch, tmp_path):
    autodev = tmp_path / ".autodev"
    autodev.mkdir()
    (autodev / "visual-review.json").write_text(json.dumps({
        "score": 10,
        "dimensions": {name: 10 for name in ("visual_hierarchy", "composition_density", "design_coherence", "task_flow_ux", "responsive_design", "product_character")},
        "production_ready": True,
        "issues": [],
    }))
    artifact = VisualArtifact("desktop", autodev / "visual" / "desktop.png", "1440,1000")
    artifact.path.parent.mkdir()
    artifact.path.write_bytes(b"png")

    class FakeWorkspace:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class CrashingDriver:
        settings = settings(fallback=True)

        def review_visual(self, workspace, prompt, timeout):
            raise RuntimeError("ACP down")

    monkeypatch.setattr("autodev.runner.capture_visual_artifacts", lambda *args, **kwargs: (artifact,))
    monkeypatch.setattr("autodev.runner.LoopbackDockerWorkspace.create", lambda app, **kwargs: FakeWorkspace())
    monkeypatch.setattr("autodev.runner.normalize_permissions", lambda app: None)
    monkeypatch.setattr("autodev.runner.codex_cli_visual_review", lambda *args, **kwargs: VisualReview(4, ()))

    review = visual_review_session(tmp_path, CrashingDriver(), 9999999999)

    assert review.score == 4


def test_visual_review_session_does_not_fallback_by_default(monkeypatch, tmp_path):
    artifact_dir = tmp_path / ".autodev" / "visual"
    artifact_dir.mkdir(parents=True)
    artifact = VisualArtifact("desktop", artifact_dir / "desktop.png", "1440,1000")
    artifact.path.write_bytes(b"png")

    class FakeWorkspace:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeDriver:
        settings = settings()

        def review_visual(self, workspace, prompt, timeout):
            raise RuntimeError("OpenHands down")

    monkeypatch.setattr("autodev.runner.capture_visual_artifacts", lambda *args, **kwargs: (artifact,))
    monkeypatch.setattr("autodev.runner.LoopbackDockerWorkspace.create", lambda app, **kwargs: FakeWorkspace())
    monkeypatch.setattr("autodev.runner.normalize_permissions", lambda app: None)
    monkeypatch.setattr("autodev.runner.codex_cli_visual_review", lambda *args, **kwargs: pytest.fail("fallback should not run"))

    with pytest.raises(RunFailure, match="OpenHands visual review failed"):
        visual_review_session(tmp_path, FakeDriver(), 9999999999)


def test_existing_workspace_is_never_reset(tmp_path):
    (tmp_path / "keep.txt").write_text("keep")
    with pytest.raises(RunFailure, match="Refusing"):
        safe_workspace(tmp_path)


def test_mount_contains_only_workspace(tmp_path):
    assert workspace_mount(tmp_path).endswith(":/workspace:rw")
    assert str(tmp_path.resolve()) in workspace_mount(tmp_path)


def test_git_checkpoint_commits_source_without_runtime_state(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / ".autodev").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export {}\n")
    (tmp_path / ".autodev" / "visual-review.json").write_text("{}")

    commit = git_checkpoint(tmp_path, "feat: add app")

    assert commit
    files = subprocess.run(
        ["git", "-C", str(tmp_path), "show", "--format=", "--name-only", "HEAD"],
        text=True, capture_output=True, check=True,
    ).stdout.splitlines()
    assert files == ["src/app.ts"]


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


def test_sandbox_access_preserves_dependency_bin_execution(tmp_path):
    dependency_bin = tmp_path / "node_modules" / "tool" / "bin"
    dependency_bin.mkdir(parents=True)
    executable = dependency_bin / "tool.js"
    executable.write_text("#!/usr/bin/env node\n")
    package_bin = tmp_path / "node_modules" / ".bin"
    package_bin.mkdir()
    direct_target = tmp_path / "node_modules" / "vitest" / "vitest.mjs"
    direct_target.parent.mkdir()
    direct_target.write_text("#!/usr/bin/env node\n")
    direct_target.chmod(0o666)
    (package_bin / "vitest").symlink_to("../vitest/vitest.mjs")
    native_executable = tmp_path / "node_modules" / "@typescript" / "typescript-linux-x64" / "lib" / "tsc"
    native_executable.parent.mkdir(parents=True)
    native_executable.write_bytes(b"\x7fELFnative compiler")
    native_executable.chmod(0o666)

    from autodev.runner import grant_sandbox_access
    grant_sandbox_access(tmp_path)

    assert executable.stat().st_mode & 0o111 == 0o111
    assert direct_target.stat().st_mode & 0o111 == 0o111
    assert native_executable.stat().st_mode & 0o111 == 0o111


def test_report_redacts_secrets(tmp_path):
    report = report_text("key top-secret-value", "plan", tmp_path, 0, 0, None, [command("test", 1)],
                         "top-secret-value", ["top-secret-value"])
    assert "top-secret-value" not in report
    assert "[REDACTED]" in report


def test_retry_is_bounded_and_success_requires_all_checks(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr("autodev.runner.planning_session", lambda app, req, driver, deadline: "plan")
    monkeypatch.setattr("autodev.runner.visual_review_session", lambda *args: VisualReview(10, ()))

    outcomes = [[command("install", 1)], [command("install", 0), command("test", 0), command("build", 0)]]
    def fake_development(app, prompt, driver, deadline):
        calls.append(prompt)
        return outcomes.pop(0)
    monkeypatch.setattr("autodev.runner.development_session", fake_development)
    success, report = run_live(tmp_path, "requirement", settings(), report_dir=tmp_path / "reports")
    assert success
    assert len(calls) == 2
    assert "Mechanical verification failed" in calls[1]
    assert "SUCCESS" in report.read_text()


def test_retry_limit_produces_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("autodev.runner.planning_session", lambda app, req, driver, deadline: "plan")
    monkeypatch.setattr("autodev.runner.visual_review_session", lambda *args: VisualReview(10, ()))
    monkeypatch.setattr("autodev.runner.development_session", lambda *args: [command("test", 1)])
    success, report = run_live(tmp_path, "requirement", settings(), report_dir=tmp_path / "reports")
    assert not success
    assert "repair limit" in report.read_text()


def test_visual_retry_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr("autodev.runner.planning_session", lambda *args: "plan")
    monkeypatch.setattr("autodev.runner.development_session", lambda *args: [command("build", 0)])
    low_review = VisualReview(7, (VisualIssue("medium", "mobile", "task row", "Crowded", "Improve spacing"),))
    monkeypatch.setattr("autodev.runner.visual_review_session", lambda *args: low_review)
    limited = Settings("agent", None, Path("/missing/auth.json"), 2, 3, 60, 10, 10, 30, 1, 8, False)
    success, report = run_live(tmp_path, "requirement", limited, report_dir=tmp_path / "reports")
    assert not success
    assert "Visual review retry limit" in report.read_text()


def test_visual_score_alone_is_not_sufficient_for_success(monkeypatch, tmp_path):
    monkeypatch.setattr("autodev.runner.planning_session", lambda *args: "plan")
    monkeypatch.setattr("autodev.runner.development_session", lambda *args: [command("build", 0)])
    review = VisualReview(8, (VisualIssue("medium", "mobile", "task row", "Crowded", "Improve spacing"),))
    monkeypatch.setattr("autodev.runner.visual_review_session", lambda *args: review)

    success, report = run_live(tmp_path, "requirement", settings(), report_dir=tmp_path / "reports")

    assert not success
    assert "Visual review retry limit" in report.read_text()
