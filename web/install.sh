#!/usr/bin/env bash
set -euo pipefail

# 丹秘 Browser Bridge 扩展安装脚本（Mac）
#
# 从 GitHub Release 下载扩展包并引导加载。自托管的 server 地址在扩展弹窗里手填
# （开源版没有中心服务发现）。可用 env 覆盖下载地址 / 可选的服务发现锚点：
#   BB_EXT_ZIP_URL   扩展 zip 地址（默认 GitHub Release latest）
#   BB_DISCOVERY_URL 可选：你自建的 discovery.json，用于自动填 server 地址
#
#   curl -sL https://raw.githubusercontent.com/danmi-ai/danmi-browser-bridge/master/web/install.sh | bash

DISCOVERY="${BB_DISCOVERY_URL:-}"
ZIP_URL="${BB_EXT_ZIP_URL:-https://github.com/danmi-ai/danmi-browser-bridge/releases/latest/download/danmi-browser-bridge-extension.zip}"
EXT_DIR="$HOME/Downloads/danmi-browser-bridge-extension"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

cb() { date +%s; }  # cache-buster：BOS 默认多日缓存，加时间戳绕过

echo ""
echo -e "${BOLD}=== 丹秘 Browser Bridge 扩展安装 ===${RESET}"
echo ""

# —— 1. 下载扩展包（带 cache-buster）——
echo "正在下载扩展包…"
if ! curl -sfL "${ZIP_URL}?t=$(cb)" -o /tmp/bb-ext.zip; then
  echo -e "${YELLOW}下载失败：无法连接分发地址。请检查网络后重试。${RESET}"
  exit 1
fi

# —— 2. 解压 ——
rm -rf "$EXT_DIR"
mkdir -p "$EXT_DIR"
unzip -q /tmp/bb-ext.zip -d "$EXT_DIR"
rm -f /tmp/bb-ext.zip

# 若 zip 内是单层 extension/ 目录，拍平到 EXT_DIR 根
if [[ -d "$EXT_DIR/extension" && -f "$EXT_DIR/extension/manifest.json" ]]; then
  mv "$EXT_DIR/extension"/* "$EXT_DIR/" 2>/dev/null || true
  mv "$EXT_DIR/extension"/.* "$EXT_DIR/" 2>/dev/null || true
  rmdir "$EXT_DIR/extension" 2>/dev/null || true
fi

if [[ ! -f "$EXT_DIR/manifest.json" ]]; then
  echo -e "${YELLOW}错误：解压后未找到 manifest.json${RESET}"
  exit 1
fi
echo -e "${GREEN}已下载到：${RESET}$EXT_DIR"
echo ""
# —— 3. 可选：从服务发现锚点读 server 地址（自托管一般没有，跳过即可）——
SERVER=""
if [[ -n "$DISCOVERY" ]]; then
  DISC=$(curl -sfL "${DISCOVERY}?t=$(cb)" 2>/dev/null || true)
  if [[ -n "$DISC" ]]; then
    SERVER=$(printf '%s' "$DISC" | sed -n 's/.*"server_url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
  fi
fi

# —— 4. 打开 Chrome 扩展页 ——
open -a "Google Chrome" "chrome://extensions" 2>/dev/null || true

# —— 5. 指引 ——
echo -e "${BOLD}接下来：${RESET}"
echo ""
echo -e "  ${GREEN}1.${RESET} Chrome 打开 ${YELLOW}chrome://extensions${RESET}（已尝试自动打开）"
echo -e "  ${GREEN}2.${RESET} 打开右上角的 ${BOLD}开发者模式${RESET}"
echo ""
echo -e "     ${BOLD}首次安装：${RESET}点「加载已解压的扩展程序」，选择目录："
echo -e "     ${YELLOW}${EXT_DIR}${RESET}"
echo ""
echo -e "     ${BOLD}更新已装：${RESET}找到「丹秘 Browser Bridge」，点 ${BOLD}刷新 🔄${RESET}"
echo ""
echo -e "  ${GREEN}3.${RESET} 点工具栏的丹秘图标，填入 ${BOLD}配对码${RESET} 后连接"
if [[ -n "$SERVER" ]]; then
  echo -e "     服务器地址已自动获取：${YELLOW}${SERVER}${RESET}"
  echo -e "     ${DIM}（扩展弹窗会自动填好，一般无需手动输入）${RESET}"
else
  echo -e "     在弹窗的 ${BOLD}服务器地址${RESET} 里填你自托管的 server（如 ${YELLOW}http://your-host:8404${RESET}）"
fi
echo ""
echo -e "${BOLD}完成！${RESET}加载后弹窗显示绿色即表示连接成功。"
echo ""

