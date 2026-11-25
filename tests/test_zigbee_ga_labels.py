"""Odoo #1905 contract v1, reader side: ga_manager's label file -> the resident's list."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_resident_info import _zdev, config_entry  # noqa: F401  (fixture)
from test_rooms import _area, _User

from greenautarky_site.scoping import resident_info as ri
from greenautarky_site.scoping.rooms import SCOPE_ALL

FIXTURE = Path(__file__).parent / "fixtures" / "zigbee_labels.v1.json"
FX = json.loads(FIXTURE.read_text())


def _file(tmp_path):
    p = tmp_path / "zigbee-labels.json"
    p.write_text(json.dumps({**FX["request"], "sha256": FX["sha256"], "written_at": "2026-11-25T08:00:00Z"}))
    return p


def test_fixture_and_path_are_the_contract():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == "364ff5a60831f699f554beb03282969d42115f485e4baed985396ad5681137eb"
    assert ri.DEFAULT_LABELS_FILE == "/share/ga_manager/zigbee-labels.json"


async def test_every_class_gets_its_label_by_ieee_and_keeps_its_name(hass, config_entry, tmp_path):  # noqa: F811
    bad, flur = await _area(hass, "Bad"), await _area(hass, "Flur")
    _zdev(hass, config_entry, "Thermostat 1", "zigbee2mqtt_0xaaaaaaaaaaaaaaa1", area=flur)
    _zdev(hass, config_entry, "Sensor 1", "zigbee2mqtt_0xAAAAAAAAAAAAAAA2", area=bad)
    _zdev(hass, config_entry, "Relais", "zigbee2mqtt_0xaaaaaaaaaaaaaaa3", area=bad)
    _zdev(hass, config_entry, "Ohne Label", "zigbee2mqtt_0xbbbbbbbbbbbbbbbb")
    status, labels = ri.read_labels(_file(tmp_path))
    rows = ri.build_zigbee_devices(hass, _User("u1"), SCOPE_ALL, [], labels)
    assert status == "ok"
    assert [(r["area"], r["name"], r["label"]) for r in rows] == [
        ("Bad", "Relais", "REN-GIR-00000005"),
        ("Bad", "Sensor 1", "THD-SON-00000327"),
        ("Flur", "Thermostat 1", "TRV-SON-00000012"),
        (None, "Ohne Label", None),
    ]


def test_a_changed_file_is_seen_on_the_next_read(tmp_path):
    p = _file(tmp_path)
    ri.read_labels(p)
    p.write_text(json.dumps({"schema": 1, "labels": {"0xaaaaaaaaaaaaaaa1": "TRV-SON-00000099"}}))
    assert ri.read_labels(p) == ("ok", {"0xaaaaaaaaaaaaaaa1": "TRV-SON-00000099"})


@pytest.mark.parametrize("text", ["{", "[]", '{"schema": 2, "labels": {}}', '{"schema": 1, "labels": []}'])
def test_a_damaged_file_is_unreadable_not_empty(tmp_path, text):
    p = tmp_path / "x.json"
    p.write_text(text)
    assert ri.read_labels(p) == ("unreadable", {})


def test_no_file_is_missing(tmp_path):
    assert ri.read_labels(tmp_path / "nope.json") == ("missing", {})


def test_free_text_is_never_shown_as_a_label(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"schema": 1, "labels": {"0xaaaaaaaaaaaaaaa1": "Papas Zimmer"}}))
    assert ri.read_labels(p) == ("ok", {})
