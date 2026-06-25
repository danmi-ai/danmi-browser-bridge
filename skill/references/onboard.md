# Onboarding: pair a new user's Mac to the bridge

Read this when the SKILL.md health check shows `connected_devices: 0`, when the user has no token file at `$BB_HOME/data/users/<username>.token`, or when the user explicitly asks to "install / configure / 绑定 / 配对" the bridge on a new Mac.

All paths in this doc are relative to the skill root. Run commands from there (or prefix with the skill's actual install path).

## Why onboarding exists

Danmi Browser Bridge is multi-user. Each Mac runs a Chrome extension that pairs to the server with a one-time **pairing code**, after which the server-side token (`bb_usr_...`) is written to `$BB_HOME/data/users/<username>.token`. Without that file you cannot drive that user's browser.

The agent's job during onboarding: get the user's Mac paired, then verify the device shows up online. The user's job: run two commands on their Mac and click through Chrome's "Load unpacked" UI.

## Decision tree

| Situation | Path |
|---|---|
| User has no entry in `$BB_HOME/data/users/` | [Flow A: brand-new user](#flow-a-brand-new-user) |
| Token file exists but `connected_devices: 0` | [Flow B: re-pair an existing user](#flow-b-re-pair-an-existing-user) |
| Token file exists, `connected_devices >= 1`, but it's the wrong user | Re-read SKILL.md Step 2 — pick the right username |

Check existing users with `ls "$BB_HOME/data/users/"`.

## Flow A: brand-new user

### A.1. Get the username

Default to `whoami` on the agent host. If the user explicitly named themselves differently (often their corporate ID, e.g. `alice`), use that.

### A.2. Issue a pairing code

Two equivalent ways to mint the code — pick whichever matches where you're running.

**Option A — over HTTP (no shell on the deploy host)**

```bash
USERNAME=<the-username>
ADMIN_TOKEN=$(cat "$BB_HOME/data/.admin_token")

curl -s -X POST "$BB_SERVER/api/v1/onboard/$USERNAME" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Response:

```json
{
  "server_url": "http://your-server:8404",
  "pairing_code": "A7K2Q9",
  "expires_at": "2026-06-05T14:30:00+00:00",
  "user_id": "..."
}
```

The pairing code is **single-use**, expires in ~30 minutes, and the server has already written `$BB_HOME/data/users/$USERNAME.token` — but the device isn't paired yet.

The `server_url` in the response reflects the server's current address, but the user doesn't need to type it — the extension popup auto-discovers it from the discovery anchor. You only need to hand the user the `pairing_code`.

**Option B — on-host CLI** (when you have shell access to the deploy host)

```bash
PY="$BB_HOME/.venv/bin/python"
(cd "$BB_HOME" && $PY -m server.cli create-pairing-code <username>)   # prints the 6-char code
```

This prints just the code and writes `$BB_HOME/data/users/<username>.token` for you, same as Option A. For a **brand-new user who doesn't exist yet**, create them first — `create-user` prints the user token:

```bash
(cd "$BB_HOME" && $PY -m server.cli create-user <name>)   # prints the user token, then run create-pairing-code
```

### A.3. Tell the user what to do on their Mac

Give the user this exact message — three steps, copy-paste friendly. Match the user's language (Chinese or English). **Substitute the actual `pairing_code` from the API response into the message before sending.** The install command and server address are fixed/auto-discovered, so the only per-user value is the pairing code.

> 在你的 Mac 上完成下面三步：
>
> **1. 安装扩展**（这条命令固定不变，服务器换地址也不影响）
>
> ```bash
> curl -sL <YOUR_INSTALL_URL> | bash
> ```
>
> 会下载扩展到 `~/Downloads/danmi-browser-bridge-extension/` 并自动打开 Chrome 的扩展页。
>
> **2. 加载扩展**
>
> 在 Chrome 的 `chrome://extensions` 页面：
> - 打开右上角的 **开发者模式**
> - 点击 **加载已解压的扩展程序**，选择 `~/Downloads/danmi-browser-bridge-extension/`
> - （更新已装的扩展：找到「丹秘 Browser Bridge」点刷新 🔄 即可，无需重选目录）
>
> **3. 配对**
>
> 点击工具栏里的丹秘扩展图标。服务器地址弹窗已自动填好，你只需填：
> - **配对码**: `<pairing_code>`（30 分钟内有效，单次使用）
>
> 点连接，弹窗显示绿色圆点就成功了，告诉我一声。

> The install command (`<YOUR_INSTALL_URL>`) and the server URL are both served
> from a static host / auto-discovered, so they never change with the server IP.
> Only the pairing code is per-user. The extension popup auto-fills the server
> address from the discovery anchor — the user doesn't type it.

### A.4. Verify the pairing

After the user confirms, re-run the health check:

```bash
curl -s $BB_SERVER/api/v1/health
```

Expected: `connected_devices` increments by 1. If it stays at 0:

- Code expired → re-issue (repeat A.2).
- User got an error in the popup → ask for a screenshot of the popup and check `references/operations.md` for the corresponding error code.
- Extension not loaded → tell the user to confirm Chrome shows "Danmi Browser Bridge" enabled in `chrome://extensions`.

Once the device is online, return to SKILL.md Step 2 and proceed with the user's task.

## Flow B: re-pair an existing user

The token file exists but the device is offline. Common causes: user reinstalled Chrome, removed the extension, switched Mac, or the extension's WS got blocked by VPN/firewall.

### B.1. Decide which one

Ask the user: "Did you remove the extension or switch Mac?"

- **No, the extension should still be there** — it's a network/runtime issue, not an onboarding issue. Ask them to: (a) click the extension popup and check the status text, (b) reload the extension on `chrome://extensions`, (c) verify they can reach `<server_url>/api/v1/health` from the Mac (use the actual URL, not the env var). Then re-check `connected_devices`. If still failing, see `references/operations.md`.
- **Yes / unsure** — proceed to B.2 to re-pair.

### B.2. Issue a fresh pairing code (token preserved)

```bash
USERNAME=<existing-user>
ADMIN_TOKEN=$(cat "$BB_HOME/data/.admin_token")

curl -s -X POST "$BB_SERVER/api/v1/onboard/$USERNAME" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

The admin endpoint is idempotent for existing users: it reuses `user_id` and the existing token, only minting a new `pairing_code`. The agent's `$BB_HOME/data/users/$USERNAME.token` stays valid.

### B.3. Send the install + pairing message

Same as Flow A.3. If the user already has the extension loaded, they can skip step 1 (curl install) and step 2 (Load unpacked) — only the popup re-pair (step 3) is needed.

### B.4. Verify

Same as Flow A.4.

## What "the agent should do" vs "the user should do"

- **Agent** runs on the Linux server, has admin token, calls `POST /onboard/<username>`, hands the user a pairing code, and verifies the device comes online.
- **User** runs on their Mac, runs `curl ... | bash`, loads the extension into Chrome, types the pairing code into the popup.

Don't try to ssh into the user's Mac, run AppleScript, or auto-open Chrome remotely — none of those work and they spook the user. Hand the user copy-paste commands and let them run them.

## Token file rotation

If a user's token file goes missing or you suspect it's compromised, rotate it:

```bash
# Force a fresh user record + new token (retires the old one)
"$BB_HOME/.venv/bin/python" "$BB_HOME/scripts/onboard.py" <username> --force-new
```

This is a destructive action — the old token immediately stops working. Confirm with the user before running it.
