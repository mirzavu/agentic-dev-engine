from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

from .config import ConfigurationError, Settings
from .runner import (OpenHandsDriver, SAMPLE_REQUIREMENT, new_workspace, planning_session,
                     initialize_git_repository, openhands_auth_volumes, run_live, safe_workspace)
from .workspace import LoopbackDockerWorkspace, normalize_permissions


def _requirement(args) -> str:
    return Path(args.requirement_file).read_text() if args.requirement_file else SAMPLE_REQUIREMENT


def login(settings: Settings, *, force: bool = False) -> None:
    from openhands.sdk import LLM

    LLM.subscription_login(
        vendor="openai",
        model=settings.codex_model or "gpt-5.5",
        force_login=force,
        open_browser=False,
        auth_method="device_code",
        timeout=settings.model_timeout_seconds,
    )
    print("OpenHands subscription login is ready.")


def verify_install(settings: Settings) -> None:
    from openhands.sdk import LLM

    import openhands.agent_server  # noqa: F401
    import openhands.sdk  # noqa: F401
    import openhands.tools  # noqa: F401
    import openhands.workspace  # noqa: F401
    if subprocess.run(["docker", "info"], capture_output=True, check=False).returncode:
        raise RuntimeError("Docker daemon is not ready")
    if settings.agent_kind == "agent":
        try:
            LLM.subscription_login(
                vendor="openai",
                model=settings.codex_model or "gpt-5.5",
                open_browser=False,
                timeout=settings.model_timeout_seconds,
            )
        except Exception as exc:
            raise RuntimeError("OpenHands subscription login is not ready; run `uv run autodev login`") from exc
    else:
        if subprocess.run(["codex", "login", "status"], capture_output=True, check=False).returncode:
            raise RuntimeError("Codex subscription login is not ready; run `codex login`")
        if not settings.codex_auth_path.is_file():
            raise RuntimeError("Codex subscription credential file is not available")
    with tempfile.TemporaryDirectory(prefix="autodev-probe-") as temp:
        app = Path(temp)
        app.chmod(0o777)
        driver = OpenHandsDriver(settings)
        with openhands_auth_volumes(driver) as auth_volumes:
            workspace = LoopbackDockerWorkspace.create(app, extra_volumes=auth_volumes)
            try:
                with workspace:
                    auth_probe = (
                        "python - <<'IN'\n"
                        "from pathlib import Path\n"
                        "state = Path.home() / '.openhands'\n"
                        "assert (state / 'auth' / 'openai_oauth.json').is_file()\n"
                        "(state / 'profiles').mkdir(exist_ok=True)\n"
                        "IN\n"
                    ) if settings.agent_kind == "agent" else ""
                    check = workspace.execute_command(
                        f"pwd && touch smoke.txt && test -w /workspace && {auth_probe}true",
                        cwd="/workspace",
                    )
                if check.exit_code or check.stdout.splitlines()[:1] != ["/workspace"]:
                    raise RuntimeError("Docker workspace cannot execute in /workspace")
                if not (app / "smoke.txt").is_file():
                    raise RuntimeError("Docker workspace cannot write to the mounted application workspace")
            finally:
                normalize_permissions(app)
    print("OpenHands SDK agent, Docker, subscription login, and isolated workspace are ready.")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="autodev")
    sub = parser.add_subparsers(dest="command", required=True)
    login_cmd = sub.add_parser("login")
    login_cmd.add_argument("--force", action="store_true")
    sub.add_parser("verify-install")
    for name in ("plan", "run-live"):
        command = sub.add_parser(name)
        command.add_argument("--requirement-file")
        command.add_argument("--workspace", type=Path)
        if name == "run-live":
            command.add_argument("--confirm", action="store_true")
            command.add_argument("--resume", action="store_true", help="explicitly resume an existing generated workspace")
    sub.add_parser("test")
    args = parser.parse_args(argv)
    if args.command == "test":
        return subprocess.run([sys.executable, "-m", "pytest", "-q"], check=False).returncode
    try:
        settings = Settings.from_env()
        if args.command == "login":
            login(settings, force=args.force)
            return 0
        if args.command == "verify-install":
            verify_install(settings)
            return 0
        requirement = _requirement(args)
        root = Path("generated-apps")
        app = safe_workspace(
            args.workspace,
            allow_plan_only=args.command == "run-live",
            allow_existing=getattr(args, "resume", False),
        ) if args.workspace else new_workspace(root, requirement)
        initialize_git_repository(app)
        if args.command == "plan":
            plan = planning_session(app, requirement, OpenHandsDriver(settings), time.monotonic() + settings.wall_clock_seconds)
            print(f"Plan written to {app / 'TASK.md'}")
            return 0
        if not args.confirm:
            raise ConfigurationError("run-live requires the explicit --confirm flag")
        success, report = run_live(app, requirement, settings, report_dir=Path("reports"))
        print(f"{'SUCCESS' if success else 'FAILURE'}: {report}")
        return 0 if success else 1
    except (ConfigurationError, RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
