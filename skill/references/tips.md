# Tips: tool quirks and patterns

Read this when a tool doesn't behave the way you expect, when you're picking between `snapshot` / `evaluate` / `selector`, or when a click/fill silently fails.

All paths in this doc are relative to the skill root.

## Prefer `snapshot` over CSS / JS selectors

`snapshot` returns the page's accessibility tree with `@e` refs based on semantic role + name (button "Search", textbox "Email", link "Sign in"). These refs are stable across CSS class hash changes and minor DOM rewrites — manually-written `.css-xK7Pq` selectors are not.

Workflow:

1. Call `snapshot`.
2. Find the element you want in the tree (text matches the page's visible label).
3. Pass its `ref` (`"@e7"` etc.) to `click`/`fill`/`screenshot`.

Fall back to CSS selectors or `evaluate` only when:

- The target has no `@e` ref (decorative-only DOM, custom canvas widgets).
- You need attributes the tree doesn't expose — e.g. `href` for link extraction, `data-*` for bookkeeping.
- You need scrolling, drag, complex multi-event sequences, or to read a value out of the DOM.

`@e` refs are **session-scoped to the latest snapshot**. After a `navigate`, `click`, `fill`, or any user-side DOM mutation, refs from the previous snapshot may be invalid (`INVALID_REF` error). Re-call `snapshot` whenever you suspect the DOM changed.

## `fill` — three input shapes, one tool

`fill` auto-detects the target type and uses the right strategy:

| Target | Strategy | Returned `mode` |
|---|---|---|
| `<input>` / `<textarea>` | Sets `.value` via the native setter, fires `input` + `change` | `"value"` |
| `[contenteditable]` (ProseMirror, Lexical, Slate, TipTap, Quill, Notion editor, 知乎编辑器, Slack message box, etc.) | Focuses, selects all, calls `document.execCommand('insertText', value)` which fires `beforeinput` + `input` with `inputType:"insertText"`, `data:value` | `"contenteditable"` |
| Other element | Best-effort `.value` + events | `"value"` |

`fill` is **clear-and-insert** — existing content is replaced. To append, read the current value via `evaluate`, concatenate, then `fill` with the result.

If `fill` returns `success:true` but the page didn't update visibly, the framework may swallow `execCommand`. Workarounds:

- For React-controlled inputs: dispatch a synthetic `InputEvent` with `bubbles:true` after `fill`.
- For frameworks that listen to `keypress`: use `evaluate` to dispatch `KeyboardEvent`s (see "Special keys" below).

## Form submit / special keys

There's no separate "press key" tool. Two common needs:

**Submit a form** — click the submit button:

```bash
# Get the button from snapshot, then:
{"action":"click","args":{"ref":"@e42"},"session":"..."}
```

**Send a key like Enter / Escape / Tab** — use `evaluate`:

```js
document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {
  key: 'Escape', code: 'Escape', bubbles: true
}))
```

For `key: 'Enter'` to submit a form, prefer clicking the submit button — many pages listen for `keydown` on the form, not the input, and the bubble target may not match.

**If there's no visible submit button** (search bars that submit on Enter only), call the form's submit directly:

```js
(() => { const f = document.activeElement.closest('form'); if (f) f.submit(); })()
```

Or use `requestSubmit()` (fires `submit` event so validators / interceptors run):

```js
(() => { const f = document.activeElement.closest('form'); if (f) f.requestSubmit(); })()
```

For Baidu/Google-style search bars, `requestSubmit()` is usually correct — `submit()` skips the page's onsubmit handler.

## `evaluate` patterns

### IIFE for fresh scope

`evaluate` calls share the page's JS realm. Re-declaring `const`/`let` across two calls throws `SyntaxError: Identifier 'x' has already been declared`. Wrap in an IIFE every time:

```js
(() => { const rows = document.querySelectorAll('.row'); return [...rows].map(r => r.innerText); })()
```

### Compact JSON, not pretty

Always use `JSON.stringify(data)` — never `JSON.stringify(data, null, 2)`. Indentation can inflate the response several times over and trigger truncation during the WS round-trip.

### Async / await

Async is supported — wrap the body in `(async () => {...})()`:

```js
(async () => {
  const r = await fetch('/api/data');
  return await r.json();
})()
```

The result must be JSON-serialisable. DOM nodes, functions, and circular refs throw `EVALUATE_FAILED`.

### Reading attributes the snapshot doesn't expose

```js
(() => [...document.querySelectorAll('a')].map(a => ({text: a.innerText, href: a.href})))()
```

## Scrolling

`snapshot` only includes elements currently in the viewport (or close to it). To reach below-fold content:

```js
// Scroll a specific element into view
document.querySelector('#footer').scrollIntoView({block: 'center'})

// Page-down
window.scrollBy({top: window.innerHeight, behavior: 'instant'})

// To the bottom
window.scrollTo(0, document.body.scrollHeight)
```

After scrolling, re-call `snapshot` to get refs for the now-visible elements.

## Screenshots — viewing the file

`screenshot` returns base64 in JSON. **Don't store it in a shell variable** — base64 of even a viewport screenshot is hundreds of KB and triggers ARG_MAX issues when echoed. Pipe directly:

```bash
curl -s -X POST $BB_SERVER/api/v1/command \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"screenshot","args":{},"session":"my-task"}' \
  | jq -r '.result.base64' | base64 -d > /tmp/shot.png
# Then use the Read tool on /tmp/shot.png to view it
```

Element-level crop:

```json
{"action":"screenshot","args":{"selector":"#main-content"},"session":"my-task"}
{"action":"screenshot","args":{"ref":"@e12"},"session":"my-task"}
```

JPEG is smaller; use `format:"jpeg"` and `quality:60` for previews when the visual is just for orientation, not detail.

## `save_as_pdf`

Same base64 round-trip as screenshot. Decode to a `.pdf`:

```bash
echo "$RESPONSE" | jq -r '.result.base64' | base64 -d > /tmp/page.pdf
```

Pages that produce >10MB PDFs may time out. Reduce `scale` (try `0.8`) or split into multiple smaller pages first.

## Network monitoring

`network start` attaches Chrome's debugger and begins capture. Chrome shows a yellow "debugging" bar to the user while active — it disappears when you call `network stop`. Always pair start/stop, and stop as soon as you have the data you need:

```python
client.network("start")
client.navigate("https://api.example.com")  # do whatever generates the requests
reqs = client.network("list", filter="api.example.com")
detail = client.network("detail", request_id=reqs["requests"][0]["requestId"])
client.network("stop")  # detach immediately
```

`list` returns `[{requestId, url, method, status, size, mimeType}]`. `detail` adds full request/response headers + body. Bodies are only retrievable while monitoring is active — call `detail` before `stop`.

## isTrusted limits

Some sites strictly check `event.isTrusted` and reject `click` / `fill` because both go through synthetic DOM events (`isTrusted=false`). This affects:

- Banking portals during high-risk operations (transfer, password change)
- Captchas (reCAPTCHA, hCaptcha, Cloudflare Turnstile)
- A few payment widgets

This is a product boundary, not a bug — no automation primitive that runs on the user's machine without stealing OS focus can produce trusted events here. Tell the user: "this site requires you to do that step manually".

## Cross-origin iframes

`snapshot` / `click` / `fill` / `evaluate` operate on the **top frame** only. If the target lives in a same-page cross-origin iframe (embedded sandbox demos, some payment widgets), you can't reach it from the parent.

Workaround: read the iframe's `src` attribute (via `evaluate` on the parent), then `navigate` directly to that URL in a new tab. Some interactions are still blocked because the original page sets up postMessage protocols you'd have to replay — those are unrecoverable without app-specific code.

## Reading large pages

`snapshot` tree can grow huge on infinite-scroll pages or apps with thousands of nodes. The server caps the response, but even within the cap, scanning the tree wastes tokens.

Strategies:

- **Crop**: pass `selector` to `screenshot` for visual output, or use `evaluate` to query just the section you need.
- **Filter in-page**: `evaluate` with a query that returns only the rows/cards you care about.
- **Paginate**: many "infinite scroll" pages have a real paging URL — find it via the network panel, then `navigate` to specific pages.

## When in doubt

If a tool is misbehaving and the cause isn't obvious from this doc, check the server logs (`references/operations.md` → Logs section). The extension also logs to Chrome's DevTools console for the popup's service-worker — the user can check it if you ask.
