# Implementation plan

Guiding rule: **the model proposes; deterministic Python decides.** Each phase is
finished (tested, lint/type clean) before the next one starts.

## Phase 1 — Local MVP ✅ (this commit)

| Component | Module | Notes |
|---|---|---|
| Schemas | `engineer/models/` | Pydantic v2: task, run, plan, repo context, validation, tool action, report |
| State machine | `engineer/orchestrator/state_machine.py` | Explicit transition table; only Python transitions state |
| Orchestrator | `engineer/orchestrator/orchestrator.py` | Analyze → plan → worktree → implement/validate/debug loop → diff checks → hand-off |
| Repository analyzer | `engineer/analysis/analyzer.py` | Deterministic; reads a `CommitView` (git objects at HEAD), never the live tree |
| Planner | `engineer/agents/planner.py` | Read-only tools + `submit_plan` (validated `EngineeringPlan`) |
| Engineer | `engineer/agents/engineer.py` | One bounded iteration per call; `finish` ends its turn but cannot declare success |
| Tools | `engineer/tools/toolbox.py` | Typed, schema-validated, policy-checked, recorded. No shell, no eval |
| Path policy | `engineer/policy/paths.py` | Workspace confinement, symlink resolution, sensitive-file denylist, task allow/forbid lists |
| Command registry | `engineer/policy/commands.py` | Model refers to commands by ID only; argv fixed in code |
| Worktrees | `engineer/git/worktree.py` | `data/runs/<id>/worktree`, branch `ai-engineer/<task>-<slug>`; primary tree never touched |
| Validation | `engineer/validation/engine.py` | Scrubbed env, isolated `HOME`, timeouts; "no tests collected" is a failure |
| Diff checks | `engineer/orchestrator/diff_checks.py` | Empty diff, tests added, tests not removed, policy, size, unplanned files |
| Persistence | `engineer/persistence/store.py` | SQLite, versioned schema, append-only actions/validations/model calls/state changes |
| Agent backends | `engineer/agents/backends.py`, `claude_code.py` | **Claude Code (default)** via the Agent SDK, gated by a PreToolUse policy hook; or our own loop on a `ModelProvider` |
| Claude Code gate | `engineer/policy/claude_code_gate.py` | Deny-by-default decision for every Claude Code tool call |
| Providers | `engineer/providers/` | `ModelProvider` protocol; Anthropic adapter; deterministic scripted provider |
| CLI | `engineer/run.py`, `engineer/examples.py` | `python -m engineer.run`, `python -m engineer.examples` |

## Phase 2 — Safety + reliability (next)

- GREEN / YELLOW / RED action classifier in front of the toolbox (dependency files, CI,
  Docker, migrations → YELLOW; configurable block vs. manual approval).
- Structured JSON logging with secret redaction + redaction tests; append-only `audit_log`.
- Wall-clock runtime ceiling and global tool-call ceiling (cost + iteration limits exist).
- Crash recovery: resume a run from its persisted state (state is already persisted;
  resume logic is not yet implemented — interrupted runs are currently left as-is).
- Container isolation for validation commands (Docker) — the largest remaining risk.
- Extended adversarial suite.

## Phase 3 — GitHub
Issue → `EngineeringTask`; push feature branch; open PR; `REQUIRE_PRIVATE_REPOSITORY`;
least-privilege fine-grained token / GitHub App. Never merge.

## Phase 4 — Review agent
Separate read-only reviewer producing `ReviewReport`; blocking findings loop back through
the bounded implementation loop. Replaces/augments deterministic diff checks.

## Phase 5 — Evaluation harness
`python -m engineer.eval run|report` over fixture repos with success, iterations, cost,
unnecessary files changed, regressions, attempted policy violations.

## Phase 6 — Operator interface
Minimal FastAPI: `/health`, `/runs`, `/runs/{id}` (+ plan, diff, validation, audit),
approve / reject / cancel.
