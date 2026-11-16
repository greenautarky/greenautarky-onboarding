"""Start a REAL Home Assistant Core on localhost for the e2e tier (no device).

The e2e tests in this directory normally target a canary through GA_DEVICE_URL.
A canary is not always free (a bench device can be in use for a hand test) and a
release gate must not depend on one, so this brings up a throwaway real Core
with the two components under test installed from source:

* ``greenautarky_site``   from ``--site``   (default: this repo's src/)
* ``ga_frontend_bundle``  from ``--bundle`` (a ga-frontend-bundle checkout —
  pass the release you want to test, e.g. a v1.23.10 checkout for a red run)

What is REAL: Home Assistant Core (the version in the venv), its frontend
(``home-assistant-frontend``), auth, the entity / device / area registries, the
HTTP API, both components and the dashboard strategy. What is SEEDED: a small
Zigbee flat (``ga_e2e_seed``, through a real entity platform) and the file
ga_manager would have written (``/share/ga_manager/status.json``), relocated
with ``GA_MANAGER_STATUS_FILE``.

Needs a venv with ``homeassistant`` + ``home-assistant-frontend`` (``--hass``
points at its ``hass`` executable) and, to read the output, aiohttp.

    python tests/e2e/local_core/launcher.py \
        --hass /path/to/venv/bin/hass --bundle /path/to/ga-frontend-bundle \
        --port 18123 --config /tmp/ga-e2e-core

prints ``export GA_DEVICE_URL=...`` lines for the e2e tests and stays running
until interrupted. CLI only; tests import :class:`LocalCore`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

ADMIN = ("e2eadmin", "e2e-Admin-pw-1")
MASTER = ("e2emaster", "e2e-Master-pw-1")

CONFIG_YAML = """\
homeassistant:
  name: GA e2e
  latitude: 51.2
  longitude: 6.4
  elevation: 40
  unit_system: metric
  time_zone: Europe/Berlin
  language: de
  country: DE
http:
  server_port: {port}
frontend:
onboarding:
lovelace:
api:
websocket_api:
config:
conversation:
sensor:
logger:
  default: warning
  logs:
    custom_components.greenautarky_site: info
    custom_components.ga_frontend_bundle: info
greenautarky_site:
ga_frontend_bundle:
ga_e2e_seed:
"""

# What ga_manager's /share/ga_manager/status.json looks like (health_engine.py),
# reduced to the fields greenautarky_site reads.
def status_report(release: str, core: str, os_version: str = "17.1.ga1") -> dict:
    return {
        "timestamp": "2026-11-16T10:00:00Z",
        "ga_release": release,
        "overall": "ok",
        "checks": [
            {
                "name": "versions.collect",
                "state": "ok",
                "details": {
                    "os": {"version": os_version},
                    "supervisor": {"version": "2026.10.2"},
                    "core": {"version": core},
                    "frontend": {"pyversion": "20260128.6"},
                    "plugins": {"dns": {"version": "2026.10.0"}},
                    "addons": {
                        "99f1cad4_ga_manager": {"name": "GA Manager", "version": "0.277.6"},
                        "45df7312_zigbee2mqtt": {"name": "Zigbee2MQTT", "version": "2.12.1"},
                        "99f1cad4_ga_default_addon": {"name": "GA Default Add-on",
                                                      "version": "1.4.0"},
                        "99f1cad4_ga_hmvapp": {"name": "GA HMV App", "version": "0.9.2"},
                    },
                },
            }
        ],
    }


class LocalCore:
    def __init__(self, hass_bin: str, bundle: Path, config: Path, port: int,
                 site: Path | None = None) -> None:
        self.hass_bin = hass_bin
        self.bundle = Path(bundle)
        self.site = Path(site) if site else REPO / "src" / "greenautarky_site"
        self.config = Path(config)
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.status_file = self.config / "share" / "ga_manager" / "status.json"
        self.proc: subprocess.Popen | None = None
        self.master_user_id: str | None = None
        self._log = None

    # -- config -----------------------------------------------------------
    def prepare(self, release: str = "BOSv1.6.1-rc2", core: str = "2026.10.4") -> None:
        if self.config.exists():
            shutil.rmtree(self.config)
        cc = self.config / "custom_components"
        cc.mkdir(parents=True)
        (cc / "greenautarky_site").symlink_to(self.site)
        (cc / "ga_frontend_bundle").symlink_to(
            self.bundle / "custom_components" / "ga_frontend_bundle")
        shutil.copytree(HERE / "ga_e2e_seed", cc / "ga_e2e_seed")
        (self.config / "configuration.yaml").write_text(CONFIG_YAML.format(port=self.port))
        # GA onboarding finished, or every route redirects to the wizard.
        storage = self.config / ".storage"
        storage.mkdir()
        (storage / "greenautarky_site").write_text(json.dumps({
            "version": 2, "minor_version": 1, "key": "greenautarky_site",
            "data": {"completed": True, "steps_done": [], "consents": {}},
        }))
        # Home Assistant's OWN onboarding done, or the frontend redirects to it.
        (storage / "onboarding").write_text(json.dumps({
            "version": 4, "minor_version": 1, "key": "onboarding",
            "data": {"done": ["user", "core_config", "analytics", "integration"]},
        }))
        # The default dashboard as a device has it AFTER ITS FIRST BOOT:
        # greenautarky_site writes the strategy config at runtime, and Core only
        # turns a stored default dashboard into a panel with a `mode` at its next
        # start (lovelace._async_migrate_default_config) — without one the
        # frontend redirects /lovelace/* to /home. A device has been through many
        # boots; a throwaway Core has had none, so seed what the first boot writes.
        (storage / "lovelace").write_text(json.dumps({
            "version": 1, "minor_version": 1, "key": "lovelace",
            "data": {"config": {"strategy": {"type": "custom:ga-home"}}},
        }))
        self.write_status(release, core)
        subprocess.run(
            [self.hass_bin, "--script", "auth", "-c", str(self.config), "add", *ADMIN],
            check=True, capture_output=True)

    def write_status(self, release: str, core: str, os_version: str = "17.1.ga1") -> None:
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        self.status_file.write_text(json.dumps(status_report(release, core, os_version)))

    # -- process ----------------------------------------------------------
    def start(self, timeout: float = 240) -> None:
        env = dict(os.environ, GA_MANAGER_STATUS_FILE=str(self.status_file))
        self._log = open(self.config / "core.log", "wb")
        self.proc = subprocess.Popen(
            [self.hass_bin, "-c", str(self.config)], env=env,
            stdout=self._log, stderr=subprocess.STDOUT, start_new_session=True)
        asyncio.run(self._wait_ready(timeout))

    async def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        async with aiohttp.ClientSession() as s:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"Core exited early, see {self.config}/core.log")
                try:
                    async with s.get(self.url + "/api/greenautarky_site/status") as r:
                        if r.status == 200:
                            return
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(2)
        raise TimeoutError(f"Core not ready in {timeout}s, see {self.config}/core.log")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        if self._log:
            self._log.close()

    # -- users ------------------------------------------------------------
    @property
    def client_id(self) -> str:
        return f"{self.url}/"

    async def token(self, username: str, password: str) -> str:
        async with aiohttp.ClientSession() as s:
            async with s.post(self.url + "/auth/login_flow", json={
                    "client_id": self.client_id, "handler": ["homeassistant", None],
                    "redirect_uri": self.client_id}) as r:
                flow = await r.json()
            async with s.post(f"{self.url}/auth/login_flow/{flow['flow_id']}", json={
                    "client_id": self.client_id, "username": username,
                    "password": password}) as r:
                code = (await r.json()).get("result")
            assert code, "login flow did not finish"
            async with s.post(self.url + "/auth/token", data={
                    "grant_type": "authorization_code", "code": code,
                    "client_id": self.client_id}) as r:
                return (await r.json())["access_token"]

    async def create_master(self) -> str:
        """A NON-admin resident, flagged as the household master."""
        admin_token = await self.token(*ADMIN)
        async with aiohttp.ClientSession() as s, s.ws_connect(self.url + "/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": admin_token})
            assert (await ws.receive_json())["type"] == "auth_ok"

            async def call(i, payload):
                await ws.send_json({"id": i, **payload})
                msg = await ws.receive_json()
                assert msg.get("success"), msg
                return msg["result"]

            res = await call(1, {"type": "config/auth/create", "name": "Mieter Master",
                                 "group_ids": ["system-users"], "local_only": False})
            uid = res["user"]["id"]
            await call(2, {"type": "config/auth_provider/homeassistant/create",
                           "user_id": uid, "username": MASTER[0], "password": MASTER[1]})
        ga = self.config / "ga"
        ga.mkdir(exist_ok=True)
        (ga / "ga-master-users.json").write_text(json.dumps({"masters": [{"ha_user_id": uid}]}))
        self.master_user_id = uid
        return uid


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hass", required=True)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--port", type=int, default=18123)
    ap.add_argument("--site", help="a greenautarky_site package dir (default: this repo's src/)")
    args = ap.parse_args()
    core = LocalCore(args.hass, Path(args.bundle), Path(args.config), args.port,
                     Path(args.site) if args.site else None)
    core.prepare()
    core.start()
    asyncio.run(core.create_master())
    print(f"export GA_DEVICE_URL={core.url}")
    print(f"export GA_DEVICE_ADMIN_USERNAME={ADMIN[0]} GA_DEVICE_ADMIN_PASSWORD={ADMIN[1]}")
    print(f"export GA_DEVICE_MASTER_USERNAME={MASTER[0]} GA_DEVICE_MASTER_PASSWORD={MASTER[1]}")
    print(f"export GA_E2E_STATUS_FILE={core.status_file}", flush=True)
    try:
        signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        core.stop()


if __name__ == "__main__":
    sys.exit(main())
