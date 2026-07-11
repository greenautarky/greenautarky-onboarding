# Device + E2E tests

Two extra test tiers above the in-process suite. Both are **skipped by
default** (CI runs `-m 'not device and not e2e'` via pyproject addopts) and
only run when pointed at a real device. **Canaries only** — never point
these at a production device.

| Tier | What it proves | Command |
|---|---|---|
| `tests/device` (`-m device`) | The shipped system serves the use-case over the HA HTTP API (component + Core + config as baked) | `pytest tests/device -m device` |
| `tests/e2e` (`-m e2e`) | A customer can do it in a real browser (adds frontend bundle + auth UI) | `pytest tests/e2e -m e2e` (needs `pip install playwright && playwright install chromium`) |

Environment (both tiers):

```bash
export GA_DEVICE_URL=http://<device-ip>:8123     # NetBird/Tailscale mesh IP
export GA_DEVICE_MASTER_USERNAME=<master login>  # a flagged master, NOT admin
export GA_DEVICE_MASTER_PASSWORD=<password>
```

Conventions:

- Tests create throwaway users named `PyTest …` / `E2E …` with a random
  marker and **remove them in `finally`** — a crashed run may leave one
  behind; delete it via the master console.
- Master credentials must belong to a user flagged in
  `ga/ga-master-users.json` (the invite API is master-gated).
- Run from a machine with mesh access (laptop / remote1) — GitHub-hosted
  runners cannot reach devices; CI wiring is the manual
  `device-tests` workflow with a self-hosted/mesh runner, or run locally
  as part of the canary roll checklist.
