"""The WRITER half of the reset-request contract (KB #169).

``tests/contract/reset_request_v1.json`` is a byte-identical copy of the same
file in the ga_manager repo. Each side asserts (a) that the shared bytes have
not changed under it, via ``CONTRACT_SHA256``, and (b) that its own half still
matches the fixture — this component produces it, ga_manager's ``validate()``
accepts it.

Why this exists: the marker schema lives in two repos with no compiler between
them, and that is precisely the drift class that produced both tenant-wipe
defects found on the K31 bench (phase 7 wrote one onboarding-trigger envelope
while the component expected another). Two independent definitions eventually
disagree; a shared fixture plus a recorded hash makes the disagreement a red
test in whichever repo moved.

To change the contract: bump ``schema_version``, add a new fixture file, update
BOTH repos and both hashes in the same rollout wave. ga_manager refuses any
schema_version it does not know, so an old addon can never misread a new one.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from homeassistant.auth.const import GROUP_ID_USER

from greenautarky_site.const import (
    DOMAIN,
    MASTER_USERS_FILE,
    PIN_FILE,
    RESET_REQUEST_FILE,
)
from greenautarky_site.household import GASiteResetRequestView

CONTRACT = Path(__file__).parent / "contract" / "reset_request_v1.json"
# Must equal the constant in ga_manager's tests/test_reset_request_contract.py.
CONTRACT_SHA256 = "e24ed4f32968c31428927edf5431a509291c7a573c85a4a48876614e6a0c8058"

DEVICE_PIN = "123456"


class _FakeStore:
    async def async_save(self, data: dict[str, Any]) -> None:
        pass


class _FakeRequest:
    def __init__(self, hass, body=None, hass_user=None) -> None:
        self.app = {"hass": hass}
        self._body = body or {}
        self._items = {"hass_user": hass_user} if hass_user is not None else {}

    async def json(self) -> dict[str, Any]:
        return self._body

    def __getitem__(self, key: str):
        return self._items[key]


def test_contract_bytes_are_unchanged() -> None:
    """If this fails, the shared fixture moved under one repo. Update BOTH
    copies and BOTH hashes, or the two halves have silently diverged."""
    assert hashlib.sha256(CONTRACT.read_bytes()).hexdigest() == CONTRACT_SHA256


@pytest.mark.asyncio
async def test_the_writer_produces_exactly_the_contract_shape(hass) -> None:
    """What this component writes must be field-for-field what ga_manager
    parses — same keys, same types, same nesting."""
    expected = json.loads(CONTRACT.read_text(encoding="utf-8"))

    hass.data[DOMAIN] = {"store": _FakeStore(), "state": {"completed": True}}
    master = await hass.auth.async_create_user("Master", group_ids=[GROUP_ID_USER])
    mpath = Path(hass.config.path(MASTER_USERS_FILE))
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps({"masters": [{"ha_user_id": master.id}]}))
    ppath = Path(hass.config.path(PIN_FILE))
    ppath.parent.mkdir(parents=True, exist_ok=True)
    ppath.write_text(DEVICE_PIN)

    marker_path = Path(hass.config.path(RESET_REQUEST_FILE))
    marker_path.unlink(missing_ok=True)
    resp = await GASiteResetRequestView().post(
        _FakeRequest(
            hass, {"confirm": "LÖSCHEN", "pin": DEVICE_PIN}, hass_user=master
        )
    )
    assert resp.status == 202, resp.body
    produced = json.loads(marker_path.read_text(encoding="utf-8"))

    assert set(produced) == set(expected), "key set drifted from the contract"
    for key, sample in expected.items():
        assert isinstance(produced[key], type(sample)), f"{key} changed type"
    assert set(produced["requested_by"]) == set(expected["requested_by"])

    # The two timestamps must be real, ordered and tz-aware — ga_manager
    # rejects anything it cannot parse, and treats a naive value as UTC.
    for key in ("requested_at", "expires_at"):
        parsed = datetime.fromisoformat(produced[key])
        assert parsed.tzinfo is not None, f"{key} must be tz-aware"
    assert datetime.fromisoformat(produced["expires_at"]) > datetime.fromisoformat(
        produced["requested_at"]
    )
    assert datetime.fromisoformat(produced["expires_at"]) > datetime.now(UTC)

    marker_path.unlink(missing_ok=True)
