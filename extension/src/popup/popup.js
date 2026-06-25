// 丹秘 Browser Bridge Popup — v0.8.0

import { DISCOVERY_URL, HOMEPAGE_URL } from "../config.js";

const $ = (id) => document.getElementById(id);
let _refreshTimer = null;
let _prevState = null;

const BTN_TIPS = {
  pause: "暂停后 AI 发来的命令会被拒绝，但连接保持",
  resume: "恢复后 AI 可以继续控制浏览器",
  reconnect: "强制断开当前连接并重新建立",
  disconnect: "清除配对信息，需要重新输入配对码",
};

// 读 BOS 锚点拿当前 server_url（带 cache-buster 防多日缓存）。失败返回 null。
async function fetchDiscoveredServer() {
  if (!DISCOVERY_URL) return null;  // discovery is optional (self-host: manual entry)
  try {
    const r = await fetch(DISCOVERY_URL + "?t=" + Date.now(), { cache: "no-store" });
    if (!r.ok) return null;
    const d = await r.json();
    return (d && d.server_url) ? String(d.server_url).replace(/\/$/, "") : null;
  } catch (_) {
    return null;
  }
}

// 配对视图：自动把 server 地址填进输入框（用户只需填配对码）。
// 优先级：上次用过的地址(localStorage) → BOS 锚点。用户手填始终可覆盖。
async function autofillServer() {
  const input = $("input-server");
  if (!input || input.value.trim()) return;
  let last = null;
  try { last = localStorage.getItem("bb_last_server"); } catch (_) {}
  if (last) { input.value = last; }
  const discovered = await fetchDiscoveredServer();
  if (discovered && !input.value.trim()) input.value = discovered;
  else if (discovered && !last) input.value = discovered;
}

async function init() {
  const status = await sendMsg({ type: "get_status" });
  render(status);
  startRefresh();
  bindEvents();
  checkUpgrade();
  checkGuide(status);
  if (!status || !status.hasToken) autofillServer();
}

async function checkUpgrade() {
  const { update_info, update_dismissed_for } = await chrome.storage.local.get(
    ["update_info", "update_dismissed_for"]
  );
  const banner = $("upgrade-banner");

  if (!update_info || (!update_info.required && !update_info.version)) {
    banner.classList.add("hidden");
    return;
  }

  // Soft (suggested) updates can be dismissed per-version; required ones can't.
  if (!update_info.required && update_dismissed_for === update_info.version) {
    banner.classList.add("hidden");
    return;
  }

  const cur = update_info.current || "?";
  const next = update_info.version || "";
  const cmd = update_info.install_command || "";

  if (update_info.required) {
    banner.className = "update-banner warn";
    $("upgrade-title").textContent = "⚠ 必须升级";
    $("upgrade-ver").innerHTML =
      `当前 <span class="v-old">v${cur}</span>` +
      (update_info.min_version
        ? ` <span class="v-arrow">·</span> 需要 <span class="v-new">v${update_info.min_version}+</span>`
        : "");
    $("upgrade-close").classList.add("hidden");
  } else {
    banner.className = "update-banner info";
    $("upgrade-title").textContent = "新版本可用";
    $("upgrade-ver").innerHTML =
      `<span class="v-old">v${cur}</span> <span class="v-arrow">→</span> <span class="v-new">v${next}</span>`;
    $("upgrade-close").classList.remove("hidden");
  }

  // Wire up the copy command. Show a shortened label but copy the full command.
  const cmdEl = $("upgrade-cmd");
  const copyBox = $("upgrade-copy");
  if (cmd) {
    copyBox.dataset.cmd = cmd;
    cmdEl.textContent = cmd.replace(/(https?:\/\/[^/]+)\/static\//, "$1/.../");
    copyBox.style.display = "";
  } else {
    copyBox.style.display = "none";
  }

  banner.classList.remove("hidden");
}

async function checkGuide(status) {
  if (!status || !status.hasToken || status.state !== "connected") return;
  const { bb_onboarded } = await chrome.storage.local.get(["bb_onboarded"]);
  if (!bb_onboarded) {
    $("guide").classList.remove("hidden");
  }
}

function render(s) {
  if (!s) return;

  const dot = $("status-dot");
  const label = $("status-label");
  const pill = $("pill");
  $("footer-ver").textContent = `ext v${s.version || "?"} · server v${s.serverVersion || "—"}`;

  const state = s.paused ? "paused" : s.state;
  dot.className = "status-dot " + state;
  pill.className = "status-pill" + (state === "connected" ? " connected" : "");

  if (!s.hasToken) {
    label.textContent = "未连接";
    showView("pairing");
    _prevState = state;
    return;
  }

  const labels = { connected: "已连接", connecting: "连接中", disconnected: "已断开", paused: "已暂停" };
  label.textContent = labels[state] || state;
  showView("connected");

  // Info
  $("info-server").textContent = truncate(s.serverUrl || "—", 24);
  $("info-device").textContent = s.deviceId ? s.deviceId.slice(0, 8) : "—";
  $("info-uptime").textContent = s.connectedAt ? formatDuration(Date.now() - s.connectedAt) : "—";

  if (s.lastHeartbeat) {
    const ago = formatAgo(s.lastHeartbeat);
    const lat = s.pingLatency != null && s.pingLatency >= 0 ? ` (${s.pingLatency}ms)` : "";
    $("info-heartbeat").textContent = ago + lat;
  } else {
    $("info-heartbeat").textContent = "等待中…";
  }

  $("info-sessions").textContent = s.activeSessions || "0";
  $("info-cmds").textContent = s.cmdCount || "0";

  const netEl = $("info-network");
  if (s.networkActive) {
    netEl.textContent = "抓包中";
    netEl.className = "card-value warn";
  } else {
    netEl.textContent = "未启动";
    netEl.className = "card-value";
  }

  $("btn-pause").textContent = s.paused ? "恢复" : "暂停";

  // Pause visual state
  const overlay = $("pause-overlay");
  const mainContent = $("main-content");
  const app = $("app");
  if (s.paused) {
    overlay.classList.remove("hidden");
    mainContent.classList.add("paused-content");
    app.classList.add("app-paused");
  } else {
    overlay.classList.add("hidden");
    mainContent.classList.remove("paused-content");
    app.classList.remove("app-paused");
  }

  renderHistory(s.cmdHistory || []);
  _prevState = state;
}

let _lastHistoryKey = "";

function renderHistory(history) {
  const list = $("history-list");
  const empty = $("history-empty");
  if (!history || history.length === 0) {
    empty.classList.remove("hidden");
    list.innerHTML = "";
    _lastHistoryKey = "";
    return;
  }

  const items = history.slice(0, 10);
  const key = items.map(h => h.cmd + h.ts + (h.rejected || "")).join("|");
  if (key === _lastHistoryKey) return;

  const isFirstRender = _lastHistoryKey === "";
  _lastHistoryKey = key;
  empty.classList.add("hidden");

  list.innerHTML = items.map((h, i) => {
    const newCls = !isFirstRender && i === 0 ? ' new-item' : '';
    const rejCls = h.rejected ? ' rejected' : '';
    const tag = h.rejected === "paused" ? '<span class="history-tag">已拦截</span>' : '';
    return `<li class="${(newCls + rejCls).trim()}"><span class="history-cmd">${h.cmd}</span>${tag}<span class="history-meta">${h.session ? h.session + " · " : ""}${formatAgo(h.ts)}</span></li>`;
  }).join("");
}

function showView(name) {
  $("view-connected").classList.toggle("hidden", name !== "connected");
  $("view-pairing").classList.toggle("hidden", name !== "pairing");
}

function showTip(text) {
  const el = $("btn-tip");
  el.textContent = text;
  el.classList.add("show");
}
function hideTip() { $("btn-tip").classList.remove("show"); }

function bindEvents() {
  // 点 logo / 品牌名 → 打开官网
  const brand = $("brand");
  if (brand) {
    const openHome = () => chrome.tabs.create({ url: HOMEPAGE_URL });
    brand.style.cursor = "pointer";
    brand.addEventListener("click", openHome);
    brand.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openHome(); }
    });
  }

  // Pair
  $("btn-connect").addEventListener("click", async () => {
    const serverUrl = $("input-server").value.trim();
    const code = $("input-code").value.trim().toUpperCase();
    const errEl = $("pair-error");
    errEl.classList.add("hidden");

    if (!serverUrl || !code) {
      showError("请填写服务器地址和配对码");
      return;
    }

    $("btn-connect").disabled = true;
    $("btn-connect").textContent = "连接中...";
    const res = await sendMsg({ type: "pair", serverUrl, pairingCode: code });
    $("btn-connect").disabled = false;
    $("btn-connect").textContent = "连接";

    if (res && res.success) {
      try { localStorage.setItem("bb_last_server", serverUrl.replace(/\/$/, "")); } catch (_) {}
      const status = await sendMsg({ type: "get_status" });
      render(status);
      checkGuide(status);
    } else {
      showError((res && res.error) || "连接失败，请检查地址和配对码");
    }
  });

  // Pause
  $("btn-pause").addEventListener("click", async () => {
    const status = await sendMsg({ type: "get_status" });
    await sendMsg({ type: "set_paused", paused: !status.paused });
    render(await sendMsg({ type: "get_status" }));
  });
  $("btn-pause").addEventListener("mouseenter", () => {
    const text = $("btn-pause").textContent === "恢复" ? BTN_TIPS.resume : BTN_TIPS.pause;
    showTip(text);
  });
  $("btn-pause").addEventListener("mouseleave", hideTip);

  // Reconnect
  $("btn-reconnect").addEventListener("click", async () => {
    await sendMsg({ type: "reconnect" });
    setTimeout(async () => render(await sendMsg({ type: "get_status" })), 1500);
  });
  $("btn-reconnect").addEventListener("mouseenter", () => showTip(BTN_TIPS.reconnect));
  $("btn-reconnect").addEventListener("mouseleave", hideTip);

  // Disconnect
  $("btn-disconnect").addEventListener("click", async () => {
    await sendMsg({ type: "disconnect" });
    render(await sendMsg({ type: "get_status" }));
  });
  $("btn-disconnect").addEventListener("mouseenter", () => showTip(BTN_TIPS.disconnect));
  $("btn-disconnect").addEventListener("mouseleave", hideTip);

  // Enter on code input
  $("input-code").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("btn-connect").click();
  });

  // Guide dismiss
  $("guide-dismiss").addEventListener("click", () => {
    $("guide").classList.add("hidden");
    chrome.storage.local.set({ bb_onboarded: true });
  });

  // Resume from pause overlay
  $("btn-resume").addEventListener("click", async () => {
    await sendMsg({ type: "set_paused", paused: false });
    render(await sendMsg({ type: "get_status" }));
  });

  // Copy install command from the update banner
  $("upgrade-copy").addEventListener("click", () => {
    const box = $("upgrade-copy");
    const cmd = box.dataset.cmd;
    if (!cmd) return;
    navigator.clipboard.writeText(cmd).then(() => {
      box.classList.add("copied");
      const code = $("upgrade-cmd");
      const ic = box.querySelector(".copy-ic");
      const orig = code.textContent;
      code.textContent = "✓ 已复制到剪贴板";
      ic.textContent = "✓";
      setTimeout(() => {
        box.classList.remove("copied");
        code.textContent = orig;
        ic.textContent = "⎘";
      }, 1500);
    });
  });

  // Dismiss a soft update (per-version; re-appears on the next release)
  $("upgrade-close").addEventListener("click", async (e) => {
    e.stopPropagation();
    const { update_info } = await chrome.storage.local.get(["update_info"]);
    if (update_info && update_info.version) {
      await chrome.storage.local.set({ update_dismissed_for: update_info.version });
    }
    $("upgrade-banner").classList.add("hidden");
  });

  // Manual "检查更新" button
  $("btn-check").addEventListener("click", async () => {
    const btn = $("btn-check");
    if (btn.disabled) return;
    const label = btn.querySelector(".check-label");
    btn.disabled = true;
    btn.classList.remove("success");
    btn.classList.add("checking");
    label.textContent = "检查中…";

    await sendMsg({ type: "refresh_update" });
    await checkUpgrade();

    const { update_info, update_dismissed_for } = await chrome.storage.local.get(
      ["update_info", "update_dismissed_for"]
    );
    const hasUpdate =
      update_info &&
      (update_info.required ||
        (update_info.version && update_dismissed_for !== update_info.version));

    btn.classList.remove("checking");
    if (hasUpdate) {
      // Banner is now visible; reset the button to idle.
      btn.disabled = false;
      btn.querySelector(".check-ic").textContent = "↻";
      label.textContent = "检查更新";
    } else {
      btn.classList.add("success");
      btn.querySelector(".check-ic").textContent = "✓";
      label.textContent = "已是最新";
      setTimeout(() => {
        btn.classList.remove("success");
        btn.disabled = false;
        btn.querySelector(".check-ic").textContent = "↻";
        label.textContent = "检查更新";
      }, 2000);
    }
  });
}

function showError(text) {
  const errEl = $("pair-error");
  errEl.textContent = text;
  errEl.classList.remove("hidden");
}

function startRefresh() {
  if (_refreshTimer) clearInterval(_refreshTimer);
  _refreshTimer = setInterval(async () => render(await sendMsg({ type: "get_status" })), 2000);
}

function sendMsg(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (response) => resolve(response));
  });
}

function formatDuration(ms) {
  const s = Math.floor(ms / 1000);
  if (s < 60) return s + "秒";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "分" + (s % 60) + "秒";
  const h = Math.floor(m / 60);
  return h + "时" + (m % 60) + "分";
}

function formatAgo(ts) {
  const ago = Date.now() - ts;
  if (ago < 1000) return "刚刚";
  if (ago < 60000) return Math.floor(ago / 1000) + "秒前";
  if (ago < 3600000) return Math.floor(ago / 60000) + "分钟前";
  return Math.floor(ago / 3600000) + "小时前";
}

function truncate(str, max) {
  return str.length > max ? str.slice(0, max) + "…" : str;
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "connection_state") {
    sendMsg({ type: "get_status" }).then(render);
  }
});

init();
