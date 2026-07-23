# GreenAutarky onboarding — build and version pinning

## Two-phase onboarding architecture

1. **Phase 1 — Stock HA onboarding**: Runs automatically, creates admin account. Stock code is never modified.
2. **Phase 2 — Custom GA onboarding** (`/greenautarky-setup`): End user creates non-admin account, GDPR, info pages, analytics. Redirects to login after completion.

## Frontend bundle — committed + content-hashed (fork-decoupled)

The wizard's `frontend_bundle/` ships **committed** and is verified by
**content-hash** (`frontend_bundle/SHA256SUMS`). It is **decoupled from the
frontend fork**: `greenautarky/frontend` is archived/read-only, so CI never
clones or builds it — the committed bytes are the source of truth. Same model
as `ga-frontend-bundle` (vendored + sha256-checked).

| File | Role |
|------|------|
| `src/greenautarky_site/frontend_bundle/SHA256SUMS` | sha256 of every committed bundle payload file — the integrity manifest |
| `src/greenautarky_site/frontend_bundle/BUILD-INFO.txt` | provenance (`source_ref`, `built_at`) of the committed bytes |
| `frontend.lock.yaml` (repo root) | records the source `repo`/`ref`/`build_cmd` for the OPTIONAL local regen only |
| `scripts/build_bundle.sh` | `--check` (offline sha256 gate, used by CI), `--hash` (recompute SHA256SUMS), `--regen` (optional local rebuild from source + re-hash) |

### To ship a panel change

1. Land the change on the frontend source (a local checkout — the fork is archived).
2. `scripts/build_bundle.sh --regen` (rebuild + re-vendor + re-hash) **or** build
   manually, copy the `greenautarky-setup.*` artifacts into `frontend_bundle/`,
   then `scripts/build_bundle.sh --hash`.
3. Commit the new bytes + `SHA256SUMS`; `--check` must pass.
4. Bump the component version (see below) and cut a release (`git tag vX.Y.Z`).

CI (`ci.yml` `bundle-integrity` + `release.yml`) runs `--check` on a fresh
checkout — a stale/frozen or tampered bundle fails the build **offline**, no
fork access needed.

## Component version pinning

The component version must match across **three** places (enforced by
`ci.yml` `build-consistency` + `release.yml` drift gate):

| Location | File | Field |
|----------|------|-------|
| this repo | `pyproject.toml` | `version` |
| this repo | `src/greenautarky_site/manifest.json` | `version` |
| this repo | git tag | `vX.Y.Z` |

The OS consumes it as an OCI artifact — the pin lives in
`ha-operating-system/version.yaml` → `components.greenautarky-site`.

## CI build flow

1. Tag `vX.Y.Z` triggers `.github/workflows/release.yml`.
2. Drift gate asserts tag == pyproject == manifest.
3. `scripts/build_bundle.sh` (+ `--check`) rebuilds `frontend_bundle/` from the
   pinned frontend source.
4. The component dir is tarred and pushed as an OCI artifact to
   `ghcr.io/greenautarky/greenautarky-site:<ver>` (+ GitHub Release).
5. GA OS pulls it at bake time (`sync-components.sh`); ga_manager places it on
   device. See ga-ihost-docs ADR-0012 + TIER-2-COMPONENTS.md.

## Backend endpoints (this component)

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/api/greenautarky_site/status` | GET | No | Returns onboarding state |
| `/api/greenautarky_site/gdpr` | POST | No | Accept GDPR consent |
| `/api/greenautarky_site/create_user` | POST | No | Create non-admin user, returns `auth_code` |
| `/api/greenautarky_site/complete` | POST | No | Mark onboarding complete |
| `/greenautarky-setup` | GET | No | Serve the panel HTML |

## Frontend panel

Located in `homeassistant_frontend/src/panels/greenautarky-setup/`. Steps: welcome → gdpr → user creation → info pages → analytics.

## Tests

- **Backend**: `venv/bin/python -m pytest tests/components/greenautarky_site/ -v`
- **Frontend**: `npx vitest run` (from the frontend repo)
