#!/usr/bin/env bash
#
# build_backend.sh — package the backend for Linux deployment WITHOUT shipping
# source (.py). Produces a sourceless .pyc bundle + tarball.
#
#   ./deploy/build_backend.sh            # default: sourceless .pyc bundle
#   MODE=plain ./deploy/build_backend.sh # plain copy (keeps .py — for debugging)
#
# IMPORTANT: .pyc files are tied to the Python MINOR version. Build on the SAME
# Python version that runs on the VM (e.g. build on 3.11 → deploy on 3.11).
# For unrecoverable source protection, see DEPLOYMENT.md → "Nuitka".
#
set -euo pipefail
cd "$(dirname "$0")/.."          # repo root

APP=ciaa
OUT=build/${APP}
MODE="${MODE:-pyc}"

# Python packages + entry/runtime files that make up the backend.
PKGS=(agents analysis api cicd config context core evaluation governance
      ingestion observability output storage)
FILES=(main.py requirements.txt)

echo "▶ Cleaning $OUT"
rm -rf "$OUT"; mkdir -p "$OUT"

echo "▶ Copying packages"
for p in "${PKGS[@]}"; do [ -d "$p" ] && cp -R "$p" "$OUT/"; done
for f in "${FILES[@]}"; do [ -f "$f" ] && cp "$f" "$OUT/"; done
cp deploy/gunicorn_conf.py "$OUT/" 2>/dev/null || true

echo "▶ Stripping secrets, caches and test data from the bundle"
rm -f  "$OUT/config/keys.json"                      # provided on the VM, never bundled
find "$OUT" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$OUT" -name '*.db' -delete 2>/dev/null || true
find "$OUT" -name '.env' -delete 2>/dev/null || true

if [ "$MODE" = "pyc" ]; then
  echo "▶ Compiling to bytecode (.pyc) and removing .py source"
  python3 -m compileall -b -q "$OUT"                # legacy layout: foo.pyc beside foo.py
  find "$OUT" -name '*.py' -delete                   # remove all source
  find "$OUT" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  echo "  → sourceless bundle (.pyc only)"
else
  echo "▶ MODE=plain — keeping .py source"
fi

TARBALL="${APP}-backend-$(date +%Y%m%d).tar.gz"
tar -czf "$TARBALL" -C build "${APP}"
echo "✔ Built $TARBALL"
echo "  Python build version: $(python3 -V)  (the VM must match this minor version)"
