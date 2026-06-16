"""Popup schema config endpoint — GET /api/v1/popup/config.

v0.6.0 Step 2 §2.3 — server-driven popup schema.

Scope (per execution-plan-v2.1.md §2.3): only the **schema-able** slice of the
popup is served here:
1. i18n strings (status labels, button labels, banner text)
2. ``pair_error_map`` (server detail substring → user-facing message)
3. ``state_label`` (connection state key → label)
4. ``visual_cues`` 4 fields metadata (badge/title/toast/notif)

Anything beyond this — DOM event wiring, OAuth flow, command history rendering
— stays in popup.js and ships in the zip. See execution-plan §2.3 for why.

Spike-stage: defaults are hardcoded constants below. No DB persistence, no
caching layer. To change a string, edit this file and reload the server.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

router = APIRouter()


# Bump when any default below changes so the extension can cache-bust.
POPUP_CONFIG_VERSION = 1


# ── defaults extracted from extension/src/popup/popup.js v0.5.0 ───────────────

# popup.js:58-62 — STATE_LABEL
_STATE_LABEL_ZH = {
    "connected": "已连接",
    "connecting": "连接中…",
    "disconnected": "未连接",
}

# popup.js:65-70 — PAIR_ERROR_MAP (server detail substring → user-facing zh)
_PAIR_ERROR_MAP_ZH = {
    "Invalid pairing code":      "配对码不正确，请确认 @丹秘 发出的 6 位码",
    "Pairing code expired":      "配对码已过期（有效期 30 分钟）。请回 IM 重新获取",
    "Pairing code already used": "配对码已被使用，每码只能用一次。请重新获取",
    "Too many pairing attempts": "请求过于频繁，请稍后再试",
}

# popup.js:282-289 — visual_cues field metadata (default-on, label, dom id)
_VISUAL_CUES = {
    "badge":  {"default": True, "label_zh": "徽标计数",        "dom_id": "vis-badge"},
    "title":  {"default": True, "label_zh": "标题闪烁",        "dom_id": "vis-title"},
    "toast":  {"default": True, "label_zh": "页内 toast 提示", "dom_id": "vis-toast"},
    "notif":  {"default": True, "label_zh": "系统通知",        "dom_id": "vis-notif"},
}

# popup.js scattered string literals — i18n bundle
# Keys are namespaced by tab/section to keep the table reviewable.
_I18N_ZH = {
    # status / header
    "status.paused":                 "已暂停",
    # pairing errors (popup.js:73-83 fallbacks)
    "pair.error.network":            "连接失败，请检查网络",
    "pair.error.unreachable":        "无法连接服务器，请检查地址是否正确、服务是否在线",
    "pair.error.http_template":      "连接失败（HTTP {status}）",
    # version badge (popup.js:172-179)
    "version.latest_suffix":         " ✓ 最新",
    "version.upgradable_template":   " ⚠ 可升级至 v{latest}",
    # test connection button (popup.js:190-208)
    "test.btn.idle":                 "🔌 测试连接",
    "test.btn.busy":                 "测试中…",
    "test.no_server":                "未连接服务器",
    "test.ok_template":              "✓ 服务正常 · {n} 个设备在线",
    "test.fail_http_template":       "服务返回 HTTP {status}",
    "test.fail_network_template":    "无法连接（{msg}）",
    # history tab (popup.js:226)
    "history.empty":                 "暂无命令",
    # settings tab — sensitive stats (popup.js:308-327)
    "stats.no_server":               "未连接服务器",
    "stats.loading":                 "加载中…",
    "stats.failed":                  "获取失败",
    # settings tab — devices list (popup.js:335-405)
    "devices.no_server":             "未连接服务器",
    "devices.loading":               "加载中…",
    "devices.empty":                 "暂无设备",
    "devices.failed_template":       "获取失败：{msg}",
    "devices.last_seen_unknown":     "—",
    "devices.last_seen_template":    "最后活跃：{ts}",
    "devices.default_name":          "Chrome Extension",
    "devices.badge_revoked":         "已吊销",
    "devices.badge_current":         "本机",
    "devices.copy_id_title":         "复制完整 ID",
    "devices.revoke_btn":            "吊销",
    "devices.revoke_busy":           "吊销中…",
    "devices.revoke_confirm_template": "确定远程吊销设备「{name}」？该设备将立即断线。",
    "devices.revoke_failed_http_template": "吊销失败：HTTP {status}",
    "devices.revoke_failed_template":      "吊销失败：{msg}",
    # pairing form (popup.js:478-506)
    "pair.btn.idle":                 "连接",
    "pair.btn.busy":                 "连接中…",
    "pair.disconnect_confirm":       "确定解除配对？需要重新输入配对码才能再连。",
    # update banner (popup.js:521-525)
    "update.force_text_template":    "当前 v{current}，最低要求 v{min}",
}


# Future-proofing: if we ever add en-US, add a sibling _I18N_EN dict and a
# language map. For v0.6.0 spike we only ship zh-CN.
_LANG_BUNDLES = {
    "zh-CN": {
        "i18n":           _I18N_ZH,
        "pair_error_map": _PAIR_ERROR_MAP_ZH,
        "state_label":    _STATE_LABEL_ZH,
    },
}

_DEFAULT_LANG = "zh-CN"


def init_popup_config_router() -> APIRouter:
    """No state to wire (defaults are module-level constants); kept for
    parity with other ``init_*_router`` factories in server/api/."""
    return router


@router.get("/popup/config")
async def get_popup_config(
    lang: str = Query(_DEFAULT_LANG, description="BCP-47 language tag, e.g. zh-CN"),
) -> dict:
    """Return the popup schema config for the requested language.

    Falls back to ``zh-CN`` if the requested language is not bundled.
    """
    bundle = _LANG_BUNDLES.get(lang) or _LANG_BUNDLES[_DEFAULT_LANG]
    return {
        "version": POPUP_CONFIG_VERSION,
        "lang": lang if lang in _LANG_BUNDLES else _DEFAULT_LANG,
        "i18n":           bundle["i18n"],
        "pair_error_map": bundle["pair_error_map"],
        "state_label":    bundle["state_label"],
        "visual_cues":    _VISUAL_CUES,
    }
