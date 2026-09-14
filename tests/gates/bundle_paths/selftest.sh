#!/usr/bin/env bash
# selftest.sh — prove the bundle-vs-component gate goes BOTH red and green.
#
# The gate compares two sources that claim the same truth: the component's
# URL_BASE / DOMAIN, and the paths compiled into the vendored wizard bundle.
# On 2026-09-14 they disagreed, the hash gate said OK, and a freshly flashed
# device rendered a blank page.
#
# This drives the REAL scripts/build_bundle.sh — copied out of the repo at run
# time, never restated here. A self-test that re-implements the check tests a
# copy of itself: it stays green while the real gate rots, which is exactly the
# failure class the gate exists to catch.
#
#   must-fail/  the gate MUST flag these
#   must-pass/  the gate must NOT flag these — not padding. A gate that flags
#               everything is overridden by reflex, which is a slower way of
#               having no gate at all. Every stock Home Assistant namespace
#               lives here so it can never become a false positive again.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"
REAL="${REPO_ROOT}/scripts/build_bundle.sh"
[ -f "${REAL}" ] || { echo "ERROR: ${REAL} not found — cannot self-test" >&2; exit 2; }
grep -q 'verify_bundle_agrees_with_component' "${REAL}" || {
  echo "ERROR: ${REAL} no longer defines verify_bundle_agrees_with_component —" >&2
  echo "       the gate this self-test guards is gone. FAILING, not skipping." >&2
  exit 2; }

GREEN='\033[0;32m' RED='\033[0;31m' RESET='\033[0m'
[[ -t 1 ]] || { GREEN=''; RED=''; RESET=''; }
pass=0 fail=0
_ok()  { pass=$((pass+1)); printf "  ${GREEN}ok${RESET}    %s\n" "$1"; }
_bad() { fail=$((fail+1)); printf "  ${RED}FAIL${RESET}  %s\n" "$1"; }

# Build a throwaway tree shaped like the repo, with the REAL script inside it
# (the script derives its roots from its own location).
#   $1 dir  $2 URL_BASE line  $3 DOMAIN line  $4 bundle js content
_fixture() {
  local d="$1" urlbase="$2" domain="$3" js="$4"
  mkdir -p "${d}/scripts" "${d}/src/greenautarky_site/frontend_bundle/frontend_latest"
  cp "${REAL}" "${d}/scripts/build_bundle.sh"
  printf '%s\n' "${urlbase}" > "${d}/src/greenautarky_site/__init__.py"
  printf '%s\n' "${domain}"  > "${d}/src/greenautarky_site/const.py"
  printf '%s\n' "${js}" > "${d}/src/greenautarky_site/frontend_bundle/frontend_latest/greenautarky-setup.abc123.js"
  # The gate derives the panel's own i18n namespace from the entry name, so a
  # fixture needs the same provenance file a real bundle carries.
  printf 'source_repo: fixture\nsource_ref: 0000000\nentry: greenautarky-setup\n' \
    > "${d}/src/greenautarky_site/frontend_bundle/BUILD-INFO.txt"
  bash "${d}/scripts/build_bundle.sh" --hash >/dev/null 2>&1
}

GOOD_URLBASE='URL_BASE = "/greenautarky_site_static"'
GOOD_DOMAIN='DOMAIN = "greenautarky_site"'
# A correct bundle also talks to stock Home Assistant APIs. Those must pass.
GOOD_JS='fetch("/api/greenautarky_site/status");u="/greenautarky_site_static/x.js";i="/api/image/1";h="/api/hassio/x";w="/api/webhook/y"'

_case() { # name  expect(fail|pass)  urlbase  domain  js
  local name="$1" expect="$2" d out rc
  d="$(mktemp -d)"; trap 'rm -rf "${d}"' RETURN
  _fixture "${d}" "$3" "$4" "$5"
  case "${name}" in *"BUILD-INFO entry unreadable"*)
    rm -f "${d}/src/greenautarky_site/frontend_bundle/BUILD-INFO.txt"
    bash "${d}/scripts/build_bundle.sh" --hash >/dev/null 2>&1 ;;
  esac
  out="$(bash "${d}/scripts/build_bundle.sh" --check 2>&1)"; rc=$?
  if [ "${expect}" = "fail" ]; then
    [ "${rc}" -ne 0 ] && _ok "must-fail: ${name}" || { _bad "must-fail: ${name} — gate stayed GREEN"; echo "${out}" | sed 's/^/        /'; }
  else
    [ "${rc}" -eq 0 ] && _ok "must-pass: ${name}" || { _bad "must-pass: ${name} — gate flagged a correct bundle"; echo "${out}" | sed 's/^/        /'; }
  fi
  rm -rf "${d}"; trap - RETURN
}

echo "bundle-paths gate self-test"

# --- must-fail: the four ways this goes wrong -------------------------------
_case "pre-rename static path (the rc34 defect)" fail "${GOOD_URLBASE}" "${GOOD_DOMAIN}" \
  'u="/greenautarky_onboarding_static/x.js";fetch("/api/greenautarky_site/s")'
_case "pre-rename api namespace (the rc34 defect)" fail "${GOOD_URLBASE}" "${GOOD_DOMAIN}" \
  'u="/greenautarky_site_static/x.js";fetch("/api/greenautarky_onboarding/s")'
_case "dev-build placeholder vendored into a release" fail "${GOOD_URLBASE}" "${GOOD_DOMAIN}" \
  "${GOOD_JS};v=\"0.0.0.dev0-abc1234\""
_case "zero coverage — a scan that inspected nothing" fail "${GOOD_URLBASE}" "${GOOD_DOMAIN}" \
  'console.log("no component paths in here at all")'
_case "URL_BASE unreadable — must FAIL, never skip" fail "# URL_BASE was renamed away" "${GOOD_DOMAIN}" \
  "${GOOD_JS}"

# --- the runtime-assembled shape: no literal path to grep at all -------------
# Found 2026-09-14 by testing this gate against the shape the FIXED producer
# emits. It derives every URL from one constant, so /api/<domain>/ never
# appears as a literal — and the api check above silently dropped to zero
# references and passed a bundle whose constant had been flipped back. A gate
# that can no longer fail is the defect it exists to catch, in its own mirror.
_case "runtime-assembled path, constant flipped to the retired namespace" fail \
  "${GOOD_URLBASE}" "${GOOD_DOMAIN}" \
  '.p="/greenautarky_site_static/frontend_latest/";const a=JSON.parse(String.raw`{"b":"greenautarky_onboarding"}`).b,i=`/api/${a}`;fetch(`${i}/status`)'

# --- must-pass: correct bundles, including stock HA namespaces --------------
_case "correct paths + stock HA namespaces" pass "${GOOD_URLBASE}" "${GOOD_DOMAIN}" "${GOOD_JS}"
_case "runtime-assembled path, constant correct — no literal to grep" pass \
  "${GOOD_URLBASE}" "${GOOD_DOMAIN}" \
  '.p="/greenautarky_site_static/frontend_latest/";const a=JSON.parse(String.raw`{"b":"greenautarky_site"}`).b,i=`/api/${a}`;fetch(`${i}/status`);fetch("/api/image/1")'
# Two measured false positives from a real produced bundle, 2026-09-14. Not
# every greenautarky_* token is a path claim, and a gate that flags these gets
# overridden by reflex within a week.
_case "the panel's own i18n keys are not a path claim" pass "${GOOD_URLBASE}" "${GOOD_DOMAIN}" \
  "${GOOD_JS};t={\"ui.panel.greenautarky_setup.welcome.cta\":\"Los\",\"ui.panel.greenautarky_setup.common.next\":\"Weiter\"}"
_case "a WS command to a real sibling component is not a path claim" pass "${GOOD_URLBASE}" "${GOOD_DOMAIN}" \
  "${GOOD_JS};e.callWS({type:\"greenautarky_telemetry/get\"})"
# …but an unknown GA namespace still must not slip through on that excuse.
_case "an unknown greenautarky_* namespace is still flagged" fail "${GOOD_URLBASE}" "${GOOD_DOMAIN}" \
  "${GOOD_JS};e.callWS({type:\"greenautarky_onboarding/get\"})"
_case "BUILD-INFO entry unreadable -> FAIL, never skip" fail "${GOOD_URLBASE}" "${GOOD_DOMAIN}" \
  "${GOOD_JS}"

_case "a future rename, applied consistently to both sides" pass \
  'URL_BASE = "/ga_wizard_static"' 'DOMAIN = "ga_wizard"' \
  'fetch("/api/ga_wizard/status");u="/ga_wizard_static/x.js";i="/api/image/1"'

echo
printf "  %d passed, %d failed\n" "${pass}" "${fail}"
[ "${fail}" -eq 0 ] || exit 1
