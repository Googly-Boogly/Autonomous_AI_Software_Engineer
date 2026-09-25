"""Deterministic validation. The engine decides what "passing" means; model
opinions about correctness are never consulted."""

from __future__ import annotations

from pathlib import Path

from engineer.models.validation import ValidationResult, ValidationSummary
from engineer.policy.commands import CommandRegistry, CommandSpec
from engineer.process import run_process, scrubbed_env, tail

# pytest exit code 5 = "no tests collected". That is not evidence of anything.
_PYTEST_NO_TESTS = 5


class ValidationEngine:
    def __init__(self, registry: CommandRegistry, worktree: Path, sandbox_home: Path, *,
                 timeout: float, tail_chars: int) -> None:
        self.registry = registry
        self.worktree = worktree
        self.sandbox_home = sandbox_home
        self.timeout = timeout
        self.tail_chars = tail_chars

    def run_one(self, spec: CommandSpec) -> ValidationResult:
        self.sandbox_home.mkdir(parents=True, exist_ok=True)
        res = run_process(spec.argv, self.worktree, timeout=self.timeout,
                          env=scrubbed_env(home=self.sandbox_home))
        passed = res.exit_code == 0
        stderr = res.stderr
        if spec.id == "pytest" and res.exit_code == _PYTEST_NO_TESTS:
            passed = False
            stderr += "\n[validation] pytest collected no tests; this counts as a failure."
        return ValidationResult(
            command_id=spec.id,
            command=list(spec.argv),
            kind=spec.kind,
            required=spec.required,
            start_time=res.start_time,
            end_time=res.end_time,
            exit_code=res.exit_code,
            stdout_tail=tail(res.stdout, self.tail_chars),
            stderr_tail=tail(stderr, self.tail_chars),
            timed_out=res.timed_out,
            passed=passed,
        )

    def run_all(self, iteration: int) -> ValidationSummary:
        return ValidationSummary(
            iteration=iteration, results=[self.run_one(s) for s in self.registry.specs()]
        )


def failure_digest(summary: ValidationSummary, max_chars: int = 6000) -> str:
    """Compact, deterministic description of failures to hand back to the engineer."""
    parts: list[str] = []
    for r in summary.failures():
        status = "TIMED OUT" if r.timed_out else f"exit code {r.exit_code}"
        req = "required" if r.required else "advisory"
        parts.append(
            f"### {r.command_id} ({req}) failed: {status}\n"
            f"$ {' '.join([Path(r.command[0]).name, *r.command[1:]])}\n"
            f"--- stdout (tail) ---\n{r.stdout_tail.strip()}\n"
            f"--- stderr (tail) ---\n{r.stderr_tail.strip()}\n"
        )
    text = "\n".join(parts) or "No failures."
    return text[-max_chars:]
