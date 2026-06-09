"""Tests for ``_migrate_legacy_pin``.

Sister to test_console_login_secret_migration.py. The PIN moves from
the v1.0.0..1.0.2 location `/config/ga-onboarding-pin` to the
v1.0.3+ location `/config/.storage/greenautarky_secrets/onboarding_pin`.

Because the PIN path is built dynamically off `hass.config.path()`, we
mock the hass.config.path resolution into a tmp_path sandbox instead of
patching module-level constants.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def fake_hass(tmp_path):
    """Build a minimal hass-like object whose `.config.path(rel)` joins
    `rel` onto a tmp directory mimicking `/config/`.

    Returns (hass, legacy_path, new_path) so each test can populate or
    assert the on-disk state.
    """
    cfg_root = tmp_path / "config"
    cfg_root.mkdir(parents=True)

    def _path(rel: str) -> str:
        # Mirror hass.config.path: join + return as string.
        return str(cfg_root / rel)

    hass = SimpleNamespace(config=SimpleNamespace(path=_path))
    legacy = Path(_path("ga-onboarding-pin"))
    new = Path(_path(".storage/greenautarky_secrets/onboarding_pin"))
    return hass, legacy, new


def _migrate(hass):
    """Import inside the test so the fake hass is exercised."""
    from greenautarky_onboarding.http import _migrate_legacy_pin

    return _migrate_legacy_pin(hass)


def test_migrate_noop_when_legacy_absent(fake_hass):
    """No legacy file → no-op, no new file created."""
    hass, _legacy, new = fake_hass
    assert _migrate(hass) is False
    assert not new.exists()


def test_migrate_moves_legacy_to_new(fake_hass):
    """Legacy present, new absent → migrates, removes legacy, chmod 0600."""
    hass, legacy, new = fake_hass
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("123456", encoding="utf-8")

    assert _migrate(hass) is True
    assert new.exists()
    assert new.read_text() == "123456"
    assert not legacy.exists()
    # 0600 — owner rw only
    assert (new.stat().st_mode & 0o777) == 0o600


def test_migrate_idempotent_with_new_only(fake_hass):
    """New file already populated, legacy absent → no-op, file untouched."""
    hass, _legacy, new = fake_hass
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text("preserved-pin", encoding="utf-8")

    assert _migrate(hass) is False
    assert new.read_text() == "preserved-pin"


def test_migrate_removes_stale_legacy_when_new_exists(fake_hass):
    """Both files present → keep new (= post-rotation), remove legacy."""
    hass, legacy, new = fake_hass
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("stale", encoding="utf-8")
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text("fresh", encoding="utf-8")

    assert _migrate(hass) is False
    assert new.read_text() == "fresh"
    assert not legacy.exists()


def test_migrate_creates_parent_dirs(fake_hass):
    """Legacy present, new path's parent dir absent → migration creates it."""
    hass, legacy, new = fake_hass
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("123456", encoding="utf-8")
    assert not new.parent.exists()

    assert _migrate(hass) is True
    assert new.parent.is_dir()
    assert new.exists()


def test_pin_file_path_uses_storage_location():
    """The v1.0.3+ `_pin_file_path` returns the .storage/ location."""
    from greenautarky_onboarding.const import PIN_FILE

    assert PIN_FILE == ".storage/greenautarky_secrets/onboarding_pin"


def test_legacy_pin_file_path_still_top_level():
    """The legacy constant still points at the v1.0.0..1.0.2 path."""
    from greenautarky_onboarding.const import PIN_FILE_LEGACY

    assert PIN_FILE_LEGACY == "ga-onboarding-pin"
