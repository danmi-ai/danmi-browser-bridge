---
name: danmi-browser-bridge
description: |
  Danmi Browser Bridge lets AI control the user's real Chrome — navigate, click, type, read, screenshot, intercept network, and interact with any website using the user's actual login sessions. Use this skill whenever the user wants to interact with websites, automate browser tasks, scrape web content, or perform any action requiring a real browser. Also use when the user mentions "browser", "webpage", "open URL", "screenshot", or asks to read/interact with any website. Use even for simple-sounding browser requests — the server handles all complexity.
---

# Danmi Browser Bridge

Control the user's real Chrome (with their login sessions) via the Command API. The user's Mac runs a Chrome extension paired to a small bridge server; you call the server, the server forwards to the extension, the extension runs in the user's browser.

**This skill is written for single-user self-host** — one person runs the server on their own machine and pairs their own Chrome. That's the common case and the rest of this doc assumes it. Hosting for a team? See `references/multi-user.md`.

Doc-relative paths (e.g. `references/...`) are relative to **this skill's directory**. Anything that touches the running deployment — the server code, `config.toml`, `data/`, `scripts/` — lives in the **deployment root**, resolved once into `$BB_HOME`:

```bash
# Deployment root: holds config.toml, server/, data/, scripts/, .venv/.
#   1. explicit BB_HOME env wins
#   2. else, if you're already inside a checkout (server/ + config.toml in CWD), use it
#   3. else, the default self-host location the onboard playbook clones into
if [ -z "${BB_HOME:-}" ]; then
  if [ -d ./server ] && [ -f ./config.toml ]; then BB_HOME="$(pwd)"; else BB_HOME="$HOME/.danmi-browser-bridge"; fi
fi
```

## Server URL

Single-user self-host runs the server locally, so `$BB_SERVER` defaults to loopback. The port comes from `config.toml` (`[server].port`, default `8404`):

```bash
PORT=$("$BB_HOME/.venv/bin/python" -c "import sys;sys.path.insert(0,'$BB_HOME');from server.config import load_config;print(load_config('$BB_HOME/config.toml').server.port)" 2>/dev/null || echo 8404)
BB_SERVER="${BB_SERVER:-http://127.0.0.1:$PORT}"
```

If the server is somewhere else (a remote box, a reverse proxy), just `export BB_SERVER=http://<host>:<port>` and skip the server-management commands — those need shell access to the deployment host.

## Step 0 — First run (is the bridge even set up here?)

Before anything else, check whether the bridge has been onboarded on this machine:

```bash
curl -s $BB_SERVER/api/v1/health        # connection refused = no server running
ls "$BB_HOME"/data/users/*.token 2>/dev/null   # no match = no user token yet
```

If the server is **unreachable** (nothing listening) **or** there's **no user token** → the bridge hasn't been set up here yet. **Read `references/onboard.md`** — it clones + starts the server, mints your token and a pairing code, and walks the user through pairing their Chrome. Come back to Step 1 once `/api/v1/health` answers.

If health already returns `status:"ok"`, skip to Step 1.

## Step 1 — Health check (always first)

```bash
curl -s $BB_SERVER/api/v1/health
```

Then act on the result:

- **`status:"ok"` and `connected_devices >= 1`** — server is up and the user's Chrome is online. Continue to Step 2.
- **`status:"ok"` and `connected_devices: 0`** — server is up but no Chrome is paired. **Read `references/onboard.md`** to pair (or re-pair) the user's browser.
- **server unreachable / non-200** — server is down or not installed. **Read `references/onboard.md`** (first run) or **`references/operations.md`** (restart/diagnose an existing install).

Don't guess fixes here — every non-healthy state is handled in those references.

## Step 2 — Your token

Single-user self-host has **one** user: the owner of this machine. Onboarding created it (named `me` by default) and wrote its token to `$BB_HOME/data/users/me.token` by capturing the output of `server.cli create-user` (the CLI only prints the token — it doesn't write the file for you). Load it:

```bash
TOKEN=$(cat "$BB_HOME/data/users/me.token" 2>/dev/null)
# If you onboarded under a different username, point at that <username>.token instead.
```

If there's no token file, you haven't onboarded yet → go to `references/onboard.md`.

> **Hosting for a team?** → `references/multi-user.md`. The server enforces per-token device ownership — a token only ever drives *its own* owner's paired device (the explicit-`device_id` path is checked too; see AUTHZ-1) — so multi-user is safe, but you must pick the right per-user token rather than this single owner token.

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

- First run / no server / no token / `connected_devices: 0` / re-pair → `references/onboard.md`
- Server down / restart / permission flags / error codes / admin CLI → `references/operations.md`
- Tool quirks (snapshot, evaluate, fill, isTrusted, scrolling) → `references/tips.md`
- Hosting for several people → `references/multi-user.md`
