# Operations: server lifecycle, permissions, error codes

Read this when SKILL.md health check shows the server unreachable, when commands return errors with codes you don't recognise, or when the user asks about evaluate/network permissions, restarting, or inspecting logs.

Server-management commands run on the **deployment host** and use `$BB_HOME` (the deployment root, resolved in SKILL.md). Doc-relative paths like `references/...` are relative to the skill directory. If you only have API access (no `$BB_HOME`), you can't run these — they need shell access to the deployment.

The server's address is resolved into `$BB_SERVER` in SKILL.md (from `config.toml` or the discovery anchor, never a hard-coded IP).

## Server lifecycle

The server is managed via `$BB_HOME/scripts/ctl.py`:

```bash
PY="$BB_HOME/.venv/bin/python"
CTL="$BB_HOME/scripts/ctl.py"

$PY $CTL status        # process + port + /api/v1/health JSON
$PY $CTL start         # nohup uvicorn server.main; idempotent
$PY $CTL stop          # SIGTERM; broadcasts shutdown_notice to extensions
$PY $CTL restart       # stop && start
$PY $CTL logs 80       # tail last 80 lines of logs/server.log
$PY $CTL list-users    # all users
$PY $CTL list-devices  # all devices + last_seen_at
$PY $CTL list-sessions # active sessions only
```

> `list-*` delegate to `server.cli` — see [Admin CLI](#admin-cli) below; `ctl.py`'s own job is lifecycle.

`$PY` (the deployment venv) is required because `ctl.py` imports `tomli`/`server.*` packages installed there.

## Admin CLI

`server.cli` is the **single source of truth for reads** (user / device / session inspection) plus user, device, and permission management and quick stats — the canonical surface beyond what `ctl.py` exposes (whose `list-*` are convenience aliases). Run it **from `$BB_HOME`** so `server.config.load_config()` finds `./config.toml` and the right DB:

```bash
PY="$BB_HOME/.venv/bin/python"
(cd "$BB_HOME" && $PY -m server.cli list-users)
```

Equivalently, set `BB_CONFIG="$BB_HOME/config.toml"` to point at the config without the `cd`. The `--json` flag forces machine-readable output and is accepted either before or after the subcommand (e.g. `$PY -m server.cli --json list-users` or `$PY -m server.cli list-users --json`). Output is also auto-JSON when stdout is not a TTY (piped).

Key subcommands:

| Subcommand | What it does |
|---|---|
| `list-users` | All users (`--active-only` to hide revoked) |
| `show-user <username>` | One user's detail — devices, recent sessions, active pairing codes |
| `list-devices` | All devices + `last_seen_at` (`--user <username>` to filter) |
| `list-sessions` | Active sessions (`--all` includes closed) |
| `list-pairing-codes` | Active pairing codes (`--all` includes used/expired) |
| `stats` | Quick deployment counts — users / devices / sessions / commands |
| `create-pairing-code <username>` | Issue a new pairing code for a user |
| `grant-evaluate <username>` / `grant-network <username>` | Enable a permission — see [Permissions](#permissions) |
| `grant-evaluate <username> --disable` / `revoke-network <username>` | Disable a permission (there is no `revoke-evaluate`) |
| `revoke-user <username>` | Revoke a user and all their devices |
| `revoke-device <device_id>` | Revoke a single device |

## Routing table — what to do based on health

Run: `curl -s $BB_SERVER/api/v1/health`

| Observed | Action |
|---|---|
| `curl: (7) Failed to connect ...` | Server down. `$PY $CTL start`; if it errors, check logs (`$PY $CTL logs 100`) for `[error]` / port conflict / DB lock |
| HTTP 500 from `/health` | Server up but degraded. Logs will say why — usually DB stuck or extension WS hub crashed. `$PY $CTL restart` |
| `status:"ok"`, `connected_devices:0` | Server fine, no Mac paired. See `references/onboard.md` |
| `status:"ok"`, `connected_devices:>=1` | Healthy. Return to `SKILL.md` Step 2 |

## Common failures during commands

| Symptom | Likely cause | Action |
|---|---|---|
| `start` fails: "address already in use" | Stale process on `:8404` | `$PY $CTL stop`, then `start`; if stop fails, `lsof -i :8404` to find the conflicting PID |
| Tool calls time out | Page slow, JS-heavy, or network blocked in browser | `$PY $CTL logs 100` for `[error]` / `panic`; if recent, `restart`. Otherwise the page itself is the problem — try a shorter operation |
| `connected_devices` drops to 0 mid-task | User's WS dropped (Mac sleep, VPN switch) | Wait 5–10s for auto-reconnect. If not back, ask user to check the popup / reload the extension |
| Yellow "debugging" bar stuck in user's Chrome | `network start` left dangling | Send `network stop` (in any session that's still authoritative) |

## Error codes from the Command API

When `POST /api/v1/command` returns non-2xx, the body is `{"detail": {"code": "...", "error": "..."}}`. Categorise to decide next step:

### Server-level (HTTP 4xx/5xx)

| Code | HTTP | Meaning | What to do |
|---|---|---|---|
| `DEVICE_OFFLINE` | 502 | No paired device for this user, or the device's WS dropped | Run health check; if 0 devices, see `references/onboard.md`. If 1+, the user's extension just disconnected — ask them to reload it |
| `AMBIGUOUS_DEVICE` | 400 | User has multiple paired devices, command didn't specify which | Pass `device_id` explicitly. List with `$PY $CTL list-devices` |
| `RATE_LIMITED` | 429 | Hit per-session command rate limit (default 2000 / 60s) | Slow down or split into multiple sessions. Limits live in `config.toml` `[server.limits]` |
| `INVALID_URL` | 400 | URL didn't parse | Fix the URL passed to `navigate` |
| `SCHEME_NOT_ALLOWED` | 400 | URL scheme blocked (e.g. `file://`, `chrome-extension://`) | Use `http(s)://` |
| `EVALUATE_NOT_ALLOWED` | 403 | User lacks evaluate permission, or domain not on allow-list | See [Permissions](#permissions) below |
| `NETWORK_NOT_ALLOWED` | 403 | User lacks network permission | See [Permissions](#permissions) below |
| `UPLOAD_TOO_LARGE` | 400 | File >5MB after base64 encode | Shrink the file or chunk uploads |
| `TIMEOUT` | 504 | Extension didn't respond within the per-action timeout | Page is slow / blocked by JS. Retry or break into smaller steps |

### Extension-level (the action ran but failed inside the page)

These come back from the extension via the server with HTTP 200 + `{success:false, code:...}` or wrapped inside command errors:

| Code | Meaning | What to do |
|---|---|---|
| `NO_ACTIVE_TAB` | This session has no current tab | Call `navigate` first to open one |
| `NO_TARGET` | `click`/`fill`/`screenshot` got neither `ref` nor `selector` | Pass one |
| `INVALID_REF` | The `@e` ref isn't in the latest snapshot | Re-call `snapshot`, refs are invalidated when the DOM changes |
| `INVALID_PARAMS` | Args didn't match the action's schema | Re-check args against `SKILL.md` Tools table |
| `SNAPSHOT_FAILED` | A11y tree extraction errored | Try again; if persistent, the page may have hostile DOM. Fall back to `evaluate` |
| `CLICK_FAILED` / `FILL_FAILED` | Element resolved but the synthetic event was rejected | Often `event.isTrusted` blocking — see `references/tips.md` "isTrusted limits" |
| `EVALUATE_FAILED` | JS threw or returned non-serialisable value | Read the `error` field. Wrap in IIFE if it's a redeclaration |
| `NETWORK_ALREADY_ACTIVE` / `NETWORK_NOT_ACTIVE` | `network start` while already capturing, or `stop` while not | Idempotent design; check current state with `network list` |

## Permissions

Two permissions are off by default. Both are per-user and stored in the SQLite DB at `$BB_HOME/data/browser_bridge.db`.

Prefer the [Admin CLI](#admin-cli) for all changes — it resolves the user, writes via the shared admin ops (with audit logging), and prints the resulting row. Run it from `$BB_HOME` so `load_config()` finds the right config and DB:

```bash
PY="$BB_HOME/.venv/bin/python"
```

### evaluate

Lets the user run `evaluate` to execute arbitrary JS in the page.

```bash
# enable for all domains
(cd "$BB_HOME" && $PY -m server.cli grant-evaluate <username> --domains '*')

# restrict to one origin (allowlist semantics below)
(cd "$BB_HOME" && $PY -m server.cli grant-evaluate <username> --domains 'https://*.example.com/*')

# disable
(cd "$BB_HOME" && $PY -m server.cli grant-evaluate <username> --disable)
```

`evaluate_domains` is a comma-separated list of URL patterns. `*` allows everything; `https://*.example.com/*` restricts to that origin.

### network

Lets the user run `network start/stop/list/detail`. While capturing, Chrome shows a yellow "debugging" bar.

```bash
# enable
(cd "$BB_HOME" && $PY -m server.cli grant-network <username>)

# disable
(cd "$BB_HOME" && $PY -m server.cli revoke-network <username>)
```

### Verifying

```bash
(cd "$BB_HOME" && $PY -m server.cli show-user <username>)
```

Permission changes take effect on the next command — no server restart needed.

### Fallback: direct SQL (when CLI is unavailable or you're inspecting the DB)

If the venv/CLI isn't reachable, or you just want to read the raw rows, hit the DB directly. **Caution:** these match by `name`, so they affect *every* row sharing that name — prefer the CLI (which resolves the most-recent user) for writes.

```bash
# enable evaluate
sqlite3 "$BB_HOME/data/browser_bridge.db" \
  "UPDATE users SET evaluate_enabled=1, evaluate_domains='*' WHERE name='<username>'"

# enable network
sqlite3 "$BB_HOME/data/browser_bridge.db" \
  "UPDATE users SET network_enabled=1 WHERE name='<username>'"

# verify
sqlite3 "$BB_HOME/data/browser_bridge.db" \
  "SELECT name, evaluate_enabled, evaluate_domains, network_enabled FROM users WHERE name='<username>'"
```

## Logs

```bash
$PY $CTL logs 200    # last 200 lines

# Or read directly:
tail -n 200 "$BB_HOME/logs/server.log"
tail -f "$BB_HOME/logs/server.log"        # follow live
```

Each log line is a JSON object — `jq` makes it readable:

```bash
tail -n 50 "$BB_HOME/logs/server.log" | jq 'select(.level == "error")'
tail -n 200 "$BB_HOME/logs/server.log" | jq 'select(.event == "command_error")'
```

Audit trail (every command + result hash) lives in the DB:

```bash
sqlite3 "$BB_HOME/data/browser_bridge.db" \
  "SELECT created_at, action, code FROM audit_log ORDER BY created_at DESC LIMIT 20"
```

## Server config

`config.toml` is the prod config. Keys you'll most often touch:

- `[server.timeouts]` — per-action timeouts (in seconds)
- `[session]` — `idle_timeout`, `max_lifetime`, `max_per_device`
- `[server.limits]` — rate limits (default 2000 commands / 60s / session)
- `[pairing]` — pairing-code length and TTL

Config changes need `restart` to take effect.

## When to restart vs not

**Safe to restart anytime** — extensions auto-reconnect within a few seconds, and in-flight tab groups stay open in the user's Chrome. Sessions are recreated on demand.

**Don't restart while a critical operation is in flight** for the user (e.g., they're watching the agent fill a long form). The command in flight will fail with `TIMEOUT` or `DEVICE_OFFLINE` and the agent will need to re-snapshot and resume.
