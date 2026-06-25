# Danmi Browser Bridge

跨机器 Chrome 自动化：AI Agent 从服务器驱动用户 Mac 上 Chrome 的登录态会话——导航、截图、点击、填写、执行 JS、网络抓包、导出 PDF。**用户不需要给 AI 任何登录密码**，浏览器里已登录的会话直接复用。

> **AI Agent 用法看 [`skill/SKILL.md`](./skill/SKILL.md)** — 包含健康检查、用户识别、调用范式、tools 表。出错或新用户接入时，Agent 会被引导到 `skill/references/onboard.md`、`operations.md`、`tips.md`。本 README 是给开发者读的项目说明。

```
[ Agent / Python SDK ] ──HTTP──▶ [ Bridge Server :8404 ] ──WSS──▶ [ Chrome Extension ]
                                        │
                                    [ SQLite ]
```

## 核心能力

| 命令 | 功能 |
|------|------|
| `navigate` | 打开/切换 URL，支持 tab group 隔离 |
| `snapshot` | 获取页面可访问性树（@e ref 定位元素） |
| `click` | 点击元素（by ref 或 CSS selector） |
| `fill` | 填写 input/textarea/contenteditable |
| `screenshot` | 全页或元素级截图 |
| `evaluate` | 在页面上下文执行 JS |
| `network` | 网络请求抓包（start/stop/list/detail） |
| `upload` | 文件上传到 `<input type=file>` |
| `save_as_pdf` | 页面导出为 PDF |
| `list_tabs` / `find_tab` / `close_tab` / `close_session` | Tab 管理 |

## 仓库结构

| 路径 | 用途 |
|------|------|
| `server/` | FastAPI server: Command API, WebSocket, 认证, 限流 |
| `extension/` | Chrome MV3 extension (service_worker + commander + popup) |
| `sdk/danmi_bridge/` | Python SDK (CommandClient 同步 + 异步) |
| `tests/` | `test_command_api_e2e.py` — 全量 E2E |
| `scripts/` | ctl.py (生命周期: status/restart/logs), server.cli (admin: 用户/设备/会话/权限读写), onboard.py, backup_db.py, install-client.sh |
| `web/` | 静态资源: install.sh, 介绍页/审计页, 管理面板 |
| `skill/` | AI Agent 调用层: `SKILL.md` + `references/`（Claude/Ducc 等 Agent 直接挂载） |
| `config.toml` | 服务配置 |

> **代码 vs Skill 的分工**：`server/` `extension/` `sdk/` 是实现；`skill/` 是给 Agent 读的操作说明，通过 `$BB_HOME`（部署根）引用部署里的 `data/`、`scripts/`、`config.toml`，本身不含实现代码。两者同仓发布、同步迭代。

## Quickstart

### 1. Server 启动

```bash
python3 scripts/ctl.py start    # nohup uvicorn, port 8404
python3 scripts/ctl.py status   # 检查健康
```

### 2. 创建用户 + 配对码

```bash
curl -s -X POST "http://<server_ip>:8404/api/v1/onboard/<username>" \
  -H "Authorization: Bearer $(cat data/.admin_token)"
# 返回 { server_url, pairing_code, user_id }
```

### 3. Mac 端安装 Extension

```bash
# 安装命令与 server IP 解耦：脚本运行时从服务发现锚点（discovery.json）拿 server 地址。
# <YOUR_INSTALL_URL> 是你发布的 install.sh 地址（对象存储 / CDN / server /static 皆可）。
curl -sL <YOUR_INSTALL_URL> | bash
```

然后 chrome://extensions → Load unpacked → 弹窗里服务器地址已自动获取，只需填 Pairing Code → 连接。

> 扩展端点配置：`cp extension/src/config.example.js extension/src/config.js` 后填入你的 `DISCOVERY_URL` / `HOMEPAGE_URL`。`config.js` 已被 gitignore。

### 4. Python SDK 驱动

```python
import sys
sys.path.insert(0, "sdk")
from danmi_bridge import CommandClient

# server="auto" 时从服务发现锚点（BB_DISCOVERY_URL）解析当前 server 地址；也可传显式 URL
client = CommandClient("auto", token="bb_usr_...", session="my-task")
client.navigate("https://example.com", group_title="研究")
snap = client.snapshot()           # 获取页面结构
client.click(ref="@e5")            # 点击元素
client.fill("hello", selector="#input")
ss = client.screenshot()           # 截图
pdf = client.save_as_pdf()         # 导出 PDF
client.close_session()
client.close()
```

## 配置

`config.toml`:

```toml
[server]
host = "0.0.0.0"
port = 8404
version = "0.8.0"

[server.timeouts]
ws_heartbeat_interval = 15
ws_heartbeat_timeout = 5
command_default = 30

[extension]
current_version = "0.8.0"
min_version = "0.7.0"
```

## 版本管理

- Server 版本: `config.toml` → `[server] version`
- Extension 版本: `extension/manifest.json` → `version`

## 权限控制

| 权限 | 默认 | 开启方式 |
|------|------|----------|
| `evaluate` | 关 | DB: `SET evaluate_enabled=1, evaluate_domains='*'` |
| `network` | 关 | DB: `SET network_enabled=1` |

## 测试

```bash
python3 tests/test_command_api_e2e.py
# 需设备在线；token 从 BB_TOKEN / BB_USER / data/users/*.token 解析
```

## 服务发现

server IP 可能漂移，因此用一个**固定 URL 的 JSON 清单**（`discovery.json`）作锚点，所有客户端从这里自发现当前地址。把它托管在任何稳定可达的静态位置（对象存储 / CDN / 静态站点），并通过 `BB_DISCOVERY_URL` 告诉各客户端。

清单内容：
- `discovery.json` — 当前 `server_url` + extension 版本/sha256/下载地址

自发现链路：
- **server 启动**：`ctl.py start` 会尝试上报当前 ip:port（发布脚本属内部 overlay，缺失时静默跳过，不阻塞启动）
- **扩展**：popup 打开时从锚点自动填 server 地址；WS 连续重连失败后重读锚点拿新地址（IP 漂移自愈）。锚点 URL 在 `extension/src/config.js` 配置
- **SDK**：`CommandClient("auto", ...)` 从 `BB_DISCOVERY_URL` 解析 server_url
- **install.sh**：扩展包从固定地址下载，server 地址运行时从锚点读

> 发布/分发脚本（把 zip、网页、discovery 推到你的静态托管）属部署细节，不随核心仓发布。自托管时实现你自己的发布步骤即可，或参考 `install-client.sh` / `web/install.sh` 中的占位符。

## 运维

```bash
# 进程生命周期
python3 scripts/ctl.py status|restart|logs
# 用户/设备/会话/权限管理（更全，单一来源）
(cd "$BB_HOME" && .venv/bin/python -m server.cli list-users|list-devices|list-sessions|show-user <u>|stats)
curl -s http://<server_host>:8404/api/v1/health
```

## License

`extension/inject-scripts/` 中的可访问性树脚本改编自 [mcp-chrome](https://github.com/hangwin/mcp-chrome) (MIT)。详见 `extension/inject-scripts/THIRD_PARTY_LICENSES.md`。
