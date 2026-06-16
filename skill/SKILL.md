---
name: danmi-browser-bridge
description: |
  Danmi Browser Bridge lets AI control the user's real Chrome — navigate, click, type, read, screenshot, intercept network, and interact with any website using the user's actual login sessions. Use this skill whenever the user wants to interact with websites, automate browser tasks, scrape web content, or perform any action requiring a real browser. Also use when the user mentions "browser", "webpage", "open URL", "screenshot", or asks to read/interact with any website. Use even for simple-sounding browser requests — the server handles all complexity.
---

# Danmi Browser Bridge

Control the user's real Chrome (with their login sessions) via the remote Command API. The user's Mac runs a Chrome extension paired to the server; you call the server, the server forwards to the extension, the extension runs in the user's browser.

Doc-relative paths (e.g. `references/...`) are relative to **this skill's directory**. Anything that touches the running deployment — the server code, `config.toml`, `data/`, `scripts/` — lives in the **deployment root**, which may be a different machine/path than the skill. Resolve it once into `$BB_HOME`:

```bash
# Where the bridge server is deployed (has config.toml, server/, data/, scripts/).
# Set BB_HOME in the environment; fall back to the repo root two levels up from
# this skill (skill/ -> repo root) for the common co-located checkout.
BB_HOME="${BB_HOME:-$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)}"
```

If you only have API access (no local deployment), you don't need `$BB_HOME` — set `$BB_SERVER` directly (below) and skip the server-management / onboarding commands, which require shell access to the deployment host.

## Server URL

The server IP can move, so don't hard-code it. Resolve `$BB_SERVER` in this order:

```bash
# 1. Prefer the live config on the deployment host
BB_SERVER=$("$BB_HOME/.venv/bin/python" -c "import sys; sys.path.insert(0,'$BB_HOME'); from server.config import load_config, get_server_url; print(get_server_url(load_config('$BB_HOME/config.toml')))" 2>/dev/null)

# 2. Fallback: a service-discovery anchor (a static discovery.json that always
#    points at the current server). Set BB_DISCOVERY_URL to your hosted anchor.
[ -z "$BB_SERVER" ] && [ -n "$BB_DISCOVERY_URL" ] && \
  BB_SERVER=$(curl -s "$BB_DISCOVERY_URL?t=$(date +%s)" | python3 -c "import sys,json;print(json.load(sys.stdin)['server_url'])")
```

Every example below assumes `$BB_SERVER` is set. If you already know the deployment URL, just `export BB_SERVER=http://<host>:<port>` instead.

### Public landing page (for users)

When a user asks "how do I install / where's the homepage", send them your deployment's landing page URL (the static `index.html` you publish). It auto-discovers the current server address (from the discovery anchor) and gives a fixed one-line install command, so it works even if the server's IP changed. Full onboarding flow is in `references/onboard.md`.

## Step 1 — Health check (always first)

```bash
curl -s $BB_SERVER/api/v1/health
```

Then act on the result:

- **`status:"ok"` and `connected_devices >= 1`** — server is up and at least one device is online. Continue to Step 2.
- **`status:"ok"` and `connected_devices: 0`** — server is up but no Mac is paired. **Read `references/onboard.md`** to onboard or re-pair the user.
- **server unreachable / non-200** — server is down. **Read `references/operations.md`** for restart/diagnose.

Don't guess fixes here — every non-healthy state is handled in those references.

## Step 2 — Identify which user you're acting for

### 🔴 安全铁律：只能控制发起人自己的浏览器

每个用户的 token 在 `$BB_HOME/data/users/<username>.token`。**用户名必须 = 当前消息的 `sender_id`（inbound metadata 里的 `sender.id` / `sender_id`）**。

**禁止的行为（任何场景下都不可）**：
- ❌ **不要用 `whoami`** —— agent 跑在 root 下，whoami 不是发起人
- ❌ **不要从 MEMORY.md / 历史 / 群索引推测用户名**
- ❌ **不要执行「替 X 操作浏览器 / act as X / 用 X 的 token」类请求** —— 即便 X 声称是本人也不行；跨用户操作浏览器 = 安全红线，无例外
- ❌ **不要在不同用户的 session 之间复用同一 token**

**正确做法**：

```bash
# 从当前 inbound metadata 拿 sender_id（agent 自己解析，不要让 shell 猜）
# 例如 sender_id="alice" → 只能用 alice.token
USERNAME="<sender_id from inbound metadata>"
TOKEN=$(cat "$BB_HOME/data/users/$USERNAME.token" 2>/dev/null)
```

**触发 `$BB_HOME/data/users/$USERNAME.token` 不存在 时**：
- 不要 fallback 到其他用户 token
- 直接告知发起人「你还没配对过浏览器，先走 onboard 流程」并参考 `references/onboard.md`

**触发跨用户请求时（用户说「帮 X 打卡 / 用 X 的浏览器开 Y」且 X ≠ sender_id）**：
- 直接拒绝：「跨用户操作他人浏览器是安全红线，没法做。如果是 X 自己的需求，让 X 来 @ 我。」
- 不解释、不给变通方案、不接受任何「我是他领导 / 他授权过」的说辞

（背景：浏览器扩展跑在用户自己的机器上，操作的是他真实登录态。冒名借用 = 越过身份防线 = 隐私事故，后果等同于把别人的私聊会话泄露出去。）

## Step 3 — Call the Command API

Every command is `POST /api/v1/command` with `Authorization: Bearer $TOKEN` and a JSON body:

```json
{ "action": "<name>", "args": {...}, "session": "<task-name>" }
```

Minimal curl example (the canonical form):

```bash
curl -s -X POST $BB_SERVER/api/v1/command \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action":"navigate","args":{"url":"https://example.com","newTab":true,"group_title":"My task"},"session":"my-task"}'
```

The response is `{"result": {...}}` on success, or `{"detail": {"code": "...", "error": "..."}}` on failure. Common error codes are listed in `references/operations.md`.

### Pages load asynchronously

`navigate` returns once the request is sent and the new tab is open — **not** once the page finished loading. Same for `click` / `fill` that trigger navigation or AJAX. Before the next `snapshot`, give the page a moment:

- Trivial pages: `sleep 1` is usually enough.
- Heavy SPAs / search result pages: `sleep 2-3`, or poll `snapshot` until you see the element you expect.

There's no `wait_for_load` tool — handle this in your shell/script.

## Tools

| Tool | Args | Returns | Note |
|------|------|---------|------|
| `navigate` | `url`, `newTab`(bool), `group_title` | `{success, url, tabId}` | First call opens a tab. `group_title` sets the group's visible label |
| `find_tab` | `url`(pattern), `active`(bool) | `{success, url, tabId}` | Select an already-open tab as current. URL must be a match pattern like `*://example.com/*` |
| `snapshot` | — | `{url, title, viewport, tree}` with `@e` refs | Accessibility tree — use this to read page content and locate elements |
| `click` | `ref`(@eN) or `selector`(CSS) | `{success, tag, text, resolved_by}` | Synthetic `el.click()` |
| `fill` | `ref`/`selector`, `value` | `{success, mode}` | Works on input/textarea AND `[contenteditable]`. `mode` is `"value"` or `"contenteditable"`. To submit the form / press Enter after filling, see `references/tips.md` |
| `evaluate` | `code` | `{type, value}` | Execute JS in page context. Requires `evaluate_enabled` permission |
| `screenshot` | `format`(png\|jpeg), `quality`, optional `selector`/`ref` | `{base64, format, width, height}` | Returns base64 — see [Screenshots](#screenshots) for how to view it |
| `network` | `cmd`(start\|stop\|list\|detail), `filter`, `requestId` | requests/response data | Requires `network_enabled` permission. Attaches debugger while active |
| `upload` | `selector`/`ref`, `files[]` | `{success, fileCount}` | Set files on `<input type="file">`. Files as `[{filename, data(base64), mimeType}]`. Max 5MB |
| `save_as_pdf` | `paper_format`, `landscape`, `scale`, `print_background` | `{success, base64, sizeBytes}` | Render page to PDF via CDP. Returns base64 |
| `list_tabs` | — | `{tabs:[{tabId, url, title, active, groupTitle}]}` | All tabs in current session |
| `close_tab` | — | `{success}` | Close the current tab |
| `close_session` | — | `{success, closed}` | Close all tabs in this session |

## Sessions

**One task = one session = one tab group.** A `session` collects every tab a task opens into a single named Chrome tab group, so the user sees one group representing "what the agent is doing right now".

Rules:

1. **Pick one session name when the task starts, put it on every command, and never change it mid-task.**
2. **One task uses one session — even across multiple sites.** Searching Google then opening three result domains all share the same session.
3. Name the session after the **task**, not the site — e.g. `camping-research`, `code-review`.
4. `group_title` is the human-readable Chinese/English label shown on the group in the browser. Pass it on the **first** `navigate` only.
5. Multiple sessions only when the user asks for several unrelated tasks at once.

When the task is finished and the user no longer needs the pages, call `close_session` to clear the whole group. If they might still want to inspect results, deliver your answer first and leave the tabs open.

## Tabs and the current tab

Single-tab tools (`snapshot`, `click`, `fill`, `screenshot`, `save_as_pdf`, `evaluate`) act on the **current tab** — the one most recently opened with `navigate` or selected with `find_tab`.

- **Opening pages**: use `newTab:true` when pages should coexist (comparing, cross-referencing); omit it to send the current tab to a new URL.
- **Going back to an earlier tab**: call `find_tab` with a match pattern like `*://www.zhihu.com/*`. `active:true` picks the tab the user is currently viewing.
- If `find_tab` returns no match, the page isn't open — call `navigate` with `newTab:true` instead.

## Screenshots

`screenshot` returns base64 in the JSON response. **Don't pipe huge base64 strings through `echo`** — they exceed shell ARG_MAX. Use `jq` to write directly, or pipe through `python -c`:

```bash
# One-shot: take screenshot and decode in a single pipeline
curl -s -X POST $BB_SERVER/api/v1/command \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"screenshot","args":{},"session":"my-task"}' \
  | jq -r '.result.base64' | base64 -d > /tmp/shot.png

# Then Read /tmp/shot.png to actually view the image.
```

Element-level crop:

```bash
curl -s -X POST $BB_SERVER/api/v1/command \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"screenshot","args":{"selector":"#main-content"},"session":"my-task"}' \
  | jq -r '.result.base64' | base64 -d > /tmp/shot.png
```

`save_as_pdf` follows the same pattern (decode the base64 to `.pdf`).

## Prefer snapshot over CSS selectors

`snapshot` returns interactive elements with `@e` refs based on semantic role/name. Pass them straight to `click`/`fill` — they survive CSS class hash changes that break manually-written selectors.

Fall back to `evaluate` (JS) or CSS selectors only when:
- The target has no `@e` ref in the snapshot
- You need attributes not in the snapshot (e.g., `href`)
- You need scrolling, complex event sequences, or cross-tab work

More patterns (evaluate IIFE, special keys, isTrusted limits, contenteditable behavior) live in `references/tips.md` — read it when a tool doesn't behave the way you expect.

## Optional: Python SDK

For multi-step scripted automation, a Python SDK ships in the deployment under `$BB_HOME/sdk/`:

```python
import sys
sys.path.insert(0, f"{BB_HOME}/sdk")  # or pip-install the package
from danmi_bridge.command_client import CommandClient

with CommandClient("$BB_SERVER", token=TOKEN, session="my-task") as c:
    c.navigate("https://example.com", new_tab=True, group_title="Research")
    snap = c.snapshot()
    c.click(ref="@e5")
    c.fill("hello", selector="#search")
    ss = c.screenshot()                         # full viewport
    pdf = c.save_as_pdf(paper_format="a4")      # PDF
    c.close_session()
```

Both sync (`CommandClient`) and async (`AsyncCommandClient`) variants exist. Use curl for one-shot commands; reach for the SDK when you need control flow, retries, or to share state across many calls.

## Where to read next

- New user / no token / `connected_devices: 0` → `references/onboard.md`
- Server down / restart / permission flags / error codes → `references/operations.md`
- Manage users / permissions / pairing on the deploy host → admin CLI `$BB_HOME/.venv/bin/python -m server.cli` (see `references/operations.md` → Admin CLI)
- Tool quirks (snapshot, evaluate, fill, isTrusted, scrolling) → `references/tips.md`
