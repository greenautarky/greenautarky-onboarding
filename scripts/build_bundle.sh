#!/usr/bin/env bash
# build_bundle.sh — content-hashed producer for the onboarding wizard bundle.
#
# The wizard bundle ships COMMITTED in src/greenautarky_site/frontend_bundle/
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
BUNDLE="${REPO_ROOT}/src/greenautarky_site/frontend_bundle"
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

# --- the bundle and the component must agree on where things live -----------
#
# WHY: on 2026-09-14 a release shipped a wizard bundle built from a pre-rename
# tree. It requested /greenautarky_onboarding_static/ and 7x
# /api/greenautarky_onboarding/*, while the component serves URL_BASE
# (/greenautarky_site_static) and /api/$DOMAIN. Everything 404'd and a freshly
# flashed device rendered a blank page — no wizard, no onboarding at all.
#
# Nothing caught it. --check verified the bytes were unchanged and the file set
# intact; both were true. Neither answers whether the bytes are RIGHT. Two
# sources claim the same truth here — the component's constants and the paths
# compiled into the bundle — and they sat side by side in one repo, uncompared.
#
# The constants are read from the LIVE source, never restated here: a check
# that re-declares the value it guards tests a copy of itself and stays green
# while the real thing rots.
verify_bundle_agrees_with_component() {
  local init="${REPO_ROOT}/src/greenautarky_site/__init__.py"
  local constf="${REPO_ROOT}/src/greenautarky_site/const.py"
  local url_base domain rc=0

  url_base="$(sed -n 's/^URL_BASE = "\(.*\)"$/\1/p' "${init}" 2>/dev/null | head -1)"
  domain="$(sed -n 's/^DOMAIN = "\(.*\)"$/\1/p' "${constf}" 2>/dev/null | head -1)"

  # If the live definition cannot be read, FAIL — never skip. A guard that
  # quietly stops guarding is worse than no guard, because the green stays.
  [ -n "${url_base}" ] || {
    echo "::error::cannot read URL_BASE from ${init} — the gate cannot verify anything" >&2; return 1; }
  [ -n "${domain}" ] || {
    echo "::error::cannot read DOMAIN from ${constf} — the gate cannot verify anything" >&2; return 1; }

  local js_files
  js_files="$(find "${BUNDLE}" -type f -name '*.js' | LC_ALL=C sort)"
  [ -n "${js_files}" ] || {
    echo "::error::no .js files found under ${BUNDLE} — nothing was inspected" >&2; return 1; }

  # 1. Every "<something>_static/" prefix must be the component's URL_BASE.
  local seen_static=0 p
  while IFS= read -r p; do
    [ -n "${p}" ] || continue
    seen_static=$((seen_static+1))
    if [ "${p}" != "${url_base}/" ]; then
      echo "::error::bundle requests ${p} but the component serves ${url_base}/ (URL_BASE)" >&2
      rc=1
    fi
  done <<< "$(echo "${js_files}" | xargs grep -hoE '/[A-Za-z0-9_]+_static/' 2>/dev/null | LC_ALL=C sort -u)"

  # 2. Every GA api namespace must be the component's DOMAIN. Stock Home
  #    Assistant namespaces (/api/image/, /api/hassio/, /api/webhook/, ...) are
  #    legitimate and must NOT be flagged — a gate that flags everything gets
  #    overridden by reflex, which is a slower way of having no gate.
  local seen_api=0
  while IFS= read -r p; do
    [ -n "${p}" ] || continue
    seen_api=$((seen_api+1))
    if [ "${p}" != "/api/${domain}/" ]; then
      echo "::error::bundle calls ${p} but the component registers /api/${domain}/ (DOMAIN)" >&2
      rc=1
    fi
  done <<< "$(echo "${js_files}" | xargs grep -hoE '/api/greenautarky[A-Za-z0-9_]*/' 2>/dev/null | LC_ALL=C sort -u)"

  # 2b. The two checks above only see paths spelled out as LITERALS. A bundle
  #     that assembles its API path at runtime from a constant
  #     (`/api/${domain}`) has no literal to grep — and a gate that silently
  #     stops matching is worse than no gate, because the green stays. Measured
  #     2026-09-14: with the producer fixed to derive paths from one constant,
  #     the api check above dropped to ZERO references and happily passed a
  #     bundle whose constant had been flipped back to the retired namespace.
  #
  #     So the real discriminator is the NAMESPACE TOKEN, wherever it appears:
  #     every greenautarky_* token in the bundle must be the component's own
  #     DOMAIN (or its static mount, which is DOMAIN + "_static").
  local seen_token=0 tok
  while IFS= read -r tok; do
    [ -n "${tok}" ] || continue
    seen_token=$((seen_token+1))
    if [ "${tok}" != "${domain}" ] && [ "${tok}" != "${domain}_static" ]; then
      echo "::error::bundle carries the namespace token '${tok}' but the component is '${domain}'" >&2
      rc=1
    fi
  done <<< "$(echo "${js_files}" | xargs grep -hoE 'greenautarky_[a-z0-9_]+' 2>/dev/null | LC_ALL=C sort -u)"

  # Coverage, not exit code: a scan that inspected nothing is a failure, not a
  # pass. This is the shape that let the defect ship in the first place.
  if [ "${seen_static}" -eq 0 ] && [ "${seen_api}" -eq 0 ] && [ "${seen_token}" -eq 0 ]; then
    echo "::error::inspected $(echo "${js_files}" | wc -l) bundle files and found NO component path references — the gate matched nothing and cannot be trusted" >&2
    return 1
  fi

  # 3. A release artifact must not carry a dev-build version placeholder.
  local stamped
  stamped="$(echo "${js_files}" | xargs grep -lF '0.0.0.dev0' 2>/dev/null | head -3)"
  if [ -n "${stamped}" ]; then
    echo "::error::bundle carries the 0.0.0.dev0 placeholder — a dev build was vendored into a release:" >&2
    echo "${stamped}" | sed 's|^|::error::  |' >&2
    rc=1
  fi

  [ "${rc}" -eq 0 ] && echo "frontend_bundle agrees with the component — ${url_base}/ + /api/${domain}/ (${seen_static} static, ${seen_api} api, ${seen_token} namespace token(s))"
  return "${rc}"
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
    # Unchanged bytes and an intact file set are not the same as CORRECT bytes.
    verify_bundle_agrees_with_component || exit 1
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
