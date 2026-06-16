// commander.js — high-level command executor (non-CDP, chrome.scripting + chrome.tabs)
// Receives "command" type messages from service_worker, executes browser actions
// using chrome.scripting.executeScript (page ops) and chrome.tabs (tab management).
// Loaded as ES module from service_worker.js.

import { addTabToSession as groupAddTab, closeSessionGroup } from "./connectedTabGroup.js";

// ---------------------------------------------------------------------------
// Logging (same pattern as relay.js)
// ---------------------------------------------------------------------------
let _verbose = false;
chrome.storage?.local?.get?.("bb_debug", (s) => { _verbose = !!(s && s.bb_debug); });

function _ts() {
  const d = new Date();
  return d.toISOString().slice(11, 23);
}
function logI(...args) { console.log("[commander]", _ts(), ...args); }
function logW(...args) { console.warn("[commander]", _ts(), ...args); }
function logE(...args) { console.error("[commander]", _ts(), ...args); }
function logD(...args) { if (_verbose) console.debug("[commander]", _ts(), ...args); }

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let _sender = null;

// session -> { currentTabId: number|null, tabs: Set<number> }
const sessionTabs = new Map();

// ---------------------------------------------------------------------------
// Sender injection (same pattern as relay.js)
// ---------------------------------------------------------------------------
export function setSender(fn) {
  _sender = fn;
}

export function clearSender() {
  _sender = null;
}

function send(msg) {
  if (_sender == null) {
    logW("send dropped, no sender:", msg && msg.type);
    return;
  }
  logD("send", msg && msg.type, msg && msg.msg_id != null ? "msg_id=" + msg.msg_id : "");
  try { _sender(msg); }
  catch (err) { logE("sender threw:", err); }
}

// ---------------------------------------------------------------------------
// Tab removal listener — keep sessionTabs consistent when tabs close externally
// ---------------------------------------------------------------------------
chrome.tabs.onRemoved.addListener((tabId) => {
  for (const [session, state] of sessionTabs.entries()) {
    if (state.tabs.has(tabId)) {
      state.tabs.delete(tabId);
      if (state.currentTabId === tabId) {
        state.currentTabId = null;
      }
      logI("tab removed externally, tabId=", tabId, "session=", session);
      // Clean up empty sessions
      if (state.tabs.size === 0) {
        sessionTabs.delete(session);
      }
      break;
    }
  }
});

// ---------------------------------------------------------------------------
// Session state helpers
// ---------------------------------------------------------------------------
function getSessionState(session) {
  if (!sessionTabs.has(session)) {
    sessionTabs.set(session, { currentTabId: null, tabs: new Set() });
  }
  return sessionTabs.get(session);
}

// ---------------------------------------------------------------------------
// dispatch — main entry point from service_worker
// ---------------------------------------------------------------------------
export async function dispatch(msg) {
  if (!msg || msg.type !== "command") {
    logW("dispatch: ignored non-command message, type=", msg && msg.type);
    return;
  }

  const { msg_id, cmd, params, session } = msg;
  logI("dispatch cmd=", cmd, "msg_id=", msg_id, "session=", session || "(none)");

  const handler = HANDLERS[cmd];
  if (!handler) {
    send({ type: "error", msg_id, message: `unknown command: ${cmd}`, code: "UNKNOWN_CMD" });
    return;
  }

  try {
    const normalizedParams = normalizeRefs(params || {});
    const data = await handler(normalizedParams, session);
    send({ type: "result", msg_id, data });
  } catch (err) {
    logE("handler threw, cmd=", cmd, "err=", err && err.message);
    send({
      type: "error",
      msg_id,
      message: err && err.message ? err.message : String(err),
      code: (err && err.code) || "INTERNAL",
    });
  }
}

// Snapshot returns refs as bare "e13"; the on-the-wire format for click/fill/etc.
// historically required "@e13". Accept both: if a caller passes "e13", auto-prefix
// with "@" so downstream validators still see the canonical "@e13".
function normalizeRefs(params) {
  if (!params || typeof params !== "object") return params;
  const out = { ...params };
  if (typeof out.ref === "string" && /^e\d+$/.test(out.ref)) {
    out.ref = "@" + out.ref;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Action handlers
// ---------------------------------------------------------------------------

const NAV_TIMEOUT_MS = 30000;

async function navigate(params, session) {
  const { url, newTab, group_title } = params;
  if (!url) {
    const err = new Error("navigate requires params.url");
    err.code = "INVALID_PARAMS";
    throw err;
  }

  const state = getSessionState(session);
  let tabId;

  if (newTab || state.currentTabId == null) {
    // Create a new tab
    let tab;
    try {
      tab = await chrome.tabs.create({ url });
    } catch (err) {
      const e = new Error("tab creation failed: " + (err && err.message));
      e.code = "TAB_CREATE_FAILED";
      throw e;
    }
    tabId = tab.id;
    state.currentTabId = tabId;
    state.tabs.add(tabId);

    // Add to session tab group
    try {
      await groupAddTab(session, tabId, group_title);
    } catch (err) {
      logW("addTabToSession failed:", err && err.message);
    }
  } else {
    // Update existing tab
    tabId = state.currentTabId;
    try {
      await chrome.tabs.update(tabId, { url });
    } catch (err) {
      // Tab might have been closed; try creating a new one
      logW("tabs.update failed, creating new tab:", err && err.message);
      let tab;
      try {
        tab = await chrome.tabs.create({ url });
      } catch (err2) {
        const e = new Error("tab creation failed: " + (err2 && err2.message));
        e.code = "TAB_CREATE_FAILED";
        throw e;
      }
      tabId = tab.id;
      state.currentTabId = tabId;
      state.tabs.add(tabId);
      try {
        await groupAddTab(session, tabId, group_title);
      } catch (err2) {
        logW("addTabToSession failed:", err2 && err2.message);
      }
    }
  }

  // Wait for tab to finish loading
  await waitForTabComplete(tabId, NAV_TIMEOUT_MS);

  // Get final tab info
  const finalTab = await chrome.tabs.get(tabId);
  return { success: true, url: finalTab.url, tabId };
}

/**
 * Wait for a tab to reach status="complete". Resolves when done or rejects on timeout.
 */
function waitForTabComplete(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    let settled = false;

    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(onUpdated);
      // Timeout is non-fatal — the page may still be usable
      logW("waitForTabComplete timeout, tabId=", tabId);
      resolve();
    }, timeoutMs);

    function onUpdated(updatedTabId, changeInfo) {
      if (updatedTabId !== tabId || settled) return;
      if (changeInfo.status === "complete") {
        settled = true;
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(onUpdated);
        resolve();
      }
    }

    chrome.tabs.onUpdated.addListener(onUpdated);

    // Check if already complete (race: tab may have loaded before listener attached)
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError || settled) return;
      if (tab && tab.status === "complete") {
        settled = true;
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(onUpdated);
        resolve();
      }
    });
  });
}


async function snapshot(params, session) {
  const state = getSessionState(session);
  const tabId = state.currentTabId;
  if (!tabId) {
    const err = new Error("No active tab in session");
    err.code = "NO_ACTIVE_TAB";
    throw err;
  }

  let results;
  try {
    results = await chrome.scripting.executeScript({
      target: { tabId },
      files: ["inject-scripts/snapshot.js"],
    });
  } catch (err) {
    const e = new Error("snapshot injection failed: " + (err && err.message));
    e.code = "SNAPSHOT_FAILED";
    throw e;
  }

  if (!results || !results[0]) {
    const err = new Error("snapshot returned no results");
    err.code = "SNAPSHOT_FAILED";
    throw err;
  }

  if (results[0].error) {
    const err = new Error("snapshot error: " + results[0].error.message);
    err.code = "SNAPSHOT_FAILED";
    throw err;
  }

  return results[0].result;
}

async function click(params, session) {
  const state = getSessionState(session);
  const tabId = state.currentTabId;
  if (!tabId) {
    const err = new Error("No active tab in session");
    err.code = "NO_ACTIVE_TAB";
    throw err;
  }

  const { ref, selector } = params;
  if (!ref && !selector) {
    const err = new Error("click requires ref or selector");
    err.code = "NO_TARGET";
    throw err;
  }

  // Validate ref format
  if (ref && !/^@e\d+$/.test(ref)) {
    const err = new Error("invalid ref format: " + ref);
    err.code = "INVALID_REF";
    throw err;
  }

  const target = ref || null;
  const css = selector || null;

  let results;
  try {
    results = await chrome.scripting.executeScript({
      target: { tabId },
      func: (targetRef, cssSel) => {
        let el = null;
        let resolvedBy = null;

        if (targetRef) {
          const refNum = targetRef.replace("@e", "");
          el = document.querySelector('[data-bb-ref="e' + refNum + '"]');
          if (!el) {
            return { success: false, error: "REF_EXPIRED", message: "Element with " + targetRef + " no longer exists" };
          }
          resolvedBy = "ref";
        } else if (cssSel) {
          el = document.querySelector(cssSel);
          if (!el) {
            return { success: false, error: "ELEMENT_NOT_FOUND", message: "No element matches: " + cssSel };
          }
          resolvedBy = "selector";
        }

        el.scrollIntoView({ block: "center", behavior: "instant" });
        el.click();
        return {
          success: true,
          tag: el.tagName.toLowerCase(),
          text: (el.textContent || "").trim().slice(0, 100),
          resolved_by: resolvedBy,
        };
      },
      args: [target, css],
    });
  } catch (err) {
    const e = new Error("click injection failed: " + (err && err.message));
    e.code = "CLICK_FAILED";
    throw e;
  }

  if (!results || !results[0]) {
    const err = new Error("click returned no results");
    err.code = "CLICK_FAILED";
    throw err;
  }

  const r = results[0].result;
  if (r && r.error) {
    const err = new Error(r.message || r.error);
    err.code = r.error;
    throw err;
  }
  return r;
}

async function fill(params, session) {
  const state = getSessionState(session);
  const tabId = state.currentTabId;
  if (!tabId) {
    const err = new Error("No active tab in session");
    err.code = "NO_ACTIVE_TAB";
    throw err;
  }

  const { ref, selector, value } = params;
  if (!ref && !selector) {
    const err = new Error("fill requires ref or selector");
    err.code = "NO_TARGET";
    throw err;
  }
  if (value == null) {
    const err = new Error("fill requires value");
    err.code = "INVALID_PARAMS";
    throw err;
  }

  if (ref && !/^@e\d+$/.test(ref)) {
    const err = new Error("invalid ref format: " + ref);
    err.code = "INVALID_REF";
    throw err;
  }

  const target = ref || null;
  const css = selector || null;

  let results;
  try {
    results = await chrome.scripting.executeScript({
      target: { tabId },
      func: (targetRef, cssSel, fillValue) => {
        let el = null;
        let resolvedBy = null;

        if (targetRef) {
          const refNum = targetRef.replace("@e", "");
          el = document.querySelector('[data-bb-ref="e' + refNum + '"]');
          if (!el) {
            return { success: false, error: "REF_EXPIRED", message: "Element with " + targetRef + " no longer exists" };
          }
          resolvedBy = "ref";
        } else if (cssSel) {
          el = document.querySelector(cssSel);
          if (!el) {
            return { success: false, error: "ELEMENT_NOT_FOUND", message: "No element matches: " + cssSel };
          }
          resolvedBy = "selector";
        }

        el.scrollIntoView({ block: "center", behavior: "instant" });
        el.focus();

        // Detect contenteditable
        if (el.isContentEditable || el.getAttribute("contenteditable") === "true" || el.getAttribute("contenteditable") === "") {
          var sel = window.getSelection();
          var range = document.createRange();
          range.selectNodeContents(el);
          sel.removeAllRanges();
          sel.addRange(range);
          document.execCommand("insertText", false, fillValue);
          return { success: true, mode: "contenteditable", resolved_by: resolvedBy };
        }

        // Input/Textarea path
        var tag = el.tagName.toLowerCase();
        var proto = tag === "textarea" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        var setter = Object.getOwnPropertyDescriptor(proto, "value");
        if (setter && setter.set) {
          setter.set.call(el, fillValue);
        } else {
          el.value = fillValue;
        }
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return { success: true, mode: "value", resolved_by: resolvedBy };
      },
      args: [target, css, value],
    });
  } catch (err) {
    const e = new Error("fill injection failed: " + (err && err.message));
    e.code = "FILL_FAILED";
    throw e;
  }

  if (!results || !results[0]) {
    const err = new Error("fill returned no results");
    err.code = "FILL_FAILED";
    throw err;
  }

  const r = results[0].result;
  if (r && r.error) {
    const err = new Error(r.message || r.error);
    err.code = r.error;
    throw err;
  }
  return r;
}

async function screenshot(params, session) {
  const state = getSessionState(session);
  const tabId = state.currentTabId;
  if (!tabId) {
    const err = new Error("No active tab in session");
    err.code = "NO_ACTIVE_TAB";
    throw err;
  }

  const format = params.format || "png";
  const quality = format === "jpeg" ? (params.quality || 80) : undefined;
  const { ref, selector } = params;

  // Element-level screenshot: scroll into view + capture + crop in content script
  if (ref || selector) {
    if (ref && !/^@e\d+$/.test(ref)) {
      const err = new Error("invalid ref format: " + ref);
      err.code = "INVALID_REF";
      throw err;
    }

    const target = ref || null;
    const css = selector || null;

    // Step 1: scroll element into view and get bounding rect
    const rectResult = await chrome.scripting.executeScript({
      target: { tabId },
      func: (targetRef, cssSel) => {
        let el = null;
        if (targetRef) {
          const refNum = targetRef.replace("@e", "");
          el = document.querySelector('[data-bb-ref="e' + refNum + '"]');
          if (!el) return { error: "REF_EXPIRED" };
        } else if (cssSel) {
          el = document.querySelector(cssSel);
          if (!el) return { error: "ELEMENT_NOT_FOUND" };
        }
        el.scrollIntoView({ block: "center", behavior: "instant" });
        const rect = el.getBoundingClientRect();
        return {
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
          dpr: window.devicePixelRatio || 1,
        };
      },
      args: [target, css],
    });

    const rr = rectResult[0].result;
    if (rr && rr.error) {
      const err = new Error(rr.error);
      err.code = rr.error;
      throw err;
    }

    // Step 2: capture full viewport
    let dataUrl;
    try {
      dataUrl = await chrome.tabs.captureVisibleTab(null, {
        format: format === "jpeg" ? "jpeg" : "png",
        quality,
      });
    } catch (err) {
      const e = new Error("screenshot failed: " + (err && err.message));
      e.code = "SCREENSHOT_FAILED";
      throw e;
    }

    // Step 3: crop in content script using Canvas
    const cropResult = await chrome.scripting.executeScript({
      target: { tabId },
      func: (fullBase64, rect, dpr, fmt, q) => {
        return new Promise((resolve) => {
          const img = new Image();
          img.onload = () => {
            const canvas = document.createElement("canvas");
            const cx = rect.x * dpr;
            const cy = rect.y * dpr;
            const cw = rect.width * dpr;
            const ch = rect.height * dpr;
            canvas.width = cw;
            canvas.height = ch;
            const ctx = canvas.getContext("2d");
            ctx.drawImage(img, cx, cy, cw, ch, 0, 0, cw, ch);
            const mimeType = fmt === "jpeg" ? "image/jpeg" : "image/png";
            const result = canvas.toDataURL(mimeType, q > 0 ? q / 100 : undefined);
            resolve(result.split(",")[1] || "");
          };
          img.src = fullBase64;
        });
      },
      args: [dataUrl, { x: rr.x, y: rr.y, width: rr.width, height: rr.height }, rr.dpr, format, quality || 0],
    });

    const croppedBase64 = cropResult[0].result;
    return { base64: croppedBase64, format, width: rr.width, height: rr.height };
  }

  // Full viewport screenshot (original behavior)
  let dataUrl;
  try {
    dataUrl = await chrome.tabs.captureVisibleTab(null, {
      format: format === "jpeg" ? "jpeg" : "png",
      quality: quality,
    });
  } catch (err) {
    const e = new Error("screenshot failed: " + (err && err.message));
    e.code = "SCREENSHOT_FAILED";
    throw e;
  }

  const base64 = dataUrl.split(",")[1] || "";

  let width = 0, height = 0;
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => ({ width: window.innerWidth, height: window.innerHeight }),
    });
    if (results && results[0] && results[0].result) {
      width = results[0].result.width;
      height = results[0].result.height;
    }
  } catch (_) {}

  return { base64, format, width, height };
}

async function evaluate(params, session) {
  const state = getSessionState(session);
  const tabId = state.currentTabId;
  if (!tabId) {
    const err = new Error("No active tab in session");
    err.code = "NO_ACTIVE_TAB";
    throw err;
  }

  const { code } = params;
  if (!code) {
    const err = new Error("evaluate requires params.code");
    err.code = "INVALID_PARAMS";
    throw err;
  }

  let results;
  try {
    results = await chrome.scripting.executeScript({
      target: { tabId },
      func: (jsCode) => {
        try {
          var result = eval(jsCode);
          var type = typeof result;
          if (result === null) type = "null";
          if (result === undefined) type = "undefined";
          // Serialize for transport
          var value = result;
          if (type === "object" || Array.isArray(result)) {
            try { value = JSON.parse(JSON.stringify(result)); }
            catch (_) { value = String(result); type = "string"; }
          }
          return { type: type, value: value };
        } catch (e) {
          return { type: "error", value: e.message };
        }
      },
      args: [code],
      world: "MAIN",
    });
  } catch (err) {
    const e = new Error("evaluate injection failed: " + (err && err.message));
    e.code = "EVALUATE_FAILED";
    throw e;
  }

  if (!results || !results[0]) {
    const err = new Error("evaluate returned no results");
    err.code = "EVALUATE_FAILED";
    throw err;
  }

  return results[0].result;
}

async function list_tabs(_params, session) {
  const state = getSessionState(session);
  const tabIds = [...state.tabs];
  const results = [];

  for (const tabId of tabIds) {
    try {
      const tab = await chrome.tabs.get(tabId);
      results.push({
        tabId: tab.id,
        url: tab.url || "",
        title: tab.title || "",
        active: tab.active,
        groupTitle: null, // Would need tabGroups.get to resolve
      });
    } catch (_) {
      // Tab no longer exists, clean up
      state.tabs.delete(tabId);
      if (state.currentTabId === tabId) {
        state.currentTabId = null;
      }
    }
  }

  // Try to resolve group titles
  if (chrome.tabGroups && chrome.tabGroups.get) {
    for (const entry of results) {
      try {
        const tab = await chrome.tabs.get(entry.tabId);
        if (tab.groupId && tab.groupId !== -1) {
          const group = await chrome.tabGroups.get(tab.groupId);
          entry.groupTitle = group.title || null;
        }
      } catch (_) {
        // Non-critical
      }
    }
  }

  return { tabs: results };
}

async function find_tab(params, _session) {
  const { url, active } = params;
  const query = {};
  if (url) query.url = url;
  if (active != null) query.active = active;

  try {
    const tabs = await chrome.tabs.query(query);
    if (tabs && tabs.length > 0) {
      const tab = tabs[0];
      return { success: true, tabId: tab.id, url: tab.url };
    }
    return { success: false, tabId: null, url: null };
  } catch (err) {
    const e = new Error("find_tab query failed: " + (err && err.message));
    e.code = "QUERY_FAILED";
    throw e;
  }
}

async function close_tab(_params, session) {
  const state = getSessionState(session);
  const tabId = state.currentTabId;

  if (tabId == null) {
    return { success: true }; // Nothing to close
  }

  try {
    await chrome.tabs.remove(tabId);
  } catch (err) {
    logW("close_tab: tabs.remove failed:", err && err.message);
    // Tab may already be gone — still clean up state
  }

  state.tabs.delete(tabId);
  state.currentTabId = null;

  // Set next available tab as current
  if (state.tabs.size > 0) {
    state.currentTabId = [...state.tabs][state.tabs.size - 1];
  }

  return { success: true };
}

async function close_session(_params, session) {
  const result = await closeSessionGroup(session);
  // Also clean local state
  sessionTabs.delete(session);
  return { success: true, closed: result ? result.closed : 0 };
}

// ---------------------------------------------------------------------------
// Network monitoring (CDP Network domain, on-demand debugger attach)
// ---------------------------------------------------------------------------

const _networkState = new Map(); // session -> { tabId, requests: Map<requestId, entry>, attached: bool }
const MAX_NETWORK_ENTRIES = 200;

async function network(params, session) {
  const { cmd, filter, requestId } = params;
  if (!cmd) {
    const err = new Error("network requires params.cmd (start|stop|list|detail)");
    err.code = "INVALID_PARAMS";
    throw err;
  }

  switch (cmd) {
    case "start": return _networkStart(params, session);
    case "stop": return _networkStop(session);
    case "list": return _networkList(session, filter);
    case "detail": return _networkDetail(session, requestId);
    default: {
      const err = new Error("unknown network cmd: " + cmd);
      err.code = "INVALID_PARAMS";
      throw err;
    }
  }
}

async function _networkStart(_params, session) {
  const state = getSessionState(session);
  const tabId = state.currentTabId;
  if (!tabId) {
    const err = new Error("No active tab in session");
    err.code = "NO_ACTIVE_TAB";
    throw err;
  }

  if (_networkState.has(session) && _networkState.get(session).attached) {
    const err = new Error("Network monitoring already active for this session");
    err.code = "NETWORK_ALREADY_ACTIVE";
    throw err;
  }

  const ns = { tabId, requests: new Map(), attached: false };
  _networkState.set(session, ns);

  try {
    await chrome.debugger.attach({ tabId }, "1.3");
    ns.attached = true;
  } catch (err) {
    _networkState.delete(session);
    const e = new Error("debugger attach failed: " + (err && err.message));
    e.code = "DEBUGGER_ATTACH_FAILED";
    throw e;
  }

  await _cdpSend(tabId, "Network.enable", {});

  chrome.debugger.onEvent.addListener(_networkEventListener);
  return { success: true, tabId };
}

function _networkEventListener(source, method, params) {
  for (const [session, ns] of _networkState.entries()) {
    if (source.tabId !== ns.tabId) continue;

    if (method === "Network.requestWillBeSent") {
      const entry = {
        requestId: params.requestId,
        url: params.request.url,
        method: params.request.method,
        headers: params.request.headers,
        timestamp: params.timestamp,
        status: null,
        responseHeaders: null,
        size: 0,
        mimeType: null,
      };
      ns.requests.set(params.requestId, entry);
      if (ns.requests.size > MAX_NETWORK_ENTRIES) {
        const first = ns.requests.keys().next().value;
        ns.requests.delete(first);
      }
    } else if (method === "Network.responseReceived") {
      const entry = ns.requests.get(params.requestId);
      if (entry) {
        entry.status = params.response.status;
        entry.responseHeaders = params.response.headers;
        entry.mimeType = params.response.mimeType;
      }
    } else if (method === "Network.loadingFinished") {
      const entry = ns.requests.get(params.requestId);
      if (entry) {
        entry.size = params.encodedDataLength || 0;
      }
    }
  }
}

async function _networkStop(session) {
  const ns = _networkState.get(session);
  if (!ns || !ns.attached) {
    const err = new Error("Network monitoring not active");
    err.code = "NETWORK_NOT_ACTIVE";
    throw err;
  }

  try {
    await _cdpSend(ns.tabId, "Network.disable", {});
    await chrome.debugger.detach({ tabId: ns.tabId });
  } catch (err) {
    logW("network stop detach error:", err && err.message);
  }

  ns.attached = false;
  const count = ns.requests.size;
  _networkState.delete(session);

  if (_networkState.size === 0) {
    chrome.debugger.onEvent.removeListener(_networkEventListener);
  }

  return { success: true, captured: count };
}

function _networkList(session, filter) {
  const ns = _networkState.get(session);
  if (!ns) {
    return { success: true, requests: [] };
  }

  let entries = [...ns.requests.values()].map(e => ({
    requestId: e.requestId,
    url: e.url,
    method: e.method,
    status: e.status,
    size: e.size,
    mimeType: e.mimeType,
  }));

  if (filter) {
    const f = filter.toLowerCase();
    entries = entries.filter(e => e.url.toLowerCase().includes(f));
  }

  return { success: true, requests: entries };
}

async function _networkDetail(session, requestId) {
  if (!requestId) {
    const err = new Error("network detail requires requestId");
    err.code = "INVALID_PARAMS";
    throw err;
  }

  const ns = _networkState.get(session);
  if (!ns) {
    const err = new Error("Network monitoring not active");
    err.code = "NETWORK_NOT_ACTIVE";
    throw err;
  }

  const entry = ns.requests.get(requestId);
  if (!entry) {
    return { success: false, error: "Request not found" };
  }

  let body = null;
  if (ns.attached) {
    try {
      const resp = await _cdpSend(ns.tabId, "Network.getResponseBody", { requestId });
      body = resp.base64Encoded ? atob(resp.body) : resp.body;
    } catch (_) {
      body = null;
    }
  }

  return {
    success: true,
    requestId: entry.requestId,
    url: entry.url,
    method: entry.method,
    status: entry.status,
    requestHeaders: entry.headers,
    responseHeaders: entry.responseHeaders,
    size: entry.size,
    mimeType: entry.mimeType,
    body,
  };
}

// ---------------------------------------------------------------------------
// File upload (CDP DOM.setFileInputFiles)
// ---------------------------------------------------------------------------

async function upload(params, session) {
  const state = getSessionState(session);
  const tabId = state.currentTabId;
  if (!tabId) {
    const err = new Error("No active tab in session");
    err.code = "NO_ACTIVE_TAB";
    throw err;
  }

  const { ref, selector, files } = params;
  if (!ref && !selector) {
    const err = new Error("upload requires ref or selector");
    err.code = "NO_TARGET";
    throw err;
  }
  if (!files || !Array.isArray(files) || files.length === 0) {
    const err = new Error("upload requires files array");
    err.code = "INVALID_PARAMS";
    throw err;
  }

  if (ref && !/^@e\d+$/.test(ref)) {
    const err = new Error("invalid ref format: " + ref);
    err.code = "INVALID_REF";
    throw err;
  }

  // Find the file input node via content script
  const target = ref || null;
  const css = selector || null;

  const nodeResult = await chrome.scripting.executeScript({
    target: { tabId },
    func: (targetRef, cssSel) => {
      let el = null;
      if (targetRef) {
        const refNum = targetRef.replace("@e", "");
        el = document.querySelector('[data-bb-ref="e' + refNum + '"]');
        if (!el) return { error: "REF_EXPIRED" };
      } else if (cssSel) {
        el = document.querySelector(cssSel);
        if (!el) return { error: "ELEMENT_NOT_FOUND" };
      }
      if (!el || el.tagName.toLowerCase() !== "input" || el.type !== "file") {
        return { error: "UPLOAD_NO_FILE_INPUT", message: "Target is not a file input" };
      }
      // We need the backend node id — mark it for CDP query
      el.setAttribute("data-bb-upload-target", "1");
      return { success: true };
    },
    args: [target, css],
  });

  const nr = nodeResult[0].result;
  if (nr && nr.error) {
    const err = new Error(nr.message || nr.error);
    err.code = nr.error;
    throw err;
  }

  // Attach debugger, resolve node, set files
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
  } catch (err) {
    const e = new Error("debugger attach failed: " + (err && err.message));
    e.code = "DEBUGGER_ATTACH_FAILED";
    throw e;
  }

  try {
    // Find node via DOM
    const doc = await _cdpSend(tabId, "DOM.getDocument", {});
    const nodeId = (await _cdpSend(tabId, "DOM.querySelector", {
      nodeId: doc.root.nodeId,
      selector: '[data-bb-upload-target="1"]',
    })).nodeId;

    // Create blob URLs for files and set them
    const filePaths = [];
    for (const f of files) {
      // Write file content to a temp blob handled by the page
      const blobResult = await chrome.scripting.executeScript({
        target: { tabId },
        func: (base64Data, filename, mimeType) => {
          const binary = atob(base64Data);
          const bytes = new Uint8Array(binary.length);
          for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
          const blob = new Blob([bytes], { type: mimeType || "application/octet-stream" });
          const file = new File([blob], filename, { type: mimeType || "application/octet-stream" });
          // Store on window for CDP to pick up
          window.__bb_upload_files = window.__bb_upload_files || [];
          window.__bb_upload_files.push(file);
          return { index: window.__bb_upload_files.length - 1 };
        },
        args: [f.data, f.filename || "file", f.mimeType || "application/octet-stream"],
        world: "MAIN",
      });
      filePaths.push(f.filename || "file");
    }

    // Use DOM.setFileInputFiles with the file objects via scripting
    await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const input = document.querySelector('[data-bb-upload-target="1"]');
        if (!input || !window.__bb_upload_files) return;
        const dt = new DataTransfer();
        for (const f of window.__bb_upload_files) dt.items.add(f);
        input.files = dt.files;
        input.dispatchEvent(new Event("change", { bubbles: true }));
        input.removeAttribute("data-bb-upload-target");
        delete window.__bb_upload_files;
      },
      world: "MAIN",
    });

    return { success: true, fileCount: files.length };
  } finally {
    try { await chrome.debugger.detach({ tabId }); } catch (_) {}
    // Clean up marker attribute
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        func: () => {
          const el = document.querySelector('[data-bb-upload-target]');
          if (el) el.removeAttribute("data-bb-upload-target");
        },
      });
    } catch (_) {}
  }
}

// ---------------------------------------------------------------------------
// Save as PDF (CDP Page.printToPDF)
// ---------------------------------------------------------------------------

async function save_as_pdf(params, session) {
  const state = getSessionState(session);
  const tabId = state.currentTabId;
  if (!tabId) {
    const err = new Error("No active tab in session");
    err.code = "NO_ACTIVE_TAB";
    throw err;
  }

  const {
    paper_format = "a4",
    landscape = false,
    scale = 1.0,
    print_background = true,
  } = params;

  const paperSizes = {
    a4: { width: 8.27, height: 11.69 },
    letter: { width: 8.5, height: 11 },
    legal: { width: 8.5, height: 14 },
    a3: { width: 11.69, height: 16.54 },
    tabloid: { width: 11, height: 17 },
  };
  const size = paperSizes[paper_format] || paperSizes.a4;

  try {
    await chrome.debugger.attach({ tabId }, "1.3");
  } catch (err) {
    const e = new Error("debugger attach failed: " + (err && err.message));
    e.code = "DEBUGGER_ATTACH_FAILED";
    throw e;
  }

  try {
    const result = await _cdpSend(tabId, "Page.printToPDF", {
      paperWidth: size.width,
      paperHeight: size.height,
      landscape: !!landscape,
      scale: Math.max(0.1, Math.min(2.0, scale)),
      printBackground: !!print_background,
      marginTop: 0.4,
      marginBottom: 0.4,
      marginLeft: 0.4,
      marginRight: 0.4,
    });

    const base64 = result.data || "";
    const sizeBytes = Math.floor(base64.length * 3 / 4);

    return { success: true, base64, sizeBytes, format: paper_format };
  } catch (err) {
    const e = new Error("PDF generation failed: " + (err && err.message));
    e.code = "PDF_GENERATION_FAILED";
    throw e;
  } finally {
    try { await chrome.debugger.detach({ tabId }); } catch (_) {}
  }
}

// ---------------------------------------------------------------------------
// CDP helper
// ---------------------------------------------------------------------------

function _cdpSend(tabId, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params || {}, (result) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
      } else {
        resolve(result);
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Handler routing table
// ---------------------------------------------------------------------------
const HANDLERS = {
  navigate,
  snapshot,
  click,
  fill,
  screenshot,
  evaluate,
  list_tabs,
  find_tab,
  close_tab,
  close_session,
  network,
  upload,
  save_as_pdf,
};
