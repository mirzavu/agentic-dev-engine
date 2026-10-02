import json
import subprocess
from pathlib import Path

import pytest

from autodev.config import Settings

from autodev.runner import (CheckResult, RunFailure, VerificationCommand, VisualArtifact, VisualIssue, VisualReview, append_visual_review_history, capture_visual_artifacts, development_session, git_checkpoint, implementation_prompt, load_verification_manifest, load_visual_review, redact, repair_prompt, safe_workspace, visual_fix_prompt, visual_review_prompt, visual_review_session)
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


def test_development_prompts_forbid_multi_heredoc_file_batches():
    guidance = "Do not create multiple files by pasting a large multi-heredoc shell script"

    assert guidance in implementation_prompt()
    assert guidance in repair_prompt(command("test", 1))
    assert guidance in visual_fix_prompt(
        VisualReview(7, (VisualIssue("medium", "desktop", "controls", "Crowded", "Improve spacing"),))
    )


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
