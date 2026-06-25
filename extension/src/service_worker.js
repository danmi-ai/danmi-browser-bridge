/**
 * Browser Bridge — Service Worker (v0.7.0)
 *
 * 控制面：配对 / WS 鉴权 (→ /ws/device) / 暂停 / 升级检测。
 * 命令执行：commander.js（chrome.scripting + chrome.tabs）。
 * 已移除：CDP relay / chrome.debugger / offscreen / alarms。
 */

import { setSender as commanderSetSender, clearSender as commanderClearSender, dispatch as commanderDispatch } from "./commander.js";
import { cleanupStaleGroups } from "./connectedTabGroup.js";
import { DISCOVERY_URL } from "./config.js";

const BACKOFF_INITIAL = 1000;
const BACKOFF_MAX = 60000;
const BACKOFF_MULTIPLIER = 2;
const HEARTBEAT_CHECK_INTERVAL = 10000;
const HEARTBEAT_DEAD_THRESHOLD = 35000;
const UPDATE_POLL_INTERVAL = 30 * 60 * 1000; // re-check /extension/latest every 30 min
// 服务发现锚点（从 config.js 注入）：连续重连失败后从这里查当前 server 地址（IP 漂移自愈）。
const REDISCOVER_AFTER_FAILS = 3; // 连续失败这么多次后去查锚点

class BridgeConnection {
  constructor() {
    this.ws = null;
    this.serverUrl = null;
    this.deviceToken = null;
    this.deviceId = null;
    this.serverLatestVersion = null;
    this.backoff = BACKOFF_INITIAL;
    this.intentionalClose = false;
    this.state = "disconnected"; // disconnected | connecting | connected
    this.paused = false;
    this._reconnectTimer = null;
    this._keepaliveTimer = null;
    this._heartbeatChecker = null;
    this._lastServerPing = null;
    this._connectedAt = null;
    this._lastHeartbeat = null;
    this._failedReconnects = 0;
    this._lastCmd = null;
    this._cmdCount = 0;
    this._cmdHistory = [];       // last 30 commands
    this._activeSessions = 0;
    this._networkActive = false;
    this._pingLatency = null;
    this._serverVersion = null;
    this._badgeClearTimer = null;    // last ping-pong round trip
  }

  async init() {
    const s = await chrome.storage.local.get([
      "serverUrl", "deviceToken", "deviceId", "paused",
    ]);
    this.serverUrl = s.serverUrl || null;
    this.deviceToken = s.deviceToken || null;
    this.deviceId = s.deviceId || null;
    this.paused = !!s.paused;

    if (this.serverUrl && this.deviceToken && this.deviceId) this.connect();
    checkForUpdate(this).catch(() => {});
    this._startUpdatePoll();
  }

  _startUpdatePoll() {
    if (this._updatePollTimer) clearInterval(this._updatePollTimer);
    // MV3 service workers can be torn down, but while alive this keeps the
    // update banner fresh without waiting for a reconnect. The popup also
    // triggers an immediate check via the "refresh_update" message.
    this._updatePollTimer = setInterval(() => {
      checkForUpdate(this).catch(() => {});
    }, UPDATE_POLL_INTERVAL);
  }

  connect() {
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return;
    if (!this.serverUrl || !this.deviceToken || !this.deviceId) {
      this.state = "disconnected";
      this._broadcastState();
      return;
    }
    if (this._reconnectTimer) { clearTimeout(this._reconnectTimer); this._reconnectTimer = null; }

    this.state = "connecting";
    this._broadcastState();

    const wsBase = this.serverUrl.replace(/^https/, "wss").replace(/^http/, "ws");
    const url = `${wsBase}/api/v1/ws/device`;

    let ws;
    try { ws = new WebSocket(url); }
    catch (e) {
      console.warn("[sw] WebSocket() threw", e);
      this._scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      // Post-handshake auth: send auth frame with device token + meta
      const authFrame = JSON.stringify({
        type: "auth",
        token: this.deviceToken,
        meta: this._collectMeta(),
      });
      ws.send(authFrame);

      this.state = "connected";
      this.backoff = BACKOFF_INITIAL;
      this._failedReconnects = 0;
      this._connectedAt = Date.now();
      this._lastServerPing = Date.now();
      this._lastHeartbeat = Date.now();
      this._broadcastState();
      this._startKeepalive();
      this._startHeartbeatCheck();

      // Sync paused state to server
      this._sendPausedState();

      // commander uses this sender to return results to server
      const senderFn = (msg) => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          try { ws.send(JSON.stringify(msg)); }
          catch (e) { console.warn("[sw] send threw", e); }
        }
      };
      commanderSetSender(senderFn);
    };

    ws.onmessage = (event) => {
      this._handleMessage(event.data);
    };

    ws.onclose = (event) => {
      this.ws = null;
      this.state = "disconnected";
      this._connectedAt = null;
      this._lastServerPing = null;
      this._stopKeepalive();
      this._stopHeartbeatCheck();
      this._broadcastState();
      commanderClearSender();

      if (event.code === 4001) {
        // Server revoked token
        chrome.storage.local.remove(["deviceToken"]);
        this.deviceToken = null;
        return;
      }
      if (!this.intentionalClose) this._scheduleReconnect();
      else this.intentionalClose = false;
    };

    ws.onerror = () => {};
  }

  disconnect() {
    this.intentionalClose = true;
    this._stopKeepalive();
    if (this._reconnectTimer) { clearTimeout(this._reconnectTimer); this._reconnectTimer = null; }
    if (this.ws) {
      try { this.ws.close(1000, "user_disconnect"); } catch (e) {}
      this.ws = null;
    }
    this.state = "disconnected";
    this._connectedAt = null;
    this._broadcastState();
    chrome.storage.local.remove(["serverUrl", "deviceToken", "deviceId"]);
    this.serverUrl = null;
    this.deviceToken = null;
    this.deviceId = null;
  }

  _handleMessage(raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }

    switch (msg.type) {
      case "auth_ok":
        if (msg.device_id) this.deviceId = msg.device_id;
        this._fetchServerVersion();
        break;

      case "command":
        // Authoritative pause gate: even if the server fails to filter (bug,
        // stale paused mirror, or a client talking to the device WS directly),
        // the extension is the source of truth for paused state and refuses to
        // execute. Must reply with an error frame carrying msg_id, otherwise
        // the server's send_and_wait future hangs until timeout (504).
        if (this.paused) {
          this._send({
            type: "error",
            msg_id: msg.msg_id,
            message: "extension is paused by user",
            code: "PAUSED",
          });
          this._cmdHistory.unshift({
            cmd: msg.cmd, ts: Date.now(), session: msg.session || "", rejected: "paused",
          });
          if (this._cmdHistory.length > 30) this._cmdHistory.pop();
          break;
        }
        this._lastCmd = { cmd: msg.cmd, ts: Date.now() };
        this._cmdCount++;
        this._cmdHistory.unshift({ cmd: msg.cmd, ts: Date.now(), session: msg.session || "" });
        if (this._cmdHistory.length > 30) this._cmdHistory.pop();
        if (msg.cmd === "network") {
          const sub = (msg.params || {}).cmd;
          if (sub === "start") this._networkActive = true;
          else if (sub === "stop") this._networkActive = false;
        }
        this._setBadge("...", "#4E6EF2");
        commanderDispatch(msg).then(() => {
          this._onCommandDone();
        });
        break;

      case "ping":
        this._lastServerPing = Date.now();
        this._lastHeartbeat = Date.now();
        this._pingLatency = msg.ts ? Math.round(Date.now() / 1000 - msg.ts) * 1000 : null;
        this._sendPong(msg.ts);
        chrome.storage.session.set({ _hb: Date.now() });
        break;

      case "force_upgrade":
        _handleForceUpgrade(msg, this).catch(() => {});
        break;

      case "session_create":
        this._activeSessions++;
        this._send({ type: "ack", msg_id: msg.msg_id });
        break;

      case "session_close":
        this._activeSessions = Math.max(0, this._activeSessions - 1);
        this._send({ type: "ack", msg_id: msg.msg_id });
        break;

      case "shutdown_notice":
        break;
    }
  }

  _collectMeta() {
    const m = chrome.runtime.getManifest();
    const scr = (typeof screen !== "undefined") ? { w: screen.width, h: screen.height } : { w: 0, h: 0 };
    const nav = (typeof navigator !== "undefined") ? navigator : {};
    return {
      ext_version: m.version, ext_name: m.name,
      user_agent: nav.userAgent || "", platform: nav.platform || "",
      languages: Array.from(nav.languages || []), screen: scr,
    };
  }

  _sendPong(ts) { this._send({ type: "pong", ts }); }

  _send(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  _scheduleReconnect() {
    if (this._reconnectTimer) return;
    this._reconnectTimer = setTimeout(async () => {
      this._reconnectTimer = null;
      this._failedReconnects++;
      // 连续失败 → server 可能换了 IP，去锚点查最新地址（自愈）
      if (this._failedReconnects % REDISCOVER_AFTER_FAILS === 0) {
        await this._rediscoverServer();
      }
      this.connect();
    }, this.backoff);
    this.backoff = Math.min(this.backoff * BACKOFF_MULTIPLIER, BACKOFF_MAX);
  }

  // 从服务发现锚点查当前 server_url；变了就更新并落盘，下次 connect 用新地址。
  async _rediscoverServer() {
    if (!DISCOVERY_URL) return;  // discovery is optional (self-host: manual entry)
    try {
      const r = await fetch(DISCOVERY_URL + "?t=" + Date.now(), { cache: "no-store" });
      if (!r.ok) return;
      const d = await r.json();
      const url = d && d.server_url ? String(d.server_url).replace(/\/$/, "") : null;
      if (url && url !== this.serverUrl) {
        console.warn("[sw] server moved via discovery:", this.serverUrl, "->", url);
        this.serverUrl = url;
        await chrome.storage.local.set({ serverUrl: url });
      }
    } catch (_) { /* 锚点不可达就维持原地址继续退避 */ }
  }

  _broadcastState() {
    chrome.runtime.sendMessage({
      type: "connection_state", state: this.state, serverUrl: this.serverUrl,
    }).catch(() => {});
  }

  async pair(serverUrl, pairingCode, deviceName) {
    const resp = await fetch(`${serverUrl}/api/v1/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pairing_code: pairingCode, device_name: deviceName }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: "Unknown error" }));
      const e = new Error(err.detail || `HTTP ${resp.status}`);
      e.httpStatus = resp.status;
      throw e;
    }
    const data = await resp.json();
    this.serverUrl = serverUrl;
    this.deviceToken = data.device_token;
    this.deviceId = data.device_id;
    await chrome.storage.local.set({
      serverUrl, deviceToken: data.device_token, deviceId: data.device_id,
    });
    this.connect();
    return data;
  }

  getStatus() {
    return {
      state: this.state,
      serverUrl: this.serverUrl,
      hasToken: !!this.deviceToken,
      deviceId: this.deviceId,
      paused: this.paused,
      serverLatestVersion: this.serverLatestVersion,
      connectedAt: this._connectedAt,
      lastHeartbeat: this._lastHeartbeat,
      lastCmd: this._lastCmd,
      cmdCount: this._cmdCount,
      cmdHistory: this._cmdHistory,
      activeSessions: this._activeSessions,
      networkActive: this._networkActive,
      pingLatency: this._pingLatency,
      serverVersion: this._serverVersion,
      version: chrome.runtime.getManifest().version,
    };
  }

  async setPaused(paused) {
    this.paused = !!paused;
    await chrome.storage.local.set({ paused: this.paused });
    this._sendPausedState();
    this._broadcastState();
    if (this.paused) {
      this._setBadge("II", "#FF7D00");
    } else {
      this._setBadge("", "");
    }
  }

  _sendPausedState() {
    this._send({ type: "extension.paused_changed", paused: !!this.paused });
  }

  async _fetchServerVersion() {
    if (!this.serverUrl) return;
    try {
      const r = await fetch(`${this.serverUrl}/api/v1/health`, { cache: "no-store" });
      if (r.ok) {
        const data = await r.json();
        this._serverVersion = data.server_version || null;
      }
    } catch (_) {}
  }

  _startKeepalive() {
    this._stopKeepalive();
    this._keepaliveTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        chrome.storage.session.set({ _ka: Date.now() });
      }
    }, 20000);
  }

  _stopKeepalive() {
    if (this._keepaliveTimer) {
      clearInterval(this._keepaliveTimer);
      this._keepaliveTimer = null;
    }
  }

  _startHeartbeatCheck() {
    this._stopHeartbeatCheck();
    this._heartbeatChecker = setInterval(() => {
      if (!this._lastServerPing) return;
      const elapsed = Date.now() - this._lastServerPing;
      if (elapsed > HEARTBEAT_DEAD_THRESHOLD) {
        console.warn("[sw] heartbeat dead, no server ping for", elapsed, "ms — forcing reconnect");
        if (this.ws) {
          try { this.ws.close(4010, "heartbeat_dead"); } catch (e) {}
        }
      }
    }, HEARTBEAT_CHECK_INTERVAL);
  }

  _stopHeartbeatCheck() {
    if (this._heartbeatChecker) {
      clearInterval(this._heartbeatChecker);
      this._heartbeatChecker = null;
    }
  }

  _setBadge(text, color) {
    try {
      chrome.action.setBadgeText({ text: text || "" });
      if (color) chrome.action.setBadgeBackgroundColor({ color });
    } catch (_) {}
  }

  _onCommandDone() {
    if (this._badgeClearTimer) clearTimeout(this._badgeClearTimer);
    // Show count briefly
    if (this.paused) {
      this._setBadge("II", "#FF7D00");
    } else if (this._networkActive) {
      this._setBadge("REC", "#FF7D00");
    } else {
      const count = String(this._cmdCount);
      this._setBadge(count, "#4E6EF2");
      this._badgeClearTimer = setTimeout(() => {
        this._setBadge("", "");
        this._badgeClearTimer = null;
      }, 2000);
    }
  }
}

// --- Upgrade detection ---

async function _fetchLatestInfo(b) {
  if (!b || !b.serverUrl) return null;
  try {
    const r = await fetch(`${b.serverUrl}/api/v1/extension/latest`, { method: "GET", cache: "no-store" });
    if (!r.ok) return null;
    return await r.json();
  } catch (_) {
    return null;
  }
}

async function _handleForceUpgrade(msg, b) {
  // Server kicked us for being below min_version. Enrich the banner with the
  // install command from /extension/latest when reachable; otherwise still
  // surface the required-upgrade banner using the WS message fields.
  const info = await _fetchLatestInfo(b);
  try {
    await chrome.storage.local.set({
      update_info: {
        required: true,
        reason: msg.reason || "extension_too_old",
        version: (info && info.version) || "",
        min_version: msg.min_version || (info && info.min_version) || "",
        download_url: (info && info.download_url) || "",
        install_command: (info && info.install_command) || "",
        current: chrome.runtime.getManifest().version,
        detected_at: Date.now(),
      },
    });
  } catch (e) {}
}

async function checkForUpdate(b) {
  if (!b || !b.serverUrl) return;
  const cur = chrome.runtime.getManifest().version;
  const info = await _fetchLatestInfo(b);
  if (!info) return;
  const latest = info.version;
  if (!latest) return;
  b.serverLatestVersion = latest;

  if (_semverLt(cur, latest)) {
    // Don't clobber a required (force-upgrade) banner with a softer one.
    const { update_info: prev } = await chrome.storage.local.get(["update_info"]);
    if (prev && prev.required) return;
    await chrome.storage.local.set({
      update_info: {
        required: false,
        version: latest,
        min_version: info.min_version || "",
        download_url: info.download_url || "",
        install_command: info.install_command || "",
        current: cur,
        detected_at: Date.now(),
      },
    });
  } else {
    const { update_info } = await chrome.storage.local.get(["update_info"]);
    if (update_info && !update_info.required) {
      await chrome.storage.local.remove(["update_info"]);
    }
  }
}

function _semverLt(a, b) {
  const pa = String(a || "0").split(".").map(Number);
  const pb = String(b || "0").split(".").map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0, y = pb[i] || 0;
    if (x !== y) return x < y;
  }
  return false;
}

// --- Entry point ---

const bridge = new BridgeConnection();
bridge.init();
cleanupStaleGroups().catch(() => {});

chrome.runtime.onStartup.addListener(() => {
  if (bridge.serverUrl && bridge.deviceToken && bridge.deviceId) bridge.connect();
});
chrome.runtime.onInstalled.addListener(() => {
  if (bridge.serverUrl && bridge.deviceToken && bridge.deviceId) bridge.connect();
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !("paused" in changes)) return;
  const next = !!changes.paused.newValue;
  if (bridge.paused === next) return;
  bridge.paused = next;
  bridge._sendPausedState();
  bridge._broadcastState();
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  switch (msg.type) {
    case "get_status":
      sendResponse(bridge.getStatus());
      return false;
    case "pair":
      bridge.pair(msg.serverUrl, msg.pairingCode, msg.deviceName || "Chrome Extension")
        .then((data) => sendResponse({ success: true, data }))
        .catch((err) => sendResponse({ success: false, error: err.message, httpStatus: err.httpStatus || 0 }));
      return true;
    case "disconnect":
      bridge.disconnect();
      sendResponse({ success: true });
      return false;
    case "reconnect":
      // Force close existing connection then reconnect
      if (bridge.ws) {
        try { bridge.ws.close(1000, "user_reconnect"); } catch (e) {}
        bridge.ws = null;
      }
      bridge.state = "disconnected";
      bridge._stopKeepalive();
      bridge._stopHeartbeatCheck();
      bridge.backoff = BACKOFF_INITIAL;
      setTimeout(() => bridge.connect(), 300);
      sendResponse({ success: true });
      return false;
    case "set_paused":
      bridge.setPaused(!!msg.paused).then(() => sendResponse({ success: true }));
      return true;
    case "refresh_update":
      checkForUpdate(bridge).then(() => sendResponse({ success: true }));
      return true;
    default:
      return false;
  }
});
