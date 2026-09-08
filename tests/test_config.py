import os

import pytest

from autodev.config import ConfigurationError, Settings


def test_codex_model_is_optional(monkeypatch):
    monkeypatch.delenv("AUTODEV_CODEX_MODEL", raising=False)
    assert Settings.from_env().codex_model is None


def test_openhands_agent_is_default_and_fallback_is_opt_in(monkeypatch):
    monkeypatch.delenv("AUTODEV_OPENHANDS_AGENT_KIND", raising=False)
    monkeypatch.delenv("AUTODEV_CODEX_CLI_FALLBACK", raising=False)
    monkeypatch.delenv("AUTODEV_MODEL_TIMEOUT_SECONDS", raising=False)
    settings = Settings.from_env()
    assert settings.agent_kind == "agent"
    assert settings.model_timeout_seconds == 360
    assert settings.codex_cli_fallback is False


def test_openhands_agent_kind_is_validated(monkeypatch):
    monkeypatch.setenv("AUTODEV_OPENHANDS_AGENT_KIND", "bad")
    with pytest.raises(ConfigurationError, match="AUTODEV_OPENHANDS_AGENT_KIND"):
        Settings.from_env()


def test_limits_must_be_positive(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("LLM_MODEL", "test/model")
    monkeypatch.setenv("AUTODEV_MAX_REPAIRS", "0")
    with pytest.raises(ConfigurationError, match="AUTODEV_MAX_REPAIRS"):
        Settings.from_env()
