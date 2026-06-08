# GreenAutarky onboarding — build and version pinning

## Two-phase onboarding architecture

1. **Phase 1 — Stock HA onboarding**: Runs automatically, creates admin account. Stock code is never modified.
2. **Phase 2 — Custom GA onboarding** (`/greenautarky-setup`): End user creates non-admin account, GDPR, info pages, analytics. Redirects to login after completion.

## Frontend version pinning

The frontend version must match across **three files**:

| Location | File | Value |
|----------|------|-------|
| Frontend repo | `pyproject.toml` → `version` | `20251105.1` |
| Core repo | `homeassistant/components/frontend/manifest.json` → `requirements` | `home-assistant-frontend==20251105.1` |
| Core repo | `homeassistant/package_constraints.txt` | `home-assistant-frontend==20251105.1` |
| Core repo | `requirements_all.txt` | `home-assistant-frontend==20251105.1` |
| Core repo | `requirements_test_all.txt` | `home-assistant-frontend==20251105.1` |

**Versioning scheme:** Keep the upstream date prefix (`YYYYMMDD`), bump the patch number (`.N`) for each GA change. Example: upstream `20251105.0` → first GA change `20251105.1` → next `20251105.2`. When rebasing onto a new upstream release, start from `.0` again.

**Important:** The CI workflow (`build-ga-core.yml`) builds the frontend from source (not PyPI). The version numbers just need to match — they are not used to fetch a package.

## CI build flow

1. Push to `ga/custom-onboarding` triggers `.github/workflows/build-ga-core.yml`
2. CI clones `homeassistant_frontend` repo (same branch) and builds from source
3. Built frontend is packaged into the HA core Docker image
4. Image is consumed by GA OS for the iHost device

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
