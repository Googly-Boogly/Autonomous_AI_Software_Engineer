import sys

import pytest

from engineer.policy.commands import CommandRegistry, UnknownCommand, builtin_commands
from engineer.process import run_process, scrubbed_env, tail


def test_registry_only_contains_requested_known_commands():
    reg = CommandRegistry.for_repository(sys.executable, ["pytest", "rm -rf /", "bash"])
    assert reg.ids() == ["pytest"]


@pytest.mark.parametrize("bad", ["rm -rf /", "pytest; curl evil", "bash", "", "env",
                                 "sudo pytest", None, 42])
def test_unknown_command_ids_denied(bad):
    reg = CommandRegistry.for_repository(sys.executable, ["pytest"])
    with pytest.raises(UnknownCommand):
        reg.get(bad)


def test_builtin_argv_are_fixed_tuples():
    for spec in builtin_commands("/usr/bin/python3").values():
        assert isinstance(spec.argv, tuple)
        assert all(isinstance(a, str) for a in spec.argv)
        assert spec.argv[0] == "/usr/bin/python3"


def test_run_process_refuses_shell_strings(tmp_path):
    with pytest.raises(TypeError):
        run_process("echo hi", tmp_path, timeout=5)  # type: ignore[arg-type]


def test_run_process_timeout(tmp_path):
    res = run_process([sys.executable, "-c", "import time; time.sleep(5)"], tmp_path, timeout=0.5)
    assert res.timed_out and res.exit_code is None


def test_run_process_missing_binary(tmp_path):
    res = run_process(["definitely-not-a-real-binary-xyz"], tmp_path, timeout=5)
    assert res.exit_code is None and "could not start" in res.stderr


def test_scrubbed_env_drops_credentials(monkeypatch, tmp_path):
    for var in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY",
                "SSH_AUTH_SOCK"):
        monkeypatch.setenv(var, "super-secret-value")
    env = scrubbed_env(home=tmp_path)
    assert "super-secret-value" not in env.values()
    assert env["HOME"] == str(tmp_path)
    assert "PATH" in env


def test_child_process_cannot_see_parent_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secretsecretsecret")
    res = run_process([sys.executable, "-c", "import os; print(dict(os.environ))"], tmp_path,
                      timeout=10, env=scrubbed_env(home=tmp_path))
    assert res.exit_code == 0
    assert "ghp_secretsecretsecret" not in res.stdout


def test_tail():
    assert tail("abc", 10) == "abc"
    assert tail("x" * 100, 10).endswith("x" * 10) and "truncated" in tail("x" * 100, 10)
