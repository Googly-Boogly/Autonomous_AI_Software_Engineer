# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A bounded autonomous software engineer. Given a task and a local git repo, it analyzes the
repo, plans, implements in an isolated git worktree, validates with allowlisted checks,
repairs within fixed limits, runs deterministic diff checks, and stops at `WAITING_FOR_HUMAN`.
It never merges, pushes, or deploys. The governing rule: **the model proposes, deterministic
Python decides.** Any change that lets model output directly change run state, pick a command
line, pick a file path without a policy check, or count as proof of success violates the design.

Phase 1 (local MVP) is done. Phases 2–6 (full GREEN/YELLOW/RED policy, GitHub, review agent,
eval harness, FastAPI UI) are planned in `docs/PLAN.md`. Don't build ahead of the current phase
unless asked.

## Commands

```bash
.venv/bin/pip install --no-binary claude-agent-sdk -e '.[dev,claude-code,anthropic]'
.venv/bin/python -m pytest -q                                    # full suite (~30 s)
.venv/bin/python -m pytest tests/unit/test_path_policy.py -q      # one file
.venv/bin/python -m pytest -q -k "repair_loop"                    # by name
.venv/bin/ruff check .
.venv/bin/mypy                                                    # strict, covers engineer/ only

.venv/bin/python -m engineer.examples          # materialize tests/fixtures/repos/* as git repos in ./examples
.venv/bin/python -m engineer.run --repo ./examples/sample-fastapi --task "..."        # real run (Claude Code)
.venv/bin/python -m engineer.run --repo ./examples/sample-fastapi --task "..." \
    --provider scripted --script tests/fixtures/scripts/sample-fastapi.json         # offline, no model

docker compose build && docker compose run --rm test           # same checks in the container
docker compose run --rm engineer --repo examples/... --task "..."   # entrypoint is engineer.run
```

`make` is not installed on the dev machine; the `Makefile` is only a convenience wrapper.
Keep `--no-binary claude-agent-sdk`: without it, pip downloads a bundled Claude Code binary
(very slow here) instead of using the `claude` on PATH.

## Architecture (the parts that span files)

**Run flow.** `engineer/orchestrator/orchestrator.py` is the only code that changes
`EngineeringRun.state`, and it does so only through `transition()` in `state_machine.py`,
whose explicit `ALLOWED_TRANSITIONS` table raises on anything else. If you add a state or
edge, update that table and `tests/unit/test_state_machine.py`. `_move()` persists after
every transition. `_finish()` always runs, even on failure: it commits only
policy-permitted paths to the run branch (`[INCOMPLETE]` prefix on failure) and writes
artifacts to `data/runs/<run_id>/artifacts/`.

**Agent backends** (`engineer/agents/backends.py`). The orchestrator only knows the
`AgentBackend` protocol (`planner()` / `engineer()`). There are two implementations:
- `ClaudeCodeBackend` (`agents/claude_code.py`, the default). Claude Code runs the loop via the
  Claude Agent SDK. Every tool call hits a `PreToolUse` hook that calls the pure, deny-by-default
  `gate_tool_request()` in `policy/claude_code_gate.py`. `setting_sources=[]`,
  `strict_mcp_config=True` and `allowed_tools=[]` are security-relevant, not stylistic.
  Our Toolbox tools are exposed as an in-process MCP server (`mcp_tools_for`). Those handlers
  run in a worker thread, which is why `Store` uses `check_same_thread=False` plus a lock.
- `ProviderBackend`. Our own tool loops (`agents/planner.py`, `agents/engineer.py`) over a
  `ModelProvider` (`providers/`: Anthropic Messages API, or `ScriptedProvider`).

A bare `ModelProvider` passed to `Orchestrator` is auto-wrapped in `ProviderBackend`. Most
tests rely on this.

**Two policy layers must stay in sync.** `Toolbox` (`tools/toolbox.py`) is how *our* tools
act: schema-validated args, then `PathPolicy` / `CommandRegistry`, then a recorded
`ToolAction`. Claude Code's *built-in* tools never touch the Toolbox. They are allowed only
by `claude_code_gate.py`. When exposing a new tool, add it to the Toolbox, to the right
`*_MCP_TOOLS` set in `claude_code.py`, and to the gate tests. Built-ins not in
`BUILTIN_FILE_TOOLS`/`BUILTIN_HARMLESS` are denied by default (Glob/Grep are deliberately
excluded because they could read ignored secret files).

**Repo views.** Analysis and planning read a `CommitView` (git objects at the base commit),
never the operator's working tree. Implementation reads a `WorktreeView`. Both filter
through `PathPolicy`. `PathPolicy.resolve()` takes repo-relative paths only. The gate
converts Claude Code's absolute paths first.

**Anything shown to a model or committed must be policy-filtered.** Use
`pending_changes()` + a policy filter + `diff_paths()` / `commit_paths()` from
`git/worktree.py`. Never use `git add -A` or an unfiltered `git diff`: that was a real
secret-leak bug (an untracked `.env` created by a test run exposed via intent-to-add).

**Success is decided only by** `ValidationSummary.passed` (at least one required check ran,
and all required checks passed) plus the blocking checks in `orchestrator/diff_checks.py`.
An agent's `finish` / final message only ends its iteration.

**Subprocesses.** Everything goes through `process.run_process` (list argv only,
`shell=False`, timeouts). Commands that execute repository code use `scrubbed_env()`
(allowlisted vars, per-run `HOME`). Git calls made by trusted code go through `git/cli.py`.

## Tests

- `tests/fixtures/repos/*` are plain directories (not nested git repos). Tests materialize
  them into tmp git repos via the `make_repo` fixture in `tests/conftest.py`. pytest is
  configured not to collect them.
- `tests/fixtures/scripts/*.json` are scripted model turns tied to exact fixture file
  contents (e.g. `replace_in_file` `old` strings). Edit fixture repos and scripts together.
  The `sample-fastapi` script makes a deliberate mistake in iteration 1 to exercise the
  repair loop.
- `tests/integration/test_claude_code_backend.py` uses `FakeClaudeCode`, injected via
  `ClaudeCodeBackend(client_factory=...)`. It calls our real hook and MCP handlers, so it
  tests the gate end to end without the `claude` CLI. The suite never makes live model calls.
- Adversarial tests (`tests/adversarial/`) assert that denials are recorded *and* that the
  legitimate work still completes.
- Ruff's `S` (bandit) rules are on. Tests deliberately use fake secrets and hostile inputs
  (per-file ignores in `pyproject.toml`).

## Gotchas

- Don't point the engineer at this repository: it could edit its own policy code on a branch
  (the Phase 2 RED rule doesn't exist yet).
- Validation runs target-repo code unsandboxed as the OS user. Only use trusted repos.
- `data/` (runs, worktrees, `engineer.db`) and `examples/` are generated and gitignored.
  Deleting `data/` leaves worktree registrations behind in the example repos. Clean them
  with `git -C examples/<name> worktree prune`.
- `Settings` is frozen. Tests vary it with `settings.model_copy(update={...})`.
