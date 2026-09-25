import os

import pytest

from engineer.config import DEFAULT_SENSITIVE_PATTERNS
from engineer.policy.paths import PathDenied, PathPolicy


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "ws"
    (r / "src").mkdir(parents=True)
    (r / "src" / "app.py").write_text("x = 1\n")
    (r / ".env").write_text("SECRET=1\n")
    return r


@pytest.fixture
def policy(root):
    return PathPolicy(root, DEFAULT_SENSITIVE_PATTERNS)


def test_normal_source_file_is_allowed(policy, root):
    assert policy.resolve("src/app.py") == root / "src" / "app.py"
    assert policy.resolve("./src/app.py") == root / "src" / "app.py"


@pytest.mark.parametrize("path", [
    ".env", ".env.local", ".env.production", "config/.env",
    "id_rsa", "keys/id_rsa.pub", "home/.ssh/id_ed25519", ".ssh/config",
    "server.pem", "tls/private.key", "credentials.json", "secrets/db.txt",
    ".aws/credentials", ".git/config", ".git", ".netrc", ".pypirc",
])
def test_sensitive_paths_denied(policy, path):
    with pytest.raises(PathDenied):
        policy.resolve(path)


@pytest.mark.parametrize("path", [
    "/etc/passwd", "~/.ssh/id_rsa", "~", "../outside.txt", "src/../../outside",
    "..", "", "   ", "a\x00b",
])
def test_outside_workspace_and_malformed_paths_denied(policy, path):
    with pytest.raises(PathDenied):
        policy.resolve(path)


def test_symlink_escaping_workspace_denied(policy, root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.symlink(outside, root / "src" / "link.txt")
    with pytest.raises(PathDenied, match="outside the workspace"):
        policy.resolve("src/link.txt")


def test_symlink_to_sensitive_file_inside_workspace_denied(policy, root):
    os.symlink(root / ".env", root / "src" / "innocent.txt")
    with pytest.raises(PathDenied, match="sensitive"):
        policy.resolve("src/innocent.txt")


def test_forbidden_paths_from_task(root):
    p = PathPolicy(root, DEFAULT_SENSITIVE_PATTERNS, forbidden_paths=("migrations/", "*.lock"))
    for bad in ("migrations/0001.py", "poetry.lock"):
        with pytest.raises(PathDenied, match="forbidden"):
            p.resolve(bad)
    p.resolve("src/app.py")


def test_allowed_write_paths_restrict_writes_only(root):
    p = PathPolicy(root, DEFAULT_SENSITIVE_PATTERNS, allowed_write_paths=("src", "tests/"))
    p.resolve("README.md")  # reading elsewhere is fine
    p.resolve("src/new.py", for_write=True)
    p.resolve("tests/test_x.py", for_write=True)
    with pytest.raises(PathDenied, match="allowed paths"):
        p.resolve("README.md", for_write=True)
    with pytest.raises(PathDenied, match="allowed paths"):
        p.resolve("srcfoo/x.py", for_write=True)  # prefix must be a directory boundary


def test_cannot_write_workspace_root(policy):
    with pytest.raises(PathDenied):
        policy.resolve(".", for_write=True)


def test_is_readable(policy):
    assert policy.is_readable("src/app.py")
    assert not policy.is_readable(".env")
