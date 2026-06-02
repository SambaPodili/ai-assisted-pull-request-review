#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# CIAA Impact Analyzer — bare-metal installer (NO Docker).
# Sets up a standalone Linux box: system deps, app user, virtualenv, systemd
# service. Idempotent — safe to re-run.
#
# Usage:
#   sudo ./deploy/install-vm.sh            # minimal install (SQLite + in-memory)
#   sudo ./deploy/install-vm.sh --full     # full deps (LangGraph, Chroma, Redis, Neo4j clients)
#
# Supports: RHEL/Fedora/Rocky/Alma (dnf|yum) and Debian/Ubuntu (apt).
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_USER="ciaa"
APP_DIR="/opt/impact-analyzer"
REQ_FILE="requirements-minimal.txt"
PYTHON_MIN="3.11"

[[ "${1:-}" == "--full" ]] && REQ_FILE="requirements.txt"

# ── Must be root ────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then echo "Please run with sudo/root."; exit 1; fi

# ── Resolve the source directory (where this repo currently sits) ─────────────
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "▶ Source: $SRC_DIR"
echo "▶ Target: $APP_DIR"
echo "▶ Requirements: $REQ_FILE"

# ── 1. System packages ────────────────────────────────────────────────────────
echo "▶ Installing system packages…"
if command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip python3-devel git gcc ripgrep || \
  dnf install -y python3 python3-pip python3-devel git gcc        # ripgrep optional
elif command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip python3-dev git build-essential ripgrep || \
  apt-get install -y python3 python3-venv python3-pip python3-dev git build-essential
else
  echo "Unsupported distro (need dnf or apt). Install python3.11+, git, gcc manually."; exit 1
fi

# ── 2. Verify Python version ──────────────────────────────────────────────────
PYVER="$(python3 -c 'import sys; print("%d.%d"%sys.version_info[:2])')"
echo "▶ Python $PYVER detected"
python3 - <<EOF || { echo "Python >= $PYTHON_MIN required"; exit 1; }
import sys
sys.exit(0 if sys.version_info >= tuple(int(x) for x in "$PYTHON_MIN".split(".")) else 1)
EOF

# ── 3. App user ────────────────────────────────────────────────────────────────
if ! id "$APP_USER" >/dev/null 2>&1; then
  echo "▶ Creating service user '$APP_USER'…"
  useradd -r -m -d "$APP_DIR" -s /sbin/nologin "$APP_USER"
fi

# ── 4. Copy source into place ───────────────────────────────────────────────────
echo "▶ Syncing application to $APP_DIR…"
mkdir -p "$APP_DIR"
# Copy everything except the venv and local data
rsync -a --exclude='.venv' --exclude='__pycache__' --exclude='data' \
      --exclude='logs' "$SRC_DIR"/ "$APP_DIR"/ 2>/dev/null || \
  cp -r "$SRC_DIR"/. "$APP_DIR"/
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ── 5. Virtualenv + dependencies ─────────────────────────────────────────────────
echo "▶ Creating virtualenv and installing $REQ_FILE…"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/$REQ_FILE"

# ── 6. Environment file ─────────────────────────────────────────────────────────
if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "▶ Creating .env from template…"
  sudo -u "$APP_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "  ⚠ EDIT $APP_DIR/.env — set ANTHROPIC_API_KEY, GITHUB_TOKEN, API_KEYS, SKIP_AUTH=false"
fi

# ── 7. systemd service ───────────────────────────────────────────────────────────
echo "▶ Installing systemd service…"
cp "$APP_DIR/deploy/impact-analyzer.service" /etc/systemd/system/impact-analyzer.service
systemctl daemon-reload
systemctl enable impact-analyzer

echo ""
echo "────────────────────────────────────────────────────────────────"
echo "✅ Install complete."
echo ""
echo "Next steps:"
echo "  1. Edit credentials:   sudo -u $APP_USER vi $APP_DIR/.env"
echo "  2. Start the service:  sudo systemctl start impact-analyzer"
echo "  3. Check status:       sudo systemctl status impact-analyzer"
echo "  4. Tail logs:          sudo journalctl -u impact-analyzer -f"
echo "  5. Verify:             curl -s http://localhost:8080/live"
echo ""
echo "  Open firewall (if needed):"
echo "    firewall-cmd --add-port=8080/tcp --permanent && firewall-cmd --reload"
echo "────────────────────────────────────────────────────────────────"
