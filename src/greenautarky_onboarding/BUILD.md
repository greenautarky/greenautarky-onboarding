# GreenAutarky onboarding — build and version pinning

## Two-phase onboarding architecture

1. **Phase 1 — Stock HA onboarding**: Runs automatically, creates admin account. Stock code is never modified.
2. **Phase 2 — Custom GA onboarding** (`/greenautarky-setup`): End user creates non-admin account, GDPR, info pages, analytics. Redirects to login after completion.

## Frontend bundle producer (reproducible)

The wizard's `frontend_bundle/` is **built from a pinned frontend source**, not
hand-edited. The frontend fork (`greenautarky/frontend`) is archived and is a
**build-time input only** — pinned by exact commit so the bundle is
reproducible.

| File | Role |
|------|------|
| `frontend.lock.yaml` (repo root) | pins the frontend `repo` + `ref` (commit), the `entry` name, and the `build_cmd` |
| `scripts/build_bundle.sh` | clones the pinned source, runs the frontend build, vendors the `greenautarky-setup` artifacts into `frontend_bundle/`; `--check` = integrity gate |
| `src/greenautarky_onboarding/frontend_bundle/BUILD-INFO.txt` | provenance the producer writes (`source_ref`, `built_at`) — `--check` compares it to the lock |

### To ship a panel change

1. Land the change on the frontend fork (`greenautarky/frontend`).
2. Bump `frontend.lock.yaml` → `frontend.ref` to the merged commit.
3. Run `scripts/build_bundle.sh` (or let release CI do it) → fresh `frontend_bundle/`.
4. Bump the component version (see below) and cut a release (`git tag vX.Y.Z`).

## Component version pinning

The component version must match across **three** places (enforced by
`ci.yml` `build-consistency` + `release.yml` drift gate):

| Location | File | Field |
|----------|------|-------|
| this repo | `pyproject.toml` | `version` |
| this repo | `src/greenautarky_onboarding/manifest.json` | `version` |
| this repo | git tag | `vX.Y.Z` |

The OS consumes it as an OCI artifact — the pin lives in
`ha-operating-system/version.yaml` → `components.greenautarky-onboarding`.

## CI build flow

1. Tag `vX.Y.Z` triggers `.github/workflows/release.yml`.
2. Drift gate asserts tag == pyproject == manifest.
3. `scripts/build_bundle.sh` (+ `--check`) rebuilds `frontend_bundle/` from the
   pinned frontend source.
4. The component dir is tarred and pushed as an OCI artifact to
   `ghcr.io/greenautarky/greenautarky-onboarding:<ver>` (+ GitHub Release).
5. GA OS pulls it at bake time (`sync-components.sh`); ga_manager places it on
   device. See ga-ihost-docs ADR-0012 + TIER-2-COMPONENTS.md.

## Backend endpoints (this component)

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/api/greenautarky_onboarding/status` | GET | No | Returns onboarding state |
| `/api/greenautarky_onboarding/gdpr` | POST | No | Accept GDPR consent |
| `/api/greenautarky_onboarding/create_user` | POST | No | Create non-admin user, returns `auth_code` |
| `/api/greenautarky_onboarding/complete` | POST | No | Mark onboarding complete |
| `/greenautarky-setup` | GET | No | Serve the panel HTML |

## Frontend panel

Located in `homeassistant_frontend/src/panels/greenautarky-setup/`. Steps: welcome → gdpr → user creation → info pages → analytics.

## Tests

- **Backend**: `venv/bin/python -m pytest tests/components/greenautarky_onboarding/ -v`
- **Frontend**: `npx vitest run` (from the frontend repo)
