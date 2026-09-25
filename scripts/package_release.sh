#!/usr/bin/env bash
# package_release.sh — build the placement artifact greenautarky-site-<V>.tar.gz.
#
# Called by .github/workflows/release.yml, and by tests/test_release_package.py
# so the test exercises the very path that ships.
#
# The wizard bundle's .gz siblings are NOT committed. They are generated here,
# on a staged copy of the component, from the same bytes that get tarred, and
# `build_bundle.sh --check-compressed` then verifies THAT staged tree — the one
# that is packaged — so a release cannot ship without them, or with stale ones.
#
# Usage: scripts/package_release.sh <version> <out_dir>
set -euo pipefail

V="${1:?usage: $0 <version> <out_dir>}"
OUT="${2:?usage: $0 <version> <out_dir>}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"; trap 'rm -rf "${STAGE}"' EXIT

cp -a "${REPO_ROOT}/src/greenautarky_site" "${STAGE}/"
# Never trust leftovers from a developer tree: no stale .gz, no bytecode.
find "${STAGE}/greenautarky_site" -type f -name '*.gz' -delete
find "${STAGE}/greenautarky_site" -type d -name '__pycache__' -prune -exec rm -rf {} +

export GA_BUNDLE_DIR="${STAGE}/greenautarky_site/frontend_bundle"
"${REPO_ROOT}/scripts/build_bundle.sh" --compress
"${REPO_ROOT}/scripts/build_bundle.sh" --check-compressed

mkdir -p "${OUT}"
# Tarball root is the component dir, so a consumer extracts straight into
# /config/custom_components/ (tar xzf - -C .../custom_components).
tar czf "${OUT}/greenautarky-site-${V}.tar.gz" -C "${STAGE}" greenautarky_site
echo "Packaged ${OUT}/greenautarky-site-${V}.tar.gz"
