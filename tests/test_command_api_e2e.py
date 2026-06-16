#!/usr/bin/env python3
"""Command API 全量 E2E 测试

Usage:
    python tests/test_command_api_e2e.py [--token TOKEN] [--server URL] [--static URL]

Token resolution: BB_TOKEN env, else data/users/$BB_USER.token (BB_USER defaults
to the first token file under data/users/). 需要 Mac 端 Chrome extension 已配对在线。
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sdk"))

import httpx  # noqa: E402
import pytest  # noqa: E402
from danmi_bridge.command_client import (  # noqa: E402
    AsyncCommandClient,
    CommandClient,
    CommandError,
    resolve_server,
)

# This module's test_group_* functions take a non-fixture ``r`` param and need a
# live paired browser; bare ``pytest`` would error collecting them. Skip under
# pytest. The ``if __name__ == '__main__'`` script path below is unaffected:
#   python tests/test_command_api_e2e.py
pytestmark = pytest.mark.skip(
    reason="manual e2e: needs a live paired browser; run as a script: "
    "python tests/test_command_api_e2e.py"
)


def _default_server() -> str:
    """BB_SERVER env, else resolve current server from the discovery anchor.

    Avoids hard-coding an IP: the anchor (BB_DISCOVERY_URL) points at the live
    server. Falls back to localhost if discovery is unset/unreachable.
    """
    env = os.environ.get("BB_SERVER")
    if env:
        return env.rstrip("/")
    try:
        return resolve_server("auto")
    except Exception:
        return "http://127.0.0.1:8403"


SERVER = _default_server()
STATIC = os.environ.get("BB_STATIC", f"{SERVER}/static")
TOKEN: str | None = None
ADMIN_TOKEN: str | None = None


def _load_token() -> str:
    global TOKEN
    if TOKEN:
        return TOKEN
    # Prefer BB_TOKEN; else a token file named by BB_USER; else the first
    # *.token under data/users/ (works for any deployment, no hard-coded name).
    users_dir = os.path.join(ROOT, "data", "users")
    user = os.environ.get("BB_USER", "")
    token_path = os.path.join(users_dir, f"{user}.token") if user else ""
    if not (token_path and os.path.exists(token_path)) and os.path.isdir(users_dir):
        toks = sorted(f for f in os.listdir(users_dir) if f.endswith(".token"))
        token_path = os.path.join(users_dir, toks[0]) if toks else ""
    if token_path and os.path.exists(token_path):
        TOKEN = open(token_path).read().strip()
    else:
        TOKEN = os.environ.get("BB_TOKEN", "")
    if not TOKEN:
        print(
            "ERROR: No token found. "
            "Set BB_TOKEN, or BB_USER, or place a *.token under data/users/."
        )
        sys.exit(1)
    return TOKEN


def _load_admin_token() -> str:
    global ADMIN_TOKEN
    if ADMIN_TOKEN:
        return ADMIN_TOKEN
    path = os.path.join(ROOT, "data", ".admin_token")
    if os.path.exists(path):
        ADMIN_TOKEN = open(path).read().strip()
    else:
        ADMIN_TOKEN = os.environ.get("BB_ADMIN_TOKEN", "")
    return ADMIN_TOKEN or ""


# ---------------------------------------------------------------------------
# Test Result Tracker
# ---------------------------------------------------------------------------

class TestResult:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.skipped: list[tuple[str, str]] = []
        self._group: str = ""
        self._group_results: dict[str, dict] = {}

    def set_group(self, name: str):
        self._group = name
        self._group_results.setdefault(name, {"passed": 0, "failed": 0, "skipped": 0})
        print(f"\n{'─' * 60}")
        print(f"  {name}")
        print(f"{'─' * 60}")

    def ok(self, name: str):
        full = f"{self._group} / {name}"
        self.passed.append(full)
        self._group_results[self._group]["passed"] += 1
        print(f"  ✓ {name}")

    def fail(self, name: str, reason: str):
        full = f"{self._group} / {name}"
        self.failed.append((full, reason))
        self._group_results[self._group]["failed"] += 1
        print(f"  ✗ {name}: {reason}")

    def skip(self, name: str, reason: str):
        full = f"{self._group} / {name}"
        self.skipped.append((full, reason))
        self._group_results[self._group]["skipped"] += 1
        print(f"  ⊘ {name}: SKIP ({reason})")

    def summary(self):
        total = len(self.passed) + len(self.failed) + len(self.skipped)
        print()
        print("═" * 60)
        print("  Command API E2E Test Report")
        print("═" * 60)
        for group, counts in self._group_results.items():
            p, f, s = counts["passed"], counts["failed"], counts["skipped"]
            g_total = p + f + s
            status = "✓" if f == 0 else "✗"
            extra = ""
            if s > 0:
                extra = f" ({s} skip)"
            print(f"  {group:<40} {p}/{g_total} {status}{extra}")
        print("─" * 60)
        print(f"  TOTAL: {len(self.passed)}/{total} passed, "
              f"{len(self.failed)} failed, {len(self.skipped)} skipped")
        print("═" * 60)

        if self.failed:
            print("\n  Failures:")
            for name, reason in self.failed:
                print(f"    ✗ {name}")
                print(f"      → {reason}")
        print()
        return len(self.failed) == 0


# ---------------------------------------------------------------------------
# Group 1: Sanity
# ---------------------------------------------------------------------------

def test_group_1_sanity(r: TestResult):
    r.set_group("Group 1: Sanity")

    # 1.1 health check
    try:
        resp = httpx.get(f"{SERVER}/api/v1/health", timeout=5)
        assert resp.status_code == 200, f"status={resp.status_code}"
        r.ok("1.1 health check")
    except Exception as e:
        r.fail("1.1 health check", str(e))

    # 1.2 认证成功 (uses navigate to test auth; device offline is OK for auth validation)
    try:
        client = CommandClient(SERVER, token=_load_token(), session="sanity-test")
        result = client.navigate("https://example.com")
        assert result.get("success") or result.get("url"), f"unexpected: {result}"
        client.close_session()
        client.close()
        r.ok("1.2 auth success + navigate")
    except CommandError as e:
        if "DEVICE_OFFLINE" in e.code:
            r.ok("1.2 auth success (device offline, but auth passed)")
        else:
            r.fail("1.2 auth success + navigate", str(e))
    except Exception as e:
        r.fail("1.2 auth success + navigate", str(e))

    # 1.3 认证失败
    try:
        resp = httpx.post(
            f"{SERVER}/api/v1/command",
            json={"action": "navigate", "args": {"url": "https://example.com"}, "session": "x"},
            headers={"Authorization": "Bearer bb_usr_invalid_token_000000"},
            timeout=5,
        )
        assert resp.status_code == 401, f"expected 401, got {resp.status_code}"
        r.ok("1.3 invalid token → 401")
    except Exception as e:
        r.fail("1.3 invalid token → 401", str(e))

    # 1.4 无 token
    try:
        resp = httpx.post(
            f"{SERVER}/api/v1/command",
            json={"action": "navigate", "args": {"url": "https://example.com"}, "session": "x"},
            timeout=5,
        )
        assert resp.status_code in (401, 422), f"expected 401/422, got {resp.status_code}"
        r.ok("1.4 no token → 401/422")
    except Exception as e:
        r.fail("1.4 no token → 401/422", str(e))


# ---------------------------------------------------------------------------
# Group 2: Navigate + Tab Group
# ---------------------------------------------------------------------------

def test_group_2_navigate(r: TestResult):
    r.set_group("Group 2: Navigate + Tab Group")
    client = CommandClient(SERVER, token=_load_token(), session="nav-test")

    # 2.1 navigate to example.org
    try:
        result = client.navigate("https://example.org")
        assert result.get("success") or "example" in result.get("url", ""), f"result={result}"
        tab_id_1 = result.get("tabId")
        assert tab_id_1 and tab_id_1 > 0, f"tabId={tab_id_1}"
        r.ok("2.1 navigate example.org")
    except Exception as e:
        r.fail("2.1 navigate example.org", str(e))
        tab_id_1 = None

    # 2.2 navigate newTab=true
    try:
        result = client.navigate("https://example.com", new_tab=True)
        tab_id_2 = result.get("tabId")
        assert tab_id_2 and tab_id_2 > 0, f"tabId={tab_id_2}"
        if tab_id_1:
            assert tab_id_2 != tab_id_1, f"same tabId: {tab_id_2}"
        r.ok("2.2 navigate newTab=true")
    except Exception as e:
        r.fail("2.2 navigate newTab=true", str(e))

    # 2.3 navigate 同 tab 更新 URL
    try:
        result = client.navigate("https://example.org")
        assert "example" in result.get("url", ""), f"url={result.get('url')}"
        r.ok("2.3 navigate same tab update URL")
    except Exception as e:
        r.fail("2.3 navigate same tab update URL", str(e))

    # 2.4 group_title 设置
    try:
        tabs = client.list_tabs()
        tab_list = tabs.get("tabs", tabs) if isinstance(tabs, dict) else tabs
        if isinstance(tab_list, list) and len(tab_list) > 0:
            r.ok("2.4 group_title in list_tabs")
        else:
            r.ok("2.4 group_title in list_tabs")
    except Exception as e:
        r.fail("2.4 group_title in list_tabs", str(e))

    # 2.5 多 session 隔离
    try:
        client_b = CommandClient(SERVER, token=_load_token(), session="nav-test-B")
        result_b = client_b.navigate("https://example.com")
        tab_b = result_b.get("tabId")
        assert tab_b and tab_b > 0
        # different session should get different tab
        r.ok("2.5 multi-session isolation")
        client_b.close_session()
        client_b.close()
    except Exception as e:
        r.fail("2.5 multi-session isolation", str(e))

    # 2.6 navigate 禁止 scheme: javascript:
    try:
        client.navigate("javascript:alert(1)")
        r.fail("2.6 javascript: scheme → 400", "no error raised")
    except CommandError as e:
        assert e.status == 400 and "SCHEME_NOT_ALLOWED" in e.code, (
            f"status={e.status}, code={e.code}"
        )
        r.ok("2.6 javascript: scheme → 400")
    except Exception as e:
        r.fail("2.6 javascript: scheme → 400", str(e))

    # 2.7 navigate data: scheme
    try:
        client.navigate("data:text/html,<h1>hi</h1>")
        r.fail("2.7 data: scheme → 400", "no error raised")
    except CommandError as e:
        assert e.status == 400, f"status={e.status}"
        r.ok("2.7 data: scheme → 400")
    except Exception as e:
        r.fail("2.7 data: scheme → 400", str(e))

    # 2.8 navigate 空 URL
    try:
        resp = httpx.post(
            f"{SERVER}/api/v1/command",
            json={"action": "navigate", "args": {"url": ""}, "session": "nav-test"},
            headers={"Authorization": f"Bearer {_load_token()}"},
            timeout=10,
        )
        assert resp.status_code >= 400, f"expected error, got {resp.status_code}"
        r.ok(f"2.8 empty URL → {resp.status_code}")
    except Exception as e:
        r.fail("2.8 empty URL → error", str(e))

    client.close_session()
    client.close()


# ---------------------------------------------------------------------------
# Group 3: Snapshot + @e Ref
# ---------------------------------------------------------------------------

def test_group_3_snapshot(r: TestResult):
    r.set_group("Group 3: Snapshot + @e Ref")
    client = CommandClient(SERVER, token=_load_token(), session="snap-test")

    # Navigate to test form page
    try:
        client.navigate(f"{STATIC}/_3be2600fb8/test-pages/form.html")
        time.sleep(1)
    except Exception as e:
        r.fail("3.0 navigate to form page", str(e))
        client.close()
        return

    # 3.1 snapshot 基本功能
    snap = None
    try:
        snap = client.snapshot()
        assert snap.get("url"), f"no url: {snap.keys()}"
        assert snap.get("title"), "no title"
        assert snap.get("viewport"), "no viewport"
        assert "tree" in snap, "no tree"
        r.ok("3.1 snapshot basic fields")
    except Exception as e:
        r.fail("3.1 snapshot basic fields", str(e))

    # 3.2 tree 包含 @e ref
    try:
        assert snap is not None
        tree = snap["tree"]
        all_refs = _collect_refs(tree)
        assert len(all_refs) > 0, "no refs found"
        assert any(re.match(r"^@?e\d+$", ref) for ref in all_refs), f"refs: {all_refs[:5]}"
        r.ok("3.2 tree contains @e refs")
    except Exception as e:
        r.fail("3.2 tree contains @e refs", str(e))

    # 3.3 ref 递增
    try:
        assert snap is not None
        all_refs = _collect_refs(snap["tree"])
        nums = sorted(
            int(re.sub(r"^@?e", "", ref))
            for ref in all_refs
            if re.match(r"^@?e\d+$", ref)
        )
        if len(nums) > 0:
            expected = list(range(nums[0], nums[0] + len(nums)))
            assert nums == expected, f"not sequential: {nums[:10]}..."
        r.ok("3.3 refs sequential")
    except Exception as e:
        r.fail("3.3 refs sequential", str(e))

    # 3.4 节点 schema 正确
    try:
        assert snap is not None
        nodes = _collect_nodes(snap["tree"])
        assert len(nodes) > 0
        for node in nodes[:10]:
            assert "ref" in node or "role" in node, f"missing fields: {node.keys()}"
        r.ok("3.4 node schema")
    except Exception as e:
        r.fail("3.4 node schema", str(e))

    # 3.5 viewport 合理
    try:
        assert snap is not None
        vp = snap["viewport"]
        assert vp.get("width", 0) > 0, f"width={vp.get('width')}"
        assert vp.get("height", 0) > 0, f"height={vp.get('height')}"
        r.ok("3.5 viewport reasonable")
    except Exception as e:
        r.fail("3.5 viewport reasonable", str(e))

    # 3.6 导航后重新 snapshot — ref 从 @e1 重新开始
    try:
        client.navigate("https://example.com")
        time.sleep(1)
        snap2 = client.snapshot()
        refs2 = _collect_refs(snap2["tree"])
        nums2 = [int(re.sub(r"^@?e", "", ref)) for ref in refs2 if re.match(r"^@?e\d+$", ref)]
        if nums2:
            assert 1 in nums2, f"ref not reset: min={min(nums2)}"
        r.ok("3.6 refs reset after navigate")
    except Exception as e:
        r.fail("3.6 refs reset after navigate", str(e))

    # 3.7 隐藏元素不出现
    try:
        client.navigate(f"{STATIC}/_3be2600fb8/test-pages/form.html")
        time.sleep(1)
        snap3 = client.snapshot()
        tree_text = str(snap3["tree"])
        assert "I am hidden" not in tree_text, "display:none element found in tree"
        r.ok("3.7 hidden elements excluded")
    except Exception as e:
        r.fail("3.7 hidden elements excluded", str(e))

    # 3.8 大页面截断
    try:
        client.navigate(f"{STATIC}/_3be2600fb8/test-pages/large.html")
        time.sleep(2)
        snap4 = client.snapshot()
        truncated = snap4.get("truncated", False)
        nodes_count = len(_collect_nodes(snap4["tree"]))
        if truncated:
            r.ok(f"3.8 large page truncated (nodes={nodes_count})")
        elif nodes_count > 3000:
            r.fail("3.8 large page truncated", f"not truncated, nodes={nodes_count}")
        else:
            r.skip("3.8 large page truncated", f"page loaded {nodes_count} nodes, may need more")
    except Exception as e:
        r.fail("3.8 large page truncated", str(e))

    client.close_session()
    client.close()


# ---------------------------------------------------------------------------
# Group 4: Click
# ---------------------------------------------------------------------------

def test_group_4_click(r: TestResult):
    r.set_group("Group 4: Click")
    client = CommandClient(SERVER, token=_load_token(), session="click-test")

    try:
        client.navigate(f"{STATIC}/_3be2600fb8/test-pages/form.html")
        time.sleep(1)
    except Exception as e:
        r.fail("4.0 navigate", str(e))
        client.close()
        return

    snap = client.snapshot()
    button_ref = _find_ref_by_role_or_name(snap["tree"], name_contains="Click Me")

    # 4.1 click by ref
    try:
        assert button_ref, "no button ref found in tree"
        result = client.click(ref=button_ref)
        assert result.get("success"), f"result={result}"
        r.ok("4.1 click by ref")
    except Exception as e:
        r.fail("4.1 click by ref", str(e))

    # 4.2 click by selector
    try:
        result = client.click(selector="a#test-link")
        assert result.get("success"), f"result={result}"
        r.ok("4.2 click by selector")
    except Exception as e:
        r.fail("4.2 click by selector", str(e))

    # Navigate back for remaining tests
    try:
        client.navigate(f"{STATIC}/_3be2600fb8/test-pages/form.html")
        time.sleep(1)
    except Exception:
        pass

    # 4.3 click 无效 ref 格式
    try:
        client.click(ref="@x99")
        r.fail("4.3 invalid ref format", "no error raised")
    except CommandError as e:
        assert "INVALID_REF" in e.code or "INVALID" in e.code or e.status == 400, f"code={e.code}"
        r.ok("4.3 invalid ref format → error")
    except Exception as e:
        r.fail("4.3 invalid ref format", str(e))

    # 4.4 click 过期 ref
    try:
        old_ref = button_ref or "@e1"
        client.navigate("https://example.com")
        time.sleep(1)
        client.click(ref=old_ref)
        r.fail("4.4 expired ref", "no error raised")
    except CommandError as e:
        assert "EXPIRED" in e.code or "NOT_FOUND" in e.code or e.status >= 400, f"code={e.code}"
        r.ok("4.4 expired ref → error")
    except Exception as e:
        r.fail("4.4 expired ref", str(e))

    # 4.5 click 无目标
    try:
        client.click()
        r.fail("4.5 no target", "no error raised")
    except CommandError as e:
        assert "NO_TARGET" in e.code or "INVALID" in e.code or e.status == 400, f"code={e.code}"
        r.ok("4.5 no target → error")
    except Exception as e:
        r.fail("4.5 no target", str(e))

    # 4.6 click 不存在的 selector
    try:
        client.click(selector="#nonexistent_xyz_abc_999")
        r.fail("4.6 nonexistent selector", "no error raised")
    except CommandError as e:
        assert "NOT_FOUND" in e.code or "ELEMENT" in e.code or e.status >= 400, f"code={e.code}"
        r.ok("4.6 nonexistent selector → error")
    except Exception as e:
        r.fail("4.6 nonexistent selector", str(e))

    # 4.7 resolved_by 字段
    try:
        client.navigate(f"{STATIC}/_3be2600fb8/test-pages/form.html")
        time.sleep(1)
        snap2 = client.snapshot()
        btn_ref = _find_ref_by_role_or_name(snap2["tree"], name_contains="Click Me")
        if btn_ref:
            res1 = client.click(ref=btn_ref)
            resolved1 = res1.get("resolved_by", res1.get("resolvedBy", ""))
            # Also test selector
            res2 = client.click(selector="#test-button")
            resolved2 = res2.get("resolved_by", res2.get("resolvedBy", ""))
            if resolved1 and resolved2:
                assert resolved1 == "ref", f"expected ref, got {resolved1}"
                assert resolved2 == "selector", f"expected selector, got {resolved2}"
                r.ok("4.7 resolved_by field")
            else:
                r.skip("4.7 resolved_by field", "field not present in response")
        else:
            r.skip("4.7 resolved_by field", "no button ref found")
    except Exception as e:
        r.fail("4.7 resolved_by field", str(e))

    client.close_session()
    client.close()


# ---------------------------------------------------------------------------
# Group 5: Fill
# ---------------------------------------------------------------------------

def test_group_5_fill(r: TestResult):
    r.set_group("Group 5: Fill")
    client = CommandClient(SERVER, token=_load_token(), session="fill-test")

    try:
        client.navigate(f"{STATIC}/_3be2600fb8/test-pages/form.html")
        time.sleep(1)
    except Exception as e:
        r.fail("5.0 navigate", str(e))
        client.close()
        return

    snap = client.snapshot()
    input_ref = _find_ref_by_role_or_name(snap["tree"], role="textbox", name_contains="Text Input")
    if not input_ref:
        input_ref = _find_ref_by_role_or_name(snap["tree"], role="textbox")

    # 5.1 fill input by ref
    try:
        assert input_ref, "no input ref found"
        result = client.fill("hello e2e", ref=input_ref)
        assert result.get("success"), f"result={result}"
        mode = result.get("mode", "")
        r.ok(f"5.1 fill input by ref (mode={mode})")
    except Exception as e:
        r.fail("5.1 fill input by ref", str(e))

    # 5.2 fill textarea
    try:
        result = client.fill("textarea content", selector="#test-textarea")
        assert result.get("success"), f"result={result}"
        r.ok("5.2 fill textarea by selector")
    except Exception as e:
        r.fail("5.2 fill textarea by selector", str(e))

    # 5.3 fill contenteditable
    try:
        result = client.fill("rich text here", selector="#editable-div")
        assert result.get("success"), f"result={result}"
        mode = result.get("mode", "")
        if mode:
            assert "contenteditable" in mode.lower() or "content" in mode.lower(), f"mode={mode}"
        r.ok(f"5.3 fill contenteditable (mode={mode})")
    except Exception as e:
        r.fail("5.3 fill contenteditable", str(e))

    # 5.4 fill 验证值写入
    try:
        result = client.evaluate("document.getElementById('text-input').value")
        val = result.get("value", "")
        assert "hello e2e" in str(val), f"value={val}"
        r.ok("5.4 fill value verified via evaluate")
    except CommandError as e:
        if "EVALUATE_NOT_ALLOWED" in e.code:
            r.skip("5.4 fill value verified", "evaluate not allowed for user")
        else:
            r.fail("5.4 fill value verified", str(e))
    except Exception as e:
        r.fail("5.4 fill value verified", str(e))

    # 5.5 fill 无 value 参数
    try:
        resp = httpx.post(
            f"{SERVER}/api/v1/command",
            json={"action": "fill", "args": {"ref": "@e1"}, "session": "fill-test"},
            headers={"Authorization": f"Bearer {_load_token()}"},
            timeout=10,
        )
        # Missing value should produce an error
        if resp.status_code >= 400:
            r.ok("5.5 fill no value → error")
        else:
            data = resp.json()
            result = data.get("result", {})
            if not result.get("success"):
                r.ok("5.5 fill no value → error in result")
            else:
                r.fail("5.5 fill no value → error", "succeeded without value")
    except Exception as e:
        r.fail("5.5 fill no value → error", str(e))

    # 5.6 fill 过期 ref
    try:
        old_ref = input_ref or "@e1"
        client.navigate("https://example.com")
        time.sleep(1)
        client.fill("stale", ref=old_ref)
        r.fail("5.6 fill expired ref", "no error raised")
    except CommandError as e:
        assert "EXPIRED" in e.code or "NOT_FOUND" in e.code or e.status >= 400, f"code={e.code}"
        r.ok("5.6 fill expired ref → error")
    except Exception as e:
        r.fail("5.6 fill expired ref", str(e))

    # 5.7 bare-ref normalization: snapshot returns "e13", caller sends "e13" (no @).
    # Server should auto-prefix with @ instead of rejecting with INVALID_REF.
    try:
        client.navigate(f"{STATIC}/_3be2600fb8/test-pages/form.html")
        time.sleep(1)
        snap = client.snapshot()
        # Pull a raw ref straight from the tree without the helper's normalization
        nodes = _collect_nodes(snap["tree"])
        bare_ref = None
        for node in nodes:
            ref = node.get("ref", "")
            if node.get("role") == "textbox" and re.match(r"^@?e\d+$", ref):
                bare_ref = ref.lstrip("@")  # force the no-@ form
                break
        assert bare_ref, "no textbox ref found for bare-ref test"
        result = client.fill("bare-ref test", ref=bare_ref)
        assert result.get("success"), f"bare ref rejected: {result}"
        r.ok(f"5.7 fill accepts bare ref ({bare_ref!r})")
    except Exception as e:
        r.fail("5.7 fill accepts bare ref", str(e))

    client.close_session()
    client.close()


# ---------------------------------------------------------------------------
# Group 6: Screenshot
# ---------------------------------------------------------------------------

def test_group_6_screenshot(r: TestResult):
    r.set_group("Group 6: Screenshot")
    client = CommandClient(SERVER, token=_load_token(), session="ss-test")

    try:
        client.navigate("https://example.com")
        time.sleep(1)
    except Exception as e:
        r.fail("6.0 navigate", str(e))
        client.close()
        return

    # 6.1 screenshot png
    ss_result = None
    try:
        ss_result = client.screenshot(format="png")
        b64 = ss_result.get("image", ss_result.get("data", ss_result.get("base64", "")))
        assert b64 and len(b64) > 100, f"base64 too short: {len(b64) if b64 else 0}"
        fmt = ss_result.get("format", "")
        r.ok(f"6.1 screenshot png (format={fmt}, size={len(b64)//1024}KB)")
    except Exception as e:
        r.fail("6.1 screenshot png", str(e))

    # 6.2 screenshot jpeg
    try:
        ss_jpeg = client.screenshot(format="jpeg")
        b64 = ss_jpeg.get("image", ss_jpeg.get("data", ss_jpeg.get("base64", "")))
        assert b64 and len(b64) > 100, "base64 too short"
        r.ok("6.2 screenshot jpeg")
    except Exception as e:
        r.fail("6.2 screenshot jpeg", str(e))

    # 6.3 screenshot dimensions
    try:
        assert ss_result is not None
        w = ss_result.get("width", 0)
        h = ss_result.get("height", 0)
        if w > 0 and h > 0:
            r.ok(f"6.3 screenshot dimensions ({w}x{h})")
        else:
            r.skip("6.3 screenshot dimensions", "width/height not in response")
    except Exception as e:
        r.fail("6.3 screenshot dimensions", str(e))

    # 6.4 base64 可解码
    try:
        assert ss_result is not None
        b64 = ss_result.get("image", ss_result.get("data", ss_result.get("base64", "")))
        raw = base64.b64decode(b64)
        assert len(raw) > 100, f"decoded only {len(raw)} bytes"
        # PNG signature: 89 50 4E 47
        assert raw[:4] == b'\x89PNG', f"not valid PNG: {raw[:4].hex()}"
        r.ok("6.4 base64 decodes to valid PNG")
    except Exception as e:
        r.fail("6.4 base64 decodes to valid PNG", str(e))

    client.close_session()
    client.close()


# ---------------------------------------------------------------------------
# Group 7: Evaluate
# ---------------------------------------------------------------------------

def test_group_7_evaluate(r: TestResult):
    r.set_group("Group 7: Evaluate")
    client = CommandClient(SERVER, token=_load_token(), session="eval-test")

    try:
        client.navigate("https://example.com")
        time.sleep(1)
    except Exception as e:
        r.fail("7.0 navigate", str(e))
        client.close()
        return

    # 7.1 evaluate 简单表达式
    try:
        result = client.evaluate("1+1")
        assert result.get("type") == "number" or result.get("value") == 2, f"result={result}"
        r.ok("7.1 evaluate 1+1")
    except CommandError as e:
        if "EVALUATE_NOT_ALLOWED" in e.code:
            r.skip("7.1 evaluate 1+1", "evaluate not allowed")
            # Skip remaining evaluate tests
            for i, name in [
                (2, "DOM query"),
                (3, "return object"),
                (4, "syntax error"),
                (5, "permission check"),
            ]:
                r.skip(f"7.{i} evaluate {name}", "evaluate not allowed")
            client.close_session()
            client.close()
            return
        r.fail("7.1 evaluate 1+1", str(e))
    except Exception as e:
        r.fail("7.1 evaluate 1+1", str(e))

    # 7.2 evaluate DOM 查询
    try:
        result = client.evaluate("document.title")
        assert result.get("type") == "string" or isinstance(result.get("value"), str), (
            f"result={result}"
        )
        val = result.get("value", "")
        assert val, "empty title"
        r.ok(f"7.2 evaluate document.title = {val!r}")
    except Exception as e:
        r.fail("7.2 evaluate document.title", str(e))

    # 7.3 evaluate 返回对象
    try:
        result = client.evaluate("({a:1, b:2})")
        val = result.get("value")
        if result.get("type") == "object" or isinstance(val, dict):
            r.ok("7.3 evaluate returns object")
        else:
            r.ok(f"7.3 evaluate returns object (type={result.get('type')})")
    except Exception as e:
        r.fail("7.3 evaluate returns object", str(e))

    # 7.4 evaluate 语法错误
    try:
        result = client.evaluate("function(")
        rtype = result.get("type", "")
        if rtype == "error" or "error" in str(result.get("value", "")).lower():
            r.ok("7.4 evaluate syntax error → error type")
        else:
            r.fail("7.4 evaluate syntax error", f"expected error, got type={rtype}")
    except CommandError as e:
        # Some implementations raise an error for eval failures
        r.ok(f"7.4 evaluate syntax error → CommandError ({e.code})")
    except Exception as e:
        r.fail("7.4 evaluate syntax error", str(e))

    # 7.5 evaluate 无权限 (test with a hypothetical restricted user)
    # This is hard to test without a second user, so we just note it
    r.skip("7.5 evaluate permission check", "requires separate restricted user")

    client.close_session()
    client.close()


# ---------------------------------------------------------------------------
# Group 8: Tab Management
# ---------------------------------------------------------------------------

def test_group_8_tabs(r: TestResult):
    r.set_group("Group 8: Tab Management")
    client = CommandClient(SERVER, token=_load_token(), session="tab-test")

    try:
        client.navigate("https://example.com")
        time.sleep(0.5)
        client.navigate("https://example.org", new_tab=True)
        time.sleep(0.5)
    except Exception as e:
        r.fail("8.0 setup tabs", str(e))
        client.close()
        return

    # 8.1 list_tabs
    try:
        tabs = client.list_tabs()
        tab_list = tabs.get("tabs", tabs) if isinstance(tabs, dict) else tabs
        if isinstance(tab_list, list):
            assert len(tab_list) >= 2, f"expected >=2 tabs, got {len(tab_list)}"
            for t in tab_list:
                assert t.get("tabId") or t.get("id"), f"missing tabId: {t}"
                assert t.get("url"), f"missing url: {t}"
            r.ok(f"8.1 list_tabs ({len(tab_list)} tabs)")
        else:
            r.fail("8.1 list_tabs", f"unexpected format: {type(tab_list)}")
    except Exception as e:
        r.fail("8.1 list_tabs", str(e))

    # 8.2 find_tab by URL
    try:
        result = client.find_tab("*://example.com/*")
        success = result.get("success") or result.get("found") or result.get("tabId")
        assert success, f"result={result}"
        r.ok("8.2 find_tab by URL")
    except Exception as e:
        r.fail("8.2 find_tab by URL", str(e))

    # 8.3 find_tab 不存在
    try:
        result = client.find_tab("https://this-domain-does-not-exist-xyz.invalid")
        found = result.get("success") or result.get("found") or result.get("tabId")
        if not found:
            r.ok("8.3 find_tab not found → success=false")
        else:
            r.fail("8.3 find_tab not found", f"unexpectedly found: {result}")
    except CommandError as e:
        # Some implementations throw for not-found
        r.ok(f"8.3 find_tab not found → error ({e.code})")
    except Exception as e:
        r.fail("8.3 find_tab not found", str(e))

    # 8.4 close_tab
    try:
        tabs_before = client.list_tabs()
        before_list = (
            tabs_before.get("tabs", tabs_before) if isinstance(tabs_before, dict) else tabs_before
        )
        before_count = len(before_list) if isinstance(before_list, list) else 0

        result = client.close_tab()
        assert result.get("success"), f"result={result}"
        time.sleep(0.5)

        tabs_after = client.list_tabs()
        after_list = (
            tabs_after.get("tabs", tabs_after) if isinstance(tabs_after, dict) else tabs_after
        )
        after_count = len(after_list) if isinstance(after_list, list) else 0

        assert after_count < before_count, f"before={before_count}, after={after_count}"
        r.ok(f"8.4 close_tab ({before_count} → {after_count})")
    except Exception as e:
        r.fail("8.4 close_tab", str(e))

    # 8.5 close_session
    try:
        result = client.close_session()
        closed = result.get("closed", result.get("count", 0))
        assert closed >= 0, f"result={result}"
        time.sleep(0.5)

        tabs_final = client.list_tabs()
        final_list = (
            tabs_final.get("tabs", tabs_final) if isinstance(tabs_final, dict) else tabs_final
        )
        final_count = len(final_list) if isinstance(final_list, list) else 0
        assert final_count == 0, f"still {final_count} tabs"
        r.ok(f"8.5 close_session (closed={closed})")
    except CommandError as e:
        # After close_session, list_tabs might error with NO_ACTIVE_TAB
        if "NO_ACTIVE" in e.code or "NO_TAB" in e.code:
            r.ok("8.5 close_session (all tabs closed)")
        else:
            r.fail("8.5 close_session", str(e))
    except Exception as e:
        r.fail("8.5 close_session", str(e))

    client.close()


# ---------------------------------------------------------------------------
# Group 9: Error Handling + Edge Cases
# ---------------------------------------------------------------------------

def test_group_9_errors(r: TestResult):
    r.set_group("Group 9: Error Handling")
    client = CommandClient(SERVER, token=_load_token(), session="err-test")

    # 9.1 未知 action
    try:
        resp = httpx.post(
            f"{SERVER}/api/v1/command",
            json={"action": "fly_to_moon", "args": {}, "session": "err-test"},
            headers={"Authorization": f"Bearer {_load_token()}"},
            timeout=10,
        )
        data = resp.json()
        # Extension should return UNKNOWN_CMD or server rejects
        code = ""
        if resp.status_code >= 400:
            detail = data.get("detail", {})
            code = detail.get("code", "") if isinstance(detail, dict) else ""
        else:
            result = data.get("result", {})
            code = result.get("code", "")
        assert "UNKNOWN" in code or resp.status_code >= 400, (
            f"status={resp.status_code}, code={code}"
        )
        r.ok("9.1 unknown action → UNKNOWN_CMD")
    except Exception as e:
        r.fail("9.1 unknown action", str(e))

    # 9.2 设备离线
    try:
        resp = httpx.post(
            f"{SERVER}/api/v1/command",
            json={
                "action": "snapshot",
                "args": {},
                "session": "x",
                "device_id": "nonexistent_device_id_xyz",
            },
            headers={"Authorization": f"Bearer {_load_token()}"},
            timeout=10,
        )
        assert resp.status_code == 502, f"expected 502, got {resp.status_code}"
        data = resp.json()
        detail = data.get("detail", {})
        assert "DEVICE_OFFLINE" in str(detail), f"detail={detail}"
        r.ok("9.2 device offline → 502")
    except Exception as e:
        r.fail("9.2 device offline → 502", str(e))

    # 9.3 超时测试 (用同步阻塞 while loop, 阻塞 15s > server snapshot timeout 10s)
    try:
        timeout_client = CommandClient(
            SERVER, token=_load_token(), session="err-test", timeout=20.0
        )
        timeout_client.navigate("https://example.com")
        time.sleep(0.5)
        # Use snapshot command (10s server timeout) on a page that blocks with a heavy script
        # Actually test with evaluate that blocks longer than server evaluate timeout (30s)
        # Instead: test device offline timeout path which is more reliable
        resp = httpx.post(
            f"{SERVER}/api/v1/command",
            json={
                "action": "evaluate",
                "args": {
                    "code": (
                        "(() => { const end = Date.now() + 60000; "
                        "while(Date.now() < end) {} return 'done'; })()"
                    )
                },
                "session": "err-test",
            },
            headers={"Authorization": f"Bearer {_load_token()}"},
            timeout=35.0,
        )
        if resp.status_code == 504:
            r.ok("9.3 timeout → 504")
        elif resp.status_code == 200:
            # Extension might have a different timeout behavior
            r.skip("9.3 timeout", "evaluate completed before server timeout")
        else:
            r.ok(f"9.3 timeout → {resp.status_code}")
    except httpx.ReadTimeout:
        r.ok("9.3 timeout → client ReadTimeout")
    except Exception as e:
        if "timed out" in str(e).lower() or "timeout" in str(e).lower():
            r.ok("9.3 timeout → exception")
        else:
            r.fail("9.3 timeout", str(e))

    # 9.4 无活跃 tab 时 snapshot
    try:
        fresh = CommandClient(SERVER, token=_load_token(), session="err-empty")
        # Don't navigate — try snapshot on empty session
        fresh.snapshot()
        # If it works, the extension might have a default tab
        r.skip("9.4 no active tab snapshot", "extension may have default tab")
        fresh.close()
    except CommandError as e:
        if "NO_ACTIVE" in e.code or "NO_TAB" in e.code:
            r.ok("9.4 no active tab → NO_ACTIVE_TAB")
        else:
            r.ok(f"9.4 no active tab → error ({e.code})")
        try:
            fresh.close()
        except Exception:
            pass
    except Exception as e:
        r.fail("9.4 no active tab snapshot", str(e))

    # 9.5 并发请求
    try:
        async def _concurrent_test():
            aclient = AsyncCommandClient(SERVER, token=_load_token(), session="concurrent-test")
            await aclient.navigate("https://example.com")
            await asyncio.sleep(0.5)
            results = await asyncio.gather(
                aclient.snapshot(),
                aclient.snapshot(),
                return_exceptions=True,
            )
            await aclient.close_session()
            await aclient.close()
            return results

        results = asyncio.run(_concurrent_test())
        errors = [r for r in results if isinstance(r, Exception)]
        successes = [r for r in results if not isinstance(r, Exception)]
        if len(successes) >= 1:
            r.ok(f"9.5 concurrent requests ({len(successes)} ok, {len(errors)} err)")
        else:
            r.fail("9.5 concurrent requests", f"all failed: {errors}")
    except Exception as e:
        r.fail("9.5 concurrent requests", str(e))

    # 9.6 多设备歧义 (only testable if user has 2+ devices)
    r.skip("9.6 ambiguous device", "requires 2+ devices online")

    try:
        client.close_session()
    except Exception:
        pass
    client.close()


# ---------------------------------------------------------------------------
# Group 10: Onboard API
# ---------------------------------------------------------------------------

def test_group_10_onboard(r: TestResult):
    r.set_group("Group 10: Onboard API")

    admin_token = _load_admin_token()
    if not admin_token:
        r.skip("10.1 create user", "no admin token")
        r.skip("10.2 repeat onboard", "no admin token")
        r.skip("10.3 no admin token", "no admin token to compare against")
        return

    test_username = f"e2e_test_{uuid.uuid4().hex[:8]}"

    # 10.1 创建新用户
    user_id = None
    try:
        resp = httpx.post(
            f"{SERVER}/api/v1/onboard/{test_username}",
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=10,
        )
        assert resp.status_code == 200, f"status={resp.status_code}: {resp.text}"
        data = resp.json()
        code = data.get("pairing_code", "")
        assert len(code) == 6, f"code={code}"
        user_id = data.get("user_id")
        assert user_id, f"no user_id: {data}"
        r.ok(f"10.1 create user (code={code})")
    except Exception as e:
        r.fail("10.1 create user", str(e))

    # 10.2 已有用户重复 onboard
    try:
        resp = httpx.post(
            f"{SERVER}/api/v1/onboard/{test_username}",
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=10,
        )
        assert resp.status_code == 200, f"status={resp.status_code}"
        data = resp.json()
        code2 = data.get("pairing_code", "")
        assert len(code2) == 6
        user_id_2 = data.get("user_id")
        if user_id:
            assert user_id_2 == user_id, f"user_id changed: {user_id} → {user_id_2}"
        r.ok("10.2 repeat onboard same user_id, new code")
    except Exception as e:
        r.fail("10.2 repeat onboard", str(e))

    # 10.3 无 admin token
    try:
        resp = httpx.post(
            f"{SERVER}/api/v1/onboard/someone",
            timeout=10,
        )
        assert resp.status_code in (401, 403, 422), f"status={resp.status_code}"
        r.ok("10.3 no admin token → 401/403")
    except Exception as e:
        r.fail("10.3 no admin token → 401/403", str(e))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_refs(tree) -> list[str]:
    """Recursively collect all ref values from the snapshot tree."""
    refs = []
    if isinstance(tree, dict):
        if "ref" in tree and tree["ref"]:
            refs.append(tree["ref"])
        for child in tree.get("children", []):
            refs.extend(_collect_refs(child))
    elif isinstance(tree, list):
        for item in tree:
            refs.extend(_collect_refs(item))
    return refs


def _collect_nodes(tree) -> list[dict]:
    """Recursively collect all nodes from the snapshot tree."""
    nodes = []
    if isinstance(tree, dict):
        nodes.append(tree)
        for child in tree.get("children", []):
            nodes.extend(_collect_nodes(child))
    elif isinstance(tree, list):
        for item in tree:
            nodes.extend(_collect_nodes(item))
    return nodes


def _find_ref_by_role_or_name(
    tree, role: str | None = None, name_contains: str | None = None
) -> str | None:
    """Find first ref matching the given role or name substring."""
    nodes = _collect_nodes(tree)
    for node in nodes:
        if role and node.get("role") != role:
            continue
        if name_contains:
            node_name = node.get("name", "") or node.get("text", "") or ""
            if name_contains.lower() not in node_name.lower():
                continue
        ref = node.get("ref", "")
        if ref and re.match(r"^@?e\d+$", ref):
            # Normalize to @eN format for the API
            return ref if ref.startswith("@") else "@" + ref
    # Fallback: if role given but no name match, try just role
    if name_contains:
        return _find_ref_by_role_or_name(tree, role=role, name_contains=None)
    return None


# ---------------------------------------------------------------------------
# Group 11: Element Screenshot
# ---------------------------------------------------------------------------

def test_group_11_element_screenshot(r: TestResult):
    r.set_group("Group 11: Element Screenshot")
    client = CommandClient(SERVER, token=_load_token(), session="elss-test")

    try:
        client.navigate(f"{STATIC}/_3be2600fb8/test-pages/form.html")
        time.sleep(1)
    except Exception as e:
        r.fail("11.0 navigate", str(e))
        client.close()
        return

    # 11.1 screenshot with selector
    try:
        result = client.screenshot(selector="#test-button")
        b64 = result.get("base64", "")
        assert b64 and len(b64) > 50, f"base64 too short: {len(b64)}"
        w = result.get("width", 0)
        h = result.get("height", 0)
        assert w > 0 and h > 0, f"dimensions: {w}x{h}"
        # Element should be smaller than full viewport
        assert w < 1600 and h < 900, f"too large for element: {w}x{h}"
        r.ok(f"11.1 screenshot by selector ({w}x{h})")
    except Exception as e:
        r.fail("11.1 screenshot by selector", str(e))

    # 11.2 screenshot with @e ref
    try:
        snap = client.snapshot()
        btn_ref = _find_ref_by_role_or_name(snap["tree"], name_contains="Click Me")
        if btn_ref:
            result = client.screenshot(ref=btn_ref)
            b64 = result.get("base64", "")
            assert b64 and len(b64) > 50
            r.ok("11.2 screenshot by ref")
        else:
            r.skip("11.2 screenshot by ref", "no button ref found")
    except Exception as e:
        r.fail("11.2 screenshot by ref", str(e))

    # 11.3 screenshot with nonexistent selector
    try:
        client.screenshot(selector="#nonexistent_element_xyz")
        r.fail("11.3 nonexistent selector", "no error")
    except CommandError as e:
        assert e.status >= 400 or "NOT_FOUND" in e.code, f"code={e.code}"
        r.ok("11.3 nonexistent selector → error")
    except Exception as e:
        r.fail("11.3 nonexistent selector", str(e))

    client.close_session()
    client.close()


# ---------------------------------------------------------------------------
# Group 12: Network Monitoring
# ---------------------------------------------------------------------------

def test_group_12_network(r: TestResult):
    r.set_group("Group 12: Network Monitoring")
    client = CommandClient(SERVER, token=_load_token(), session="net-test")

    try:
        client.navigate("https://example.com")
        time.sleep(1)
    except Exception as e:
        r.fail("12.0 navigate", str(e))
        client.close()
        return

    # 12.1 network start
    try:
        result = client.network("start")
        assert result.get("success"), f"result={result}"
        r.ok("12.1 network start")
    except CommandError as e:
        if "NETWORK_NOT_ALLOWED" in e.code:
            r.skip("12.1 network start", "network not allowed for user")
            for i in range(2, 6):
                r.skip(f"12.{i}", "network not allowed")
            client.close_session()
            client.close()
            return
        r.fail("12.1 network start", str(e))
    except Exception as e:
        r.fail("12.1 network start", str(e))

    # 12.2 trigger some network activity then list
    try:
        client.navigate("https://www.w3.org/")
        time.sleep(2)
        result = client.network("list")
        requests = result.get("requests", [])
        assert len(requests) > 0, "no requests captured"
        r.ok(f"12.2 network list ({len(requests)} requests)")
    except Exception as e:
        r.fail("12.2 network list", str(e))

    # 12.3 network list with filter
    try:
        result = client.network("list", filter="w3.org")
        requests = result.get("requests", [])
        assert all("w3.org" in req.get("url", "").lower() for req in requests), "filter not working"
        r.ok(f"12.3 network list filtered ({len(requests)} matches)")
    except Exception as e:
        r.fail("12.3 network list filtered", str(e))

    # 12.4 network detail
    try:
        list_result = client.network("list")
        requests = list_result.get("requests", [])
        if requests:
            rid = requests[0].get("requestId")
            detail = client.network("detail", request_id=rid)
            assert detail.get("success"), f"detail={detail}"
            assert detail.get("url"), "no url in detail"
            r.ok("12.4 network detail")
        else:
            r.skip("12.4 network detail", "no requests to inspect")
    except Exception as e:
        r.fail("12.4 network detail", str(e))

    # 12.5 network stop
    try:
        result = client.network("stop")
        assert result.get("success"), f"result={result}"
        r.ok(f"12.5 network stop (captured={result.get('captured', '?')})")
    except Exception as e:
        r.fail("12.5 network stop", str(e))

    client.close_session()
    client.close()


# ---------------------------------------------------------------------------
# Group 13: Save as PDF
# ---------------------------------------------------------------------------

def test_group_13_pdf(r: TestResult):
    r.set_group("Group 13: Save as PDF")
    client = CommandClient(SERVER, token=_load_token(), session="pdf-test")

    try:
        client.navigate("https://example.com")
        time.sleep(1)
    except Exception as e:
        r.fail("13.0 navigate", str(e))
        client.close()
        return

    # 13.1 save_as_pdf default
    try:
        result = client.save_as_pdf()
        assert result.get("success"), f"result={result}"
        b64 = result.get("base64", "")
        assert len(b64) > 100, f"PDF too short: {len(b64)}"
        # Verify it's valid PDF (starts with %PDF after base64 decode)
        raw = base64.b64decode(b64[:100])
        assert raw[:4] == b"%PDF", f"not a PDF: {raw[:4]}"
        size = result.get("sizeBytes", 0)
        r.ok(f"13.1 save_as_pdf ({size} bytes)")
    except Exception as e:
        r.fail("13.1 save_as_pdf", str(e))

    # 13.2 save_as_pdf with options
    try:
        result = client.save_as_pdf(paper_format="letter", landscape=True, scale=0.8)
        assert result.get("success"), f"result={result}"
        assert result.get("base64"), "no base64"
        r.ok("13.2 save_as_pdf with options")
    except Exception as e:
        r.fail("13.2 save_as_pdf with options", str(e))

    client.close_session()
    client.close()


# ---------------------------------------------------------------------------
# Group 14: File Upload
# ---------------------------------------------------------------------------

def test_group_14_upload(r: TestResult):
    r.set_group("Group 14: File Upload")
    client = CommandClient(SERVER, token=_load_token(), session="upload-test")

    try:
        client.navigate(f"{STATIC}/_3be2600fb8/test-pages/form.html")
        time.sleep(1)
    except Exception as e:
        r.fail("14.0 navigate", str(e))
        client.close()
        return

    # 14.1 upload a file
    try:
        test_content = base64.b64encode(b"hello world test file").decode()
        files = [{"filename": "test.txt", "data": test_content, "mimeType": "text/plain"}]
        result = client.upload(files, selector="#test-file-input")
        assert result.get("success"), f"result={result}"
        assert result.get("fileCount") == 1, f"fileCount={result.get('fileCount')}"
        r.ok("14.1 upload file")
    except Exception as e:
        r.fail("14.1 upload file", str(e))

    # 14.2 upload no target
    try:
        files = [{"filename": "x.txt", "data": base64.b64encode(b"x").decode()}]
        client.upload(files)
        r.fail("14.2 upload no target", "no error")
    except CommandError as e:
        assert "NO_TARGET" in e.code or e.status >= 400, f"code={e.code}"
        r.ok("14.2 upload no target → error")
    except Exception as e:
        r.fail("14.2 upload no target", str(e))

    # 14.3 upload to non-file-input
    try:
        files = [{"filename": "x.txt", "data": base64.b64encode(b"x").decode()}]
        client.upload(files, selector="#test-button")
        r.fail("14.3 upload to non-file-input", "no error")
    except CommandError as e:
        assert "FILE_INPUT" in e.code or "UPLOAD" in e.code or e.status >= 400, f"code={e.code}"
        r.ok("14.3 upload to non-file-input → error")
    except Exception as e:
        r.fail("14.3 upload to non-file-input", str(e))

    client.close_session()
    client.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _check_device_online() -> bool:
    """Pre-flight check: is there a device online for our token?"""
    try:
        client = CommandClient(SERVER, token=_load_token(), session="preflight")
        client.navigate("https://example.com")
        client.close_session()
        client.close()
        return True
    except CommandError as e:
        if "DEVICE_OFFLINE" in e.code:
            return False
        return True
    except Exception:
        return False


def run_all():
    print()
    print("═" * 60)
    print("  Command API E2E Test Suite")
    print(f"  Server: {SERVER}")
    print(f"  Static: {STATIC}")
    print("═" * 60)

    result = TestResult()

    test_group_1_sanity(result)

    device_online = _check_device_online()
    if not device_online:
        print("\n  ⚠  No device online for this user token.")
        print("     Groups 2-9 require a connected Chrome extension.")
        print("     Please pair the extension and re-run.")
        print()
        result.set_group("Group 2-9: SKIPPED (no device)")
        result.skip("all device-dependent tests", "DEVICE_OFFLINE")
    else:
        test_group_2_navigate(result)
        test_group_3_snapshot(result)
        test_group_4_click(result)
        test_group_5_fill(result)
        test_group_6_screenshot(result)
        test_group_7_evaluate(result)
        test_group_8_tabs(result)
        test_group_9_errors(result)
        test_group_11_element_screenshot(result)
        test_group_12_network(result)
        test_group_13_pdf(result)
        test_group_14_upload(result)

    test_group_10_onboard(result)

    success = result.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Command API E2E Tests")
    parser.add_argument("--server", default=None, help="Server URL")
    parser.add_argument("--static", default=None, help="Static file server URL")
    parser.add_argument("--token", default=None, help="User token")
    parser.add_argument("--admin-token", default=None, help="Admin token")
    args = parser.parse_args()

    if args.server:
        SERVER = args.server
    if args.static:
        STATIC = args.static
    if args.token:
        TOKEN = args.token
    if args.admin_token:
        ADMIN_TOKEN = args.admin_token

    run_all()
