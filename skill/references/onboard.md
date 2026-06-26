# Onboarding: stand up the bridge and pair the user's Chrome

Read this when:

- the SKILL.md Step 0 / health check shows the **server is unreachable** (nothing installed/running here yet), or
- health is `ok` but `connected_devices: 0` (server up, no Chrome paired), or
- there's **no user token** at `$BB_HOME/data/users/*.token`, or
- the user asks to "install / configure / 绑定 / 配对 / onboard" the bridge.

Everything here is **colocated on the user's own machine**: the bridge server, this skill, and the agent all run on the same box, and the user's Chrome pairs to `127.0.0.1`. There is no central server and no SSH. (Hosting for several people instead? See `references/multi-user.md`.)

`$BB_HOME` is the deployment root (resolved in SKILL.md). The default for a fresh self-host is `$HOME/.danmi-browser-bridge`.

## Decision tree

| Situation | Flow |
|---|---|
| `/api/v1/health` refuses to connect — no server here yet | [Flow A: first run](#flow-a-first-run) |
| Server up, token exists, but `connected_devices: 0` (device offline) | [Flow B: re-pair](#flow-b-re-pair) |
| Token exists, `connected_devices >= 1` | Already onboarded — return to SKILL.md Step 2 |

## Flow A: first run

Run these on the user's machine. `$PY` is the deployment's venv python.

### A.0. Get the server

If `$BB_HOME` doesn't exist yet, clone it:

```bash
[ -d "$BB_HOME" ] || git clone https://github.com/danmi-ai/danmi-browser-bridge "$BB_HOME"
```

(Skip if you're already running inside a checkout — then `$BB_HOME` is that checkout.)

### A.1. Bootstrap (venv + install + admin token)

```bash
python3 -m venv "$BB_HOME/.venv"
"$BB_HOME/.venv/bin/pip" install -e "$BB_HOME"
PY="$BB_HOME/.venv/bin/python"

# One-time admin token (written to $BB_HOME/data/.admin_token, chmod 0600).
(cd "$BB_HOME" && "$PY" -m server.cli create-admin-token)
```

### A.2. Start the server

```bash
(cd "$BB_HOME" && "$PY" scripts/ctl.py start)
curl -s "$BB_SERVER/api/v1/health"     # expect status:"ok", connected_devices:0
```

If `start` fails, see `references/operations.md` (port conflict / logs).

### A.3. Mint the user's identity + a pairing code

`create-user` **prints** the user token to stdout — it does **not** write a file — so capture it:

```bash
mkdir -p "$BB_HOME/data/users"
(cd "$BB_HOME" && "$PY" -m server.cli create-user me) > "$BB_HOME/data/users/me.token"
# Sanity-check it holds a real token:
grep -q '^bb_usr_' "$BB_HOME/data/users/me.token" && echo "token OK"

# Now mint a single-use pairing code for that user (prints the 6-char code):
(cd "$BB_HOME" && "$PY" -m server.cli create-pairing-code me)
```

`me` is the conventional single-user name; use anything you like, just keep the `.token` filename matching. The pairing code is single-use and expires in ~30 min.

### A.4. Hand the user the pairing details

Give the user this message — copy-paste friendly. **Substitute the actual pairing code** before sending. Match their language.

> 在你的浏览器（Mac）上完成下面三步：
>
> **1. 安装扩展**
>
> ```bash
> curl -sL https://raw.githubusercontent.com/danmi-ai/danmi-browser-bridge/master/web/install.sh | bash
> ```
>
> 会下载扩展到 `~/Downloads/danmi-browser-bridge-extension/` 并自动打开 Chrome 的扩展页。
>
> **2. 加载扩展**
>
> 在 `chrome://extensions`：打开右上角 **开发者模式** → 点 **加载已解压的扩展程序** → 选 `~/Downloads/danmi-browser-bridge-extension/`。
> （更新已装的：找到「丹秘 Browser Bridge」点刷新 🔄 即可。）
>
> **3. 配对**
>
> 点工具栏的丹秘图标，填：
> - **服务器地址**: `http://127.0.0.1:8404`（你本机跑的 server）
> - **配对码**: `<pairing_code>`（30 分钟内有效，单次使用）
>
> 点连接，弹窗显示绿色圆点就成功了，告诉我一声。

> Self-host has no central discovery, so the user types the server URL once into
> the popup. Default is `http://127.0.0.1:8404`; if you changed the port in
> `config.toml`, use that. Only the pairing code is per-session.

### A.5. Verify

```bash
curl -s "$BB_SERVER/api/v1/health"     # expect connected_devices >= 1
```

If it stays at 0:

- Code expired → re-mint (`create-pairing-code me`) and resend.
- Popup showed an error → ask for a screenshot and check the error code in `references/operations.md`.
- Extension not loaded → confirm Chrome shows "Danmi Browser Bridge" enabled in `chrome://extensions`.

Once online, return to SKILL.md Step 2.

## Flow B: re-pair

Server is up and the token exists, but the device is offline (user reinstalled Chrome, removed the extension, switched machines, or the WS got blocked). The token is preserved — you only need a fresh pairing code:

```bash
(cd "$BB_HOME" && "$BB_HOME/.venv/bin/python" -m server.cli create-pairing-code me)
```

Send the user the pairing message from A.4. If the extension is still installed, they can skip the install/load steps and just re-pair in the popup. Then verify as in A.5.

If the user says the extension *should* still be connected (didn't touch it), it's a runtime issue, not onboarding — have them reload the extension on `chrome://extensions` and check it can reach `$BB_SERVER/api/v1/health`; if it still won't connect, see `references/operations.md`.

## Token rotation

If a token is lost or you suspect it's compromised, retire the old user and mint a fresh one (this immediately invalidates the old token — confirm with the user first):

```bash
PY="$BB_HOME/.venv/bin/python"
(cd "$BB_HOME" && "$PY" -m server.cli revoke-user me)
(cd "$BB_HOME" && "$PY" -m server.cli create-user me) > "$BB_HOME/data/users/me.token"
(cd "$BB_HOME" && "$PY" -m server.cli create-pairing-code me)   # re-pair with the new code
```

The user re-enters the new pairing code in the popup. (Don't use `scripts/onboard.py --force-new` — it depends on a template that isn't shipped and will error.)
