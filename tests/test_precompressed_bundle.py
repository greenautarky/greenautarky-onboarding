"""Every served wizard asset ships a precompressed ``.gz`` sibling.

WHY: measured 2026-09-25 on a canary running the shipped component, the
wizard's entry bundle ``frontend_latest/greenautarky-setup.<hash>.js`` went
over the wire UNCOMPRESSED — 735,608 bytes, no ``Content-Encoding`` — although
the browser sent ``Accept-Encoding: gzip, deflate, br``. Home Assistant's
static routes serve through aiohttp's ``FileResponse``, which does not
compress on the fly: it serves ``<file>.br`` / ``<file>.gz`` when such a
sibling exists on disk and the client accepts it, and the raw file otherwise.
Stock HA frontend ships those siblings; our vendored bundle did not.

The .gz files are NOT committed: they are generated at packaging time
(``scripts/package_release.sh``) and, for these tests, by
``scripts/build_bundle.sh --compress`` into the (git-ignored) checkout — the
same function the release runs. The packaged artifact itself is checked in
``test_release_package.py``.

The file set is derived from the component's REAL route registration
(``_scan_frontend_bundle``), never from a list restated here, so a newly
served directory or file is covered the day it is registered.
"""

from __future__ import annotations

import gzip
import hashlib
import subprocess
from pathlib import Path

import pytest

from greenautarky_site import _scan_frontend_bundle

# Asset types a browser actually fetches from the served routes. The
# *.LICENSE.txt files next to the chunks are never requested by the page.
COMPRESSIBLE = {".js", ".css", ".html"}


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", autouse=True)
def _compressed_bundle() -> None:
    """Generate the siblings with the real producer, as the release does."""
    subprocess.run(
        [str(REPO_ROOT / "scripts" / "build_bundle.sh"), "--compress"],
        check=True, capture_output=True, text=True,
    )


def _served_assets() -> list[Path]:
    configs = _scan_frontend_bundle()
    assert configs, "_scan_frontend_bundle() registered nothing"
    found: set[Path] = set()
    for cfg in configs:
        p = Path(cfg.path)
        if p.is_dir():
            found.update(
                f for f in p.rglob("*") if f.is_file() and f.suffix in COMPRESSIBLE
            )
        elif p.is_file() and p.suffix in COMPRESSIBLE:
            found.add(p)
    return sorted(found)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_served_assets_are_found() -> None:
    """Coverage, not exit code: zero inspected assets is a failure."""
    assets = _served_assets()
    # The wizard alone is hundreds of chunks in two flavours; a handful means
    # the scan broke, not that the bundle shrank.
    assert len(assets) > 100, f"only {len(assets)} served assets found"
    assert any(a.name.startswith("greenautarky-setup.") and a.suffix == ".js"
               for a in assets), "the wizard entry bundle is not among them"


def test_every_served_asset_has_a_gz_sibling() -> None:
    assets = _served_assets()
    assert assets
    missing = [str(a) for a in assets if not a.with_name(a.name + ".gz").is_file()]
    assert not missing, (
        f"{len(missing)}/{len(assets)} served assets have no .gz sibling, "
        f"e.g. {missing[:3]} — run scripts/build_bundle.sh --compress"
    )


def test_every_gz_decompresses_to_its_uncompressed_sibling() -> None:
    """The .gz must be the SAME version as the file next to it, byte for byte.

    aiohttp picks the .gz over the raw file whenever the client accepts gzip,
    so a stale .gz is served INSTEAD of the current code — silently, to every
    browser, while the raw file looks right on disk.
    """
    assets = _served_assets()
    assert assets
    checked, wrong = 0, []
    for a in assets:
        gz = a.with_name(a.name + ".gz")
        if not gz.is_file():
            continue
        checked += 1
        if _sha(gzip.decompress(gz.read_bytes())) != _sha(a.read_bytes()):
            wrong.append(str(gz))
    assert checked == len(assets), f"only {checked}/{len(assets)} .gz files compared"
    assert not wrong, f"{len(wrong)} .gz differ from their source: {wrong[:3]}"


def test_no_orphan_gz() -> None:
    """A .gz whose source is gone is still served for its URL — ship none."""
    bundle = Path(_scan_frontend_bundle()[0].path).parent
    orphans = [
        str(gz) for gz in bundle.rglob("*.gz")
        if not gz.with_name(gz.name[: -len(".gz")]).is_file()
    ]
    assert not orphans, f"orphan .gz files: {orphans[:3]}"


@pytest.mark.asyncio
async def test_static_route_answers_gzip_through_the_real_registration(
    hass, hass_client_no_auth
) -> None:
    """Seam: the component's real registration + HA's http + aiohttp FileResponse.

    Asks for the entry bundle and the HTML shell the way a browser does and
    asserts the answer is gzip-encoded AND decodes to the file on disk.
    """
    from homeassistant.setup import async_setup_component

    from greenautarky_site import URL_BASE, _async_register_frontend_bundle

    assert await async_setup_component(hass, "http", {"http": {}})
    await _async_register_frontend_bundle(hass)
    client = await hass_client_no_auth()

    entry = next(
        a for a in _served_assets()
        if a.parent.name == "frontend_latest" and a.name.startswith("greenautarky-setup.")
        and a.suffix == ".js"
    )
    html = next(a for a in _served_assets() if a.name == "greenautarky-setup.html")
    for url, src in (
        (f"{URL_BASE}/frontend_latest/{entry.name}", entry),
        ("/greenautarky-setup.html", html),
    ):
        resp = await client.get(url, headers={"Accept-Encoding": "gzip"})
        assert resp.status == 200, url
        assert resp.headers.get("Content-Encoding") == "gzip", (
            f"{url}: Content-Encoding={resp.headers.get('Content-Encoding')!r}"
        )
        # The test client transparently decodes; what arrives must be the
        # file on disk, not some other version.
        assert _sha(await resp.read()) == _sha(src.read_bytes()), url
