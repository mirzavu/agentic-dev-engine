# Agentic Dev Engine

A Python development engine built around OpenHands agents and isolated Docker workspaces.

## Setup

Requires Python 3.12 or 3.13 and `uv`. Docker is required for agent workspaces.

```bash
uv sync
uv run pytest
```

Optional settings are listed in `.env.example`. Local credentials, generated applications, dependencies, and runtime output are excluded from this repository.
