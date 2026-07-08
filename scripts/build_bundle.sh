#!/usr/bin/env bash
# build_bundle.sh — PRODUCER for the onboarding wizard frontend bundle.
#
# Builds the `greenautarky-setup` panel from the frontend source pinned in
# frontend.lock.yaml and vendors the emitted artifacts into
# src/greenautarky_onboarding/frontend_bundle/. This is the step that used to
# be missing: the committed bundle was a hand-captured snapshot that drifted
# from source (telemetry redesign + copy fixes never reached devices). With
# this producer the bundle is reproducible from a pinned commit.
#
# Usage:
#   scripts/build_bundle.sh          # clone pinned source, build, vendor
#   scripts/build_bundle.sh --check  # integrity gate: assert the committed
#                                    # bundle was produced from the pinned ref
#
# Requires: git, node/yarn (for the real build). No yq — the flat lock file is
# parsed with grep/sed so this runs on a minimal CI runner.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCK="${REPO_ROOT}/frontend.lock.yaml"
DEST="${REPO_ROOT}/src/greenautarky_onboarding/frontend_bundle"
INFO="${DEST}/BUILD-INFO.txt"

[ -f "${LOCK}" ] || { echo "::error::${LOCK} not found" >&2; exit 2; }

# Minimal flat-YAML value reader: `lock_val <key>` → value with any trailing
# comment and surrounding quotes stripped. Keys are unique across the file.
lock_val() {
  grep -E "^[[:space:]]*$1:" "${LOCK}" | head -1 \
    | sed -E "s/^[^:]+:[[:space:]]*//; s/[[:space:]]*#.*$//; s/^\"//; s/\"$//; s/[[:space:]]+$//"
}

REPO="$(lock_val 'repo')"
REF="$(lock_val 'ref')"
ENTRY="$(lock_val 'entry')"
BUILD_CMD="$(lock_val 'build_cmd')"
OUTPUT_ROOT="$(lock_val 'output_root')"

[ -n "${REPO}" ] && [ -n "${REF}" ] && [ -n "${ENTRY}" ] \
  || { echo "::error::frontend.lock.yaml missing repo/ref/entry" >&2; exit 2; }

# ---- integrity check mode -------------------------------------------------
if [ "${1:-}" = "--check" ]; then
  if [ ! -f "${INFO}" ]; then
    echo "::error::${INFO} missing — bundle was never produced by build_bundle.sh" >&2
    exit 1
  fi
  built_ref="$(grep '^source_ref:' "${INFO}" | awk '{print $2}')"
  if [ "${built_ref}" != "${REF}" ]; then
    echo "::error::bundle STALE — built from '${built_ref}' but frontend.lock pins '${REF}'. Run scripts/build_bundle.sh." >&2
    exit 1
  fi
  echo "bundle OK — built from ${built_ref} (matches frontend.lock)"
  exit 0
fi

# ---- produce mode ---------------------------------------------------------
command -v git >/dev/null || { echo "::error::git not on PATH" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

echo "==> Cloning ${REPO} @ ${REF}"
git clone --quiet --no-checkout "${REPO}" "${WORK}/frontend"
git -C "${WORK}/frontend" checkout --quiet "${REF}"

echo "==> Building (${BUILD_CMD})"
( cd "${WORK}/frontend" && eval "${BUILD_CMD}" )

OUT="${WORK}/frontend/${OUTPUT_ROOT}"
[ -d "${OUT}" ] || { echo "::error::build output '${OUT}' not found — check output_root/build_cmd" >&2; exit 1; }

# Vendor the entry's HTML + hashed JS (both build flavours). Remove stale
# hashed files first so a new hash doesn't leave the old one behind.
mkdir -p "${DEST}/frontend_latest" "${DEST}/frontend_es5"
rm -f "${DEST}"/frontend_latest/"${ENTRY}".*.js "${DEST}"/frontend_es5/"${ENTRY}".*.js

cp "${OUT}/${ENTRY}.html"                     "${DEST}/${ENTRY}.html"
cp "${OUT}"/frontend_latest/"${ENTRY}".*.js   "${DEST}/frontend_latest/"
cp "${OUT}"/frontend_es5/"${ENTRY}".*.js       "${DEST}/frontend_es5/"

# ga-onboarding-redirect.js in DEST is hand-maintained (not a build artifact)
# and is intentionally left untouched.

{
  echo "source_repo: ${REPO}"
  echo "source_ref: ${REF}"
  echo "entry: ${ENTRY}"
  echo "built_at: $(date -u +%FT%TZ)"
} > "${INFO}"

echo "==> Vendored ${ENTRY} bundle into ${DEST}:"
ls -1 "${DEST}/${ENTRY}.html" "${DEST}"/frontend_latest/"${ENTRY}".*.js "${DEST}"/frontend_es5/"${ENTRY}".*.js
echo "==> BUILD-INFO:"; cat "${INFO}"
