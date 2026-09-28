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
| `scripts/build_bundle.sh` | `--check` (offline sha256 gate, used by CI), `--hash` (recompute SHA256SUMS), `--compress` (write the `.gz` siblings, not committed), `--check-compressed` (`--check` + every served asset has a matching `.gz`), `--regen` (optional local rebuild from source + re-hash) |
| `scripts/package_release.sh` | builds the release tarball: stage → `--compress` → `--check-compressed` → tar (called by `release.yml`) |

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

### Precompressed `.gz` siblings — generated at packaging, never committed

Home Assistant serves the bundle through aiohttp's `FileResponse`, which does
**not** compress on the fly: it sends `<file>.gz` (or `.br`) when that sibling
exists and the client accepts the encoding, and the raw file otherwise. So the
shipped component carries a `.gz` next to every served `.js`/`.css`/`.html`.

They are **not in git** (`.gitignore`) and not in `SHA256SUMS`.
`scripts/package_release.sh` copies the component to a staging dir, deletes
any leftover `.gz`, runs `build_bundle.sh --compress` (`gzip -9 -n`,
reproducible) and then `--check-compressed` **on that staged tree**, and tars
exactly that tree. `release.yml` calls it; a failed check fails the release.

**Invariant:** each `.gz` is generated from the same bytes in the same step,
and no `.gz` exists without its source — a stale `.gz` would be served
*instead of* the current file. Enforced by:

- `tests/test_release_package.py` — builds the tarball with
  `package_release.sh`, unpacks it, asserts every served asset has a `.gz`
  that decompresses to it (sha256), no orphans, more than 100 assets;
- `tests/test_precompressed_bundle.py` — runs `--compress` on the checkout,
  derives the file set from the component's real route registration, and
  asserts through HA's http component that the entry bundle and the HTML shell
  answer `Content-Encoding: gzip` and decode to the file on disk;
- `build_bundle.sh --check-compressed` in `ci.yml` bundle-integrity, with
  missing/stale/orphan must-fail fixtures in `tests/gates/bundle_paths/selftest.sh`.

Plain `--check` stays usable in a developer tree: with no `.gz` present it
passes; a stale or orphan `.gz` left over from a local `--compress` still fails
it. The "every asset MUST have one" rule lives in `--check-compressed`, which
only CI and the release run — after generating them.

No `.br`: the shipped aiohttp would serve it (it tries `.br` before `.gz`),
but every producer and CI job would need a brotli encoder for an estimated
further 15–20 %. Not worth a second sibling to keep in sync today.

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
4. `scripts/package_release.sh` generates and checks the `.gz` siblings, the
   staged component dir is tarred and pushed as an OCI artifact to
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
