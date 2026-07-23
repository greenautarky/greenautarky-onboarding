"""Tests for ``_migrate_legacy_console_secret``.

The helper moves the console-login HMAC secret from the legacy
addon-readable `/share/ga/console-login-secret` to the HA-Core-only
`/config/.storage/greenautarky_secrets/console_login_secret`. See
`docs/SECURITY.md` for the threat model.

Tests use `monkeypatch` to redirect the module-level path constants
into a `tmp_path` sandbox, so nothing touches `/share/` or `/config/`
on the host.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest


@pytest.fixture
def patched_secret_paths(tmp_path, monkeypatch):
    """Redirect both Path constants into a tmp sandbox.

    Returns the (legacy, new) Paths so each test can populate them as
    needed and assert the post-migration state.
    """
    legacy = tmp_path / "share" / "ga" / "console-login-secret"
    new = tmp_path / "config" / ".storage" / "greenautarky_secrets" / "console_login_secret"
    monkeypatch.setattr(
        "greenautarky_site.http.LEGACY_CONSOLE_LOGIN_SECRET_FILE",
        legacy,
    )
    monkeypatch.setattr(
        "greenautarky_site.http.CONSOLE_LOGIN_SECRET_FILE",
        new,
    )
    return legacy, new


def _migrate():
    """Re-import inside the test so monkeypatched paths take effect."""
    from greenautarky_site.http import _migrate_legacy_console_secret

    return _migrate_legacy_console_secret()


def test_migrate_noop_when_legacy_absent(patched_secret_paths):
    """No legacy file → nothing to do. Returns False, no new file."""
    _legacy, new = patched_secret_paths
    assert _migrate() is False
    assert not new.exists()


def test_migrate_moves_legacy_to_new_path_with_0600(patched_secret_paths):
    """Happy path: legacy exists, new doesn't → copy + chmod + unlink."""
    legacy, new = patched_secret_paths
    legacy.parent.mkdir(parents=True)
    legacy.write_text("super-secret-hmac-key", encoding="utf-8")
    legacy.chmod(0o644)  # legacy may have been world-readable

    assert _migrate() is True

    assert not legacy.exists(), "legacy file must be removed after migration"
    assert new.exists()
    assert new.read_text(encoding="utf-8") == "super-secret-hmac-key"
    # 0600 = rw for owner only — no group/other read
    mode = stat.S_IMODE(new.stat().st_mode)
    assert mode == 0o600, f"new file must be 0600, got {oct(mode)}"


def test_migrate_noop_when_new_already_populated(patched_secret_paths):
    """If both files exist, never overwrite the new path — the legacy
    value may be stale after a rotation that only hit the new path.
    But the legacy file MUST be unlinked so addons can't read it."""
    legacy, new = patched_secret_paths
    legacy.parent.mkdir(parents=True)
    legacy.write_text("OLD-stale-key", encoding="utf-8")
    new.parent.mkdir(parents=True)
    new.write_text("NEW-rotated-key", encoding="utf-8")
    new.chmod(0o600)

    assert _migrate() is False
    assert new.read_text(encoding="utf-8") == "NEW-rotated-key"
    assert not legacy.exists(), "legacy must be unlinked even when no migrate happened"


def test_migrate_idempotent_only_new_path_present(patched_secret_paths):
    """After a successful first-boot migration, subsequent boots are
    no-ops — the legacy file is gone, the new file stays untouched."""
    _legacy, new = patched_secret_paths
    new.parent.mkdir(parents=True)
    new.write_text("already-migrated", encoding="utf-8")
    new.chmod(0o600)

    assert _migrate() is False
    assert new.read_text(encoding="utf-8") == "already-migrated"


def test_migrate_creates_parent_dir(patched_secret_paths):
    """New path's parent dir doesn't exist on a fresh install — the
    helper must `mkdir -p` it, not raise FileNotFoundError."""
    legacy, new = patched_secret_paths
    legacy.parent.mkdir(parents=True)
    legacy.write_text("key", encoding="utf-8")
    # Deliberately DO NOT mkdir new.parent — that's the point of this test.
    assert not new.parent.exists()

    assert _migrate() is True
    assert new.exists()
    assert new.read_text(encoding="utf-8") == "key"


def test_migrate_returns_false_on_unreadable_legacy(patched_secret_paths, monkeypatch):
    """If the legacy file is_file()==True but read raises OSError, the
    helper must log + return False without raising, so integration
    setup keeps going. We simulate by monkeypatching `read_text`."""
    legacy, new = patched_secret_paths
    legacy.parent.mkdir(parents=True)
    legacy.write_text("k", encoding="utf-8")

    def _raise(*_a, **_kw):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _raise)

    # Must not raise
    assert _migrate() is False
    # Legacy file remains (we couldn't read it, so we don't delete it)
    assert legacy.exists()
    assert not new.exists()
