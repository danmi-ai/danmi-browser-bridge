#!/usr/bin/env bash
set -euo pipefail

# --- Platform check ---
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: This script only supports macOS." >&2
  exit 1
fi

# --- Args ---
USERNAME="${1:-}"
if [[ -z "$USERNAME" ]]; then
  echo "Usage: install-client.sh <username>" >&2
  exit 1
fi

# --- Config ---
# Point these at your deployment. Override via env without editing the script.
SERVER="${BB_SERVER:-http://127.0.0.1:8403}"
# URL to the packaged extension zip (object storage / CDN / server /static).
EXT_ZIP_URL="${BB_EXT_ZIP_URL:-${SERVER}/static/danmi-browser-bridge-extension.zip}"
BB_ADMIN_TOKEN="${BB_ADMIN_TOKEN:-}"

if [[ -z "$BB_ADMIN_TOKEN" ]]; then
  echo "WARNING: BB_ADMIN_TOKEN not set; onboard request may fail." >&2
fi

# --- Colors ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
RESET='\033[0m'

# --- Onboard ---
echo "Onboarding user: ${USERNAME} ..."
RESPONSE=$(curl -sf -X POST "${SERVER}/api/v1/onboard/${USERNAME}" \
  -H "Authorization: Bearer ${BB_ADMIN_TOKEN}" \
  -H "Content-Type: application/json") || {
  echo "ERROR: Onboard request failed." >&2
  exit 1
}

# Parse response
SERVER_URL=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['server_url'])")
PAIRING_CODE=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['pairing_code'])")
EXPIRES_AT=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['expires_at'])")

# --- Download extension ---
echo "Downloading extension ..."
curl -sfL "${EXT_ZIP_URL}" -o /tmp/bb-ext.zip

EXT_DIR="$HOME/Downloads/danmi-browser-bridge-ext"
rm -rf "$EXT_DIR"
unzip -q /tmp/bb-ext.zip -d "$EXT_DIR"

# --- Open Chrome extensions page ---
open "chrome://extensions"

# --- Instructions ---
echo ""
echo -e "${BOLD}=== Browser Bridge Setup ===${RESET}"
echo ""
echo -e "${GREEN}1.${RESET} Enable ${BOLD}Developer Mode${RESET} (top-right toggle)"
echo -e "${GREEN}2.${RESET} Click ${BOLD}Load unpacked${RESET} -> select: ${YELLOW}${EXT_DIR}${RESET}"
echo -e "${GREEN}3.${RESET} Click the extension icon in the toolbar"
echo ""
echo -e "   ${BOLD}Server URL:${RESET}    ${YELLOW}${SERVER_URL}${RESET}"
echo -e "   ${BOLD}Pairing Code:${RESET}  ${YELLOW}${PAIRING_CODE}${RESET}  (expires at ${EXPIRES_AT})"
echo ""
echo -e "${GREEN}4.${RESET} Click ${BOLD}Connect${RESET}"
echo ""
echo -e "${GREEN}Done! The extension will connect to the server automatically.${RESET}"
