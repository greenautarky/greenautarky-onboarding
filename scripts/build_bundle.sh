#!/usr/bin/env bash
# build_bundle.sh — content-hashed producer for the onboarding wizard bundle.
#
# The wizard bundle ships COMMITTED in src/greenautarky_onboarding/frontend_bundle/
# and is verified by sha256 (frontend_bundle/SHA256SUMS). It is DECOUPLED from
# the frontend build: the committed bytes are the source of truth and CI's
# ci.yml/release.yml only re-verify their hashes offline (--check). Since
# 2026-07-09 greenautarky/frontend is un-archived with the provenance branches
# pushed, so --regen ALSO works in CI (manual produce-bundle workflow) — not
# just from a local checkout. This mirrors ga-frontend-bundle's vendored +
# hash-checked model.
#
# Modes:
#   scripts/build_bundle.sh --check   # OFFLINE integrity gate (CI + ci.yml):
#                                     # committed bytes must match SHA256SUMS.
#   scripts/build_bundle.sh --hash    # recompute SHA256SUMS from the committed
#                                     # bytes (run after a manual re-vendor).
#   scripts/build_bundle.sh --regen   # OPTIONAL regen: rebuild from the
#                                     # frontend source in frontend.lock.yaml,
#                                     # re-vendor into frontend_bundle/, re-hash.
#                                     # Needs node + the pinned ref (reachable on
#                                     # the remote since 2026-07-09; also runs in
#                                     # CI via the produce-bundle workflow).
#
# Default (no arg) = --check.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE="${REPO_ROOT}/src/greenautarky_onboarding/frontend_bundle"
SUMS="${BUNDLE}/SHA256SUMS"
INFO="${BUNDLE}/BUILD-INFO.txt"
LOCK="${REPO_ROOT}/frontend.lock.yaml"

# Files that are NOT payload (excluded from the hash manifest).
is_meta() { case "$1" in ./SHA256SUMS|./BUILD-INFO.txt) return 0;; *) return 1;; esac; }

hash_bundle() {
  # Deterministic, sorted list of payload files, relative to BUNDLE.
  ( cd "${BUNDLE}"
    find . -type f | LC_ALL=C sort | while read -r f; do
      is_meta "$f" && continue
      sha256sum "$f"
    done
  )
}

MODE="${1:---check}"
case "${MODE}" in
  --hash)
    hash_bundle > "${SUMS}"
    echo "Wrote $(grep -c . "${SUMS}") hashes to ${SUMS}"
    ;;

  --check)
    [ -f "${SUMS}" ] || { echo "::error::${SUMS} missing — run scripts/build_bundle.sh --hash" >&2; exit 1; }
    # Verify committed bytes match the manifest, AND the manifest still covers
    # exactly the payload set (no added/removed file slipped past the hash).
    ( cd "${BUNDLE}" && sha256sum --check --strict --quiet SHA256SUMS ) || {
      echo "::error::frontend_bundle bytes do not match SHA256SUMS" >&2; exit 1; }
    if ! diff <(hash_bundle | LC_ALL=C sort) <(LC_ALL=C sort "${SUMS}") >/dev/null; then
      echo "::error::frontend_bundle file set differs from SHA256SUMS (file added/removed)" >&2
      exit 1
    fi
    echo "frontend_bundle OK — $(grep -c . "${SUMS}") files match SHA256SUMS"
    ;;

  --regen)
    command -v git >/dev/null || { echo "::error::git required" >&2; exit 1; }
    lock_val() {
      grep -E "^[[:space:]]*$1:" "${LOCK}" | head -1 \
        | sed -E "s/^[^:]+:[[:space:]]*//; s/[[:space:]]*#.*$//; s/^\"//; s/\"$//; s/[[:space:]]+$//"
    }
    REPO="$(lock_val repo)"; REF="$(lock_val ref)"; ENTRY="$(lock_val entry)"
    BUILD_CMD="$(lock_val build_cmd)"; OUTPUT_ROOT="$(lock_val output_root)"
    WORK="$(mktemp -d)"; trap 'rm -rf "${WORK}"' EXIT
    echo "==> Cloning ${REPO} @ ${REF}"
    git clone --quiet --no-checkout "${REPO}" "${WORK}/frontend"
    git -C "${WORK}/frontend" checkout --quiet "${REF}"
    echo "==> Building (${BUILD_CMD})"
    ( cd "${WORK}/frontend" && eval "${BUILD_CMD}" )
    OUT="${WORK}/frontend/${OUTPUT_ROOT}"
    [ -d "${OUT}" ] || { echo "::error::build output '${OUT}' not found" >&2; exit 1; }
    # The wizard is a DEDICATED compilation (gulp build-ga-wizard, #512): its
    # frontend_latest/ + frontend_es5/ output dirs contain exactly the wizard's
    # chunk set (entry + every code-split chunk). Vendor the WHOLE dirs —
    # complete by construction; the served publicPath is the component's own
    # static mount, so nothing collides with the stock Core's /frontend_latest.
    rm -rf "${BUNDLE}/frontend_latest" "${BUNDLE}/frontend_es5"
    mkdir -p "${BUNDLE}/frontend_latest" "${BUNDLE}/frontend_es5"
    cp "${OUT}/${ENTRY}.html"                   "${BUNDLE}/${ENTRY}.html"
    find "${OUT}/frontend_latest" -maxdepth 1 -type f \( -name "*.js" -o -name "*.txt" \) \
      -exec cp {} "${BUNDLE}/frontend_latest/" \;
    find "${OUT}/frontend_es5" -maxdepth 1 -type f \( -name "*.js" -o -name "*.txt" \) \
      -exec cp {} "${BUNDLE}/frontend_es5/" \;
    { echo "source_repo: ${REPO}"; echo "source_ref: ${REF}"; echo "entry: ${ENTRY}"; \
      echo "built_at: $(date -u +%FT%TZ)"; } > "${INFO}"
    "$0" --hash
    echo "==> Regenerated + re-hashed. Review the diff and commit."
    ;;

  *)
    echo "usage: $0 [--check|--hash|--regen]" >&2; exit 2;;
esac
