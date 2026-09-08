from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    pass


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    agent_kind: str
    codex_model: str | None
    codex_auth_path: Path
    max_repairs: int
    max_iterations: int
    wall_clock_seconds: int
    model_timeout_seconds: int
    command_timeout_seconds: int
    visual_review_seconds: int
    visual_max_rounds: int
    visual_score_threshold: int
    codex_cli_fallback: bool

    @classmethod
    def from_env(cls) -> "Settings":
        model = os.getenv("AUTODEV_CODEX_MODEL", "").strip() or None
        agent_kind = os.getenv("AUTODEV_OPENHANDS_AGENT_KIND", "agent").strip().lower()
        if agent_kind not in {"agent", "acp"}:
            raise ConfigurationError("AUTODEV_OPENHANDS_AGENT_KIND must be 'agent' or 'acp'")
        auth_path = Path(os.getenv("AUTODEV_CODEX_AUTH_PATH", str(Path.home() / ".codex" / "auth.json")))
        settings = cls(
            agent_kind=agent_kind,
            codex_model=model,
            codex_auth_path=auth_path,
            max_repairs=_positive_int("AUTODEV_MAX_REPAIRS", 2),
            max_iterations=_positive_int("AUTODEV_MAX_ITERATIONS", 75),
            wall_clock_seconds=_positive_int("AUTODEV_WALL_CLOCK_SECONDS", 2700),
            model_timeout_seconds=_positive_int("AUTODEV_MODEL_TIMEOUT_SECONDS", 360),
            command_timeout_seconds=_positive_int("AUTODEV_COMMAND_TIMEOUT_SECONDS", 600),
            visual_review_seconds=_positive_int("AUTODEV_VISUAL_REVIEW_SECONDS", 360),
            visual_max_rounds=_positive_int("AUTODEV_VISUAL_MAX_ROUNDS", 3),
            visual_score_threshold=_positive_int("AUTODEV_VISUAL_SCORE_THRESHOLD", 8),
            codex_cli_fallback=_bool("AUTODEV_CODEX_CLI_FALLBACK", False),
        )
        if settings.visual_score_threshold > 10:
            raise ConfigurationError("AUTODEV_VISUAL_SCORE_THRESHOLD must be 10 or less")
        return settings
