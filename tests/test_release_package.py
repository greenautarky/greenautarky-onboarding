"""The PACKAGED release artifact carries a .gz of every served wizard asset.

The .gz siblings are not committed; they exist only if the packaging path
generates them. So the check has to look at what is actually shipped: build
the tarball with the same script release.yml calls, unpack it, and inspect
the unpacked component — not the checkout, which may hold leftovers.
"""

from __future__ import annotations

import gzip
import hashlib
import subprocess
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPRESSIBLE = {".js", ".css", ".html"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_packaged_artifact_has_a_matching_gz_for_every_served_asset(tmp_path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run(
        [str(REPO_ROOT / "scripts" / "package_release.sh"), "0.0.0-test", str(out)],
        check=True, capture_output=True, text=True,
    )
    tarball = out / "greenautarky-site-0.0.0-test.tar.gz"
    assert tarball.is_file(), f"packaging produced no {tarball.name}"

    unpacked = tmp_path / "unpacked"
    with tarfile.open(tarball) as tf:
        tf.extractall(unpacked, filter="data")
    bundle = unpacked / "greenautarky_site" / "frontend_bundle"
    assert bundle.is_dir(), "the artifact carries no greenautarky_site/frontend_bundle"

    assets = sorted(
        p for p in bundle.rglob("*") if p.is_file() and p.suffix in COMPRESSIBLE
    )
    # Coverage, not exit code: the wizard is hundreds of chunks.
    assert len(assets) > 100, f"only {len(assets)} served assets in the artifact"

    missing, wrong = [], []
    for a in assets:
        gz = a.with_name(a.name + ".gz")
        if not gz.is_file():
            missing.append(str(a.relative_to(bundle)))
        elif _sha(gzip.decompress(gz.read_bytes())) != _sha(a.read_bytes()):
            wrong.append(str(gz.relative_to(bundle)))
    assert not missing, f"{len(missing)}/{len(assets)} without .gz in the artifact, e.g. {missing[:3]}"
    assert not wrong, f"{len(wrong)} .gz differ from their source in the artifact: {wrong[:3]}"

    orphans = [
        str(g.relative_to(bundle)) for g in bundle.rglob("*.gz")
        if not g.with_name(g.name[:-3]).is_file()
    ]
    assert not orphans, f"orphan .gz in the artifact: {orphans[:3]}"
