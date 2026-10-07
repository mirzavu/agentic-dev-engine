# Agentic Dev Engine

A local Python experiment that turns an application requirement into a plan,
implemented code, verified build, and visual review. Failed checks trigger a
limited number of repair attempts, and every run produces a Markdown report.

## How it works

1. An OpenHands planning agent writes an implementation plan.
2. A development agent builds the application in an isolated Docker workspace.
3. The runner executes the application's documented install, test, and build commands.
4. Failed checks trigger repairs, up to the configured limit.
5. A dedicated reviewer evaluates desktop and mobile screenshots and requests visual refinements when needed.
6. Functional checks run again after visual changes; source checkpoints and a final report record the outcome.

The default integration uses OpenHands SDK `Conversation`, planning and development
agents, and `DockerWorkspace`. Subscription authentication uses
`LLM.subscription_login(...)`. An ACP integration and Codex CLI fallback are
available as optional experiment paths.

## Requirements

- Python 3.12 or 3.13 and `uv`.
- Docker for live agent runs and workspace verification.
- Git for generated application checkpoints.
- A ChatGPT/Codex subscription login for live runs.
- Chrome or Chromium for visual screenshot capture.
- Node.js and npm when building generated web applications that use them.

The core stack is Python, OpenHands SDK/tools/workspace, `python-dotenv`, and
`pytest`. Generated applications choose their own stack from the requirement.

## Setup and offline tests

```bash
uv sync
uv run autodev test
```

The tests run offline and do not start a live agent run. Configuration is optional:
copy `.env.example` to `.env` to supply overrides. `.env` is ignored by Git.

## Live usage

Authenticate and check the installation:

```bash
uv run autodev login
uv run autodev verify-install
```

Run the built-in task-management application requirement:

```bash
uv run autodev run-live --confirm
```

Or supply a requirement in a text file:

```bash
uv run autodev run-live --requirement-file requirement.txt --confirm
```

Generate a plan without implementing the application:

```bash
uv run autodev plan --requirement-file requirement.txt
```

Resume an existing generated workspace explicitly:

```bash
uv run autodev run-live --workspace generated-apps/my-app --resume --confirm
```

Live runs require `--confirm`. Existing non-empty workspaces require explicit
resumption; they are never reset or deleted by the runner.

## Project structure

| Path | Purpose |
| --- | --- |
| `src/autodev/runner.py` | Core planning, development, verification, repair, visual review, fallback, checkpoints, and reporting logic. |
| `src/autodev/workspace.py` | Docker lifecycle, loopback networking, workspace mounts, and permission normalization. |
| `src/autodev/config.py` | Environment configuration, validation, and execution limits. |
| `src/autodev/cli.py` | Login, installation checks, planning, live runs, and test commands. |
| `tests/` | Offline tests for configuration and the development pipeline. |
| `generated-apps/` | Generated application workspaces, including their independent Git histories. |
| `reports/` | Markdown run reports and local execution logs. |

Generated application verification commands live in `.autodev/verification.json`.
Install and test commands are required; a build command is optional. The generated
README must document its test command.

## Execution and visual quality limits

Defaults include two functional repairs, three visual refinement rounds, and a
45-minute overall run limit. `.env.example` lists configurable settings, including
`AUTODEV_MAX_REPAIRS`, `AUTODEV_WALL_CLOCK_SECONDS`, and
`AUTODEV_VISUAL_MAX_ROUNDS`. The Codex CLI fallback is disabled by default.

Visual approval requires an overall score of at least
`AUTODEV_VISUAL_SCORE_THRESHOLD` (default 8/10), every design dimension at least
7/10, no medium/high-severity findings, and a production-ready assessment from
the reviewer. Dimensions cover visual hierarchy, composition and density,
design coherence, task-flow UX, responsive design, and product character.

The generated application directory is the writable application mount. Subscription
authentication uses a separate temporary mount when needed; OpenHands conversation
state stays in the disposable container.
