"""Manifest + pyproject + CHANGELOG version-pin consistency.

Catches the classic drift where pyproject.toml gets bumped but the
manifest is forgotten (or vice versa). The CI runs this on every PR,
the release workflow runs it again before publishing the OCI artifact.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_manifest_matches_pyproject() -> None:
    """`manifest.json.version` must equal `pyproject.toml.version`."""
    root = _repo_root()

    py_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', py_text, flags=re.M)
    assert m, "pyproject.toml has no version line"
    py_version = m.group(1)

    mf = json.loads(
        (root / "src" / "greenautarky_onboarding" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    mf_version = mf["version"]

    assert py_version == mf_version, (
        f"version drift — pyproject.toml={py_version!r} but "
        f"manifest.json={mf_version!r}. Bump both together."
    )


def test_changelog_has_entry_for_current_version() -> None:
    """The version we're about to ship must have a CHANGELOG entry —
    forces a deliberate "what changed" note before a release."""
    root = _repo_root()

    py_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    py_version = re.search(r'^version\s*=\s*"([^"]+)"', py_text, flags=re.M).group(1)

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    # Be permissive about formatting: a "## X.Y.Z" header or "## vX.Y.Z" works.
    assert (
        f"## {py_version}" in changelog or f"## v{py_version}" in changelog
    ), f"CHANGELOG.md has no entry for version {py_version}"
