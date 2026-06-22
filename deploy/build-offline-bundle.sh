#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# CIAA Impact Analyzer — OFFLINE bundle builder.
#
# Produces a single  ciaa-offline.tar.gz  containing everything an AIR-GAPPED
# Linux box needs: the source, a wheelhouse of Linux Python wheels, and the
# pre-built frontend (frontend/dist). Copy that one file to the isolated box,
# unpack, and run  sudo ./deploy/install-vm.sh --offline.
#
#   RUN THIS ON AN INTERNET-CONNECTED **LINUX** HOST whose CPU arch (x86_64/arm64)
#   and Python minor version (e.g. 3.11) MATCH the air-gapped target — Python
#   wheels with native code (pydantic-core, etc.) are platform-specific.
#
# Usage:
#   ./deploy/build-offline-bundle.sh            # full deps (requirements.txt)
#   ./deploy/build-offline-bundle.sh --minimal  # core only (requirements-minimal.txt)
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REQ_FILE="requirements.txt"
[[ "${1:-}" == "--minimal" ]] && REQ_FILE="requirements-minimal.txt"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SRC_DIR"

OUT="$SRC_DIR/ciaa-offline.tar.gz"
STAGE_PARENT="$(mktemp -d)"
STAGE="$STAGE_PARENT/impact-analyzer"
mkdir -p "$STAGE/wheelhouse"

OS="$(uname -s)"; ARCH="$(uname -m)"
echo "▶ Builder platform: $OS/$ARCH   Python: $(python3 --version 2>&1)"
echo "▶ Requirements:     $REQ_FILE"
if [[ "$OS" != "Linux" ]]; then
  echo "⚠  You are NOT on Linux. The wheels downloaded here will NOT run on the"
  echo "   Linux target. Run this on a Linux host matching the target's arch +"
  echo "   Python version, or the install will fail with binary-incompatibility."
  read -r -p "   Continue anyway? [y/N] " ans; [[ "$ans" == "y" ]] || exit 1
fi

# 1. Vendor the Python wheels (+ pip/setuptools/wheel so the target can bootstrap)
echo "▶ Downloading wheels into wheelhouse/ …"
python3 -m pip download -r "$REQ_FILE" -d "$STAGE/wheelhouse"
python3 -m pip download pip setuptools wheel -d "$STAGE/wheelhouse"

# 2. Build the frontend (so the air-gapped box needs no Node)
if [[ -f frontend/package.json ]]; then
  if command -v npm >/dev/null 2>&1; then
    echo "▶ Building frontend (npm ci && npm run build) …"
    ( cd frontend && npm ci && npm run build )
  elif [[ -f frontend/dist/index.html ]]; then
    echo "▶ npm not found — reusing existing frontend/dist."
  else
    echo "✗ npm not found and frontend/dist is missing. Build the UI first"
    echo "  (cd frontend && npm run build) then re-run."; exit 1
  fi
fi

# 3. Stage the source (no venv, caches, git, node_modules, or local runtime data)
echo "▶ Staging source …"
rsync -a \
  --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
  --exclude='node_modules' --exclude='data' --exclude='logs' \
  --exclude='ciaa-offline.tar.gz' \
  ./ "$STAGE/"

# record which requirements file the offline installer should use
echo "$REQ_FILE" > "$STAGE/.offline_req"

# 4. Package
echo "▶ Packaging $OUT …"
tar -C "$STAGE_PARENT" -czf "$OUT" impact-analyzer
rm -rf "$STAGE_PARENT"

echo ""
echo "────────────────────────────────────────────────────────────────"
echo "✅ Offline bundle: $OUT  ($(du -h "$OUT" | cut -f1))"
echo ""
echo "On the air-gapped Linux box:"
echo "  tar xzf ciaa-offline.tar.gz && cd impact-analyzer"
echo "  sudo ./deploy/install-vm.sh --offline"
echo "  (requires python3 3.11+, git already present on the box)"
echo "────────────────────────────────────────────────────────────────"
