# claude-langfuse

把 Claude Code 的会话、turn、工具调用、git commit 等事件采集到 [Langfuse](https://langfuse.com) 的 hook 脚本，并按 **feature branch** 维度打 tag，便于在 Langfuse 里按分支过滤、追溯。

配套 Grafana 看板提供用量、费效、治理审计、运营可靠性等多维度分析。

> **完整部署（Langfuse + Grafana + 全局 Hook）** 请看 [DEPLOY.md](DEPLOY.md)。

---

## 它做了什么

`langfuse_hook.py` 作为 Claude Code 的 hook，监听以下事件并发送到 Langfuse：

| Claude 事件 | 在 Langfuse 中的对象 | 备注 |
|---|---|---|
| `Stop`（每个 turn 结束） | `span` + `generation` + `tool` | tail transcript jsonl，按 turn 拆分；包含 token usage、cost、LOC、tool 决策等 |
| `SubagentStop` | `span` | 子 agent 的 turn |
| `PostToolUse(Bash, git commit)` | `event` (`GitCommit`) | commit SHA、branch、message、改动文件列表 |

每一条 trace 都会自动打上：
- `branch:<name>` — 当前 git 分支
- `repo:<name>` — 仓库名
- `claude-code` + 事件类型 tag

同时，hook 把 `{trace_id, session_id, branch, head_sha}` 写到 `<repo>/.claude/active-session.json`，供 git 的 `prepare-commit-msg` 钩子消费——把 Langfuse trace 链接作为 trailer 注入 commit message。

---

## 仓库结构

```
claude-langfuse/
├── langfuse_hook.py         # hook 核心脚本
├── README.md                # 本文档（hook 说明 + 单用户安装）
├── DEPLOY.md                # 完整平台部署指南（Langfuse + Grafana + 全局 hook）
└── deploy/
    ├── docker-compose.yml   # Langfuse v3 + Grafana 全栈
    ├── .env.example         # 环境变量模板
    ├── seed_evaluators.sql  # 8 个 LLM evaluator 中文 prompt 定义
    └── grafana/provisioning/
        ├── datasources/
        │   └── clickhouse.yml
        └── dashboards/
            ├── claude-my-usage.json
            ├── claude-cost-effectiveness.json
            ├── claude-governance-audit.json
            ├── claude-ops-reliability.json
            └── claude-people-adoption.json
```

---

## 快速安装（单用户）

适合个人开发者连接已有 Langfuse 实例。团队/服务器部署请看 [DEPLOY.md](DEPLOY.md)。

### 1. 依赖

```bash
pip3.11 install langfuse opentelemetry-api opentelemetry-sdk
```

### 2. 放置脚本

```bash
git clone <this-repo> /opt/git/claude-langfuse
mkdir -p ~/.claude/hooks
ln -s /opt/git/claude-langfuse/langfuse_hook.py ~/.claude/hooks/langfuse_hook.py
```

### 3. 创建 wrapper（`~/.claude/hooks/lfhook_wrapper.sh`）

```bash
cat > ~/.claude/hooks/lfhook_wrapper.sh <<'EOF'
#!/bin/bash
set -a
[ -f ~/.bashrc ] && source ~/.bashrc 2>/dev/null
set +a
export TRACE_TO_LANGFUSE=true

INPUT=$(cat)
echo "$INPUT" | python3.11 ~/.claude/hooks/langfuse_hook.py 2>>/tmp/hook_debug_$(whoami).log
EOF
chmod +x ~/.claude/hooks/lfhook_wrapper.sh
```

### 4. 配置环境变量（写进 `~/.bashrc`）

```bash
export TRACE_TO_LANGFUSE=true
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_BASE_URL=http://your-langfuse-host:3000  # 或 https://cloud.langfuse.com
# 可选
# export CC_LANGFUSE_DEBUG=true
# export CC_LANGFUSE_USER_ID=alice@company.com
# export CC_LANGFUSE_MAX_CHARS=20000
```

### 5. 注册 Claude Code hook（`~/.claude/settings.json`）

```json
{
  "hooks": {
    "Stop": [{ "hooks": [{ "type": "command", "command": "/root/.claude/hooks/lfhook_wrapper.sh" }] }],
    "SubagentStop": [{ "hooks": [{ "type": "command", "command": "/root/.claude/hooks/lfhook_wrapper.sh" }] }]
  }
}
```

> Claude Code 不展开 `~`，填**绝对路径**。

---

## 把 commit 链接到 Langfuse trace（git 全局 hook）

让任何终端里的 `git commit` 都自动在 commit message 里加上 Langfuse trace 链接。

### 1. 创建 `prepare-commit-msg`

```bash
mkdir -p ~/.config/git/hooks
cat > ~/.config/git/hooks/prepare-commit-msg <<'SH'
#!/bin/bash
set -e
COMMIT_MSG_FILE="$1"
COMMIT_SOURCE="$2"

case "$COMMIT_SOURCE" in
  merge|squash|commit) exit 0 ;;
esac

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
SESSION_FILE="$REPO_ROOT/.claude/active-session.json"
[ -f "$SESSION_FILE" ] || exit 0
grep -q '^Langfuse-Trace:' "$COMMIT_MSG_FILE" 2>/dev/null && exit 0

python3 - "$SESSION_FILE" "$COMMIT_MSG_FILE" <<'PY'
import json, os, sys
session_file, msg_file = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(session_file))
except Exception:
    sys.exit(0)
trace_id = d.get('trace_id') or ''
session_id = d.get('session_id') or ''
branch = d.get('branch') or ''
if not trace_id:
    sys.exit(0)
host = (os.environ.get('LANGFUSE_BASE_URL')
        or os.environ.get('LANGFUSE_HOST')
        or 'https://cloud.langfuse.com').rstrip('/')
url = f"{host}/trace/{trace_id}"
with open(msg_file, 'r', encoding='utf-8') as f:
    msg = f.read()
trailers = [f"Langfuse-Trace: {url}"]
if branch:     trailers.append(f"Claude-Branch: {branch}")
if session_id: trailers.append(f"Claude-Session: {session_id}")
sep = "" if msg.endswith("\n\n") else ("\n" if msg.endswith("\n") else "\n\n")
with open(msg_file, 'w', encoding='utf-8') as f:
    f.write(msg + sep + "\n".join(trailers) + "\n")
PY
SH
chmod +x ~/.config/git/hooks/prepare-commit-msg
```

### 2. 全局启用

```bash
git config --global core.hooksPath ~/.config/git/hooks
```

> ⚠️ `core.hooksPath` 是覆盖式的——一旦设置，所有 repo 的 `.git/hooks/*` 都不再被调用。如有 repo 级 hook，需一并放入 `~/.config/git/hooks/`。

---

## 数据流

```
Claude 在 feature 分支跑活
  │
  ├─ Stop / SubagentStop
  │     └─ langfuse_hook.py
  │            ├─ emit span/event 到 Langfuse  (tags: branch:feature/foo, repo:bar, claude-code)
  │            └─ 刷新 <repo>/.claude/active-session.json (trace_id, branch, sha)
  │
  └─ Claude / 用户跑 `git commit`
        ├─ PostToolUse(Bash) → langfuse_hook.py → emit GitCommit event
        └─ git prepare-commit-msg → 注入 commit message trailer:
             Langfuse-Trace: http://.../trace/<id>
             Claude-Branch:  feature/foo
             Claude-Session: <sid>
```

---

## 在 Langfuse 里查询

- **看某个分支的所有 Claude 活动**：tag 过滤 `branch:feature/your-branch`
- **只看 git 提交事件**：tag 过滤 `commit`
- **从 `git log` 反查**：commit message 里的 `Langfuse-Trace:` URL 直接打开 trace

---

## 故障排查

| 现象 | 排查 |
|---|---|
| Langfuse 里没数据 | `tail -f /tmp/hook_debug_$(whoami).log`；确认 `LANGFUSE_PUBLIC_KEY` / `SECRET_KEY` 正确 |
| commit message 没有 trailer | 确认 `<repo>/.claude/active-session.json` 存在且含 `trace_id`；检查 `git config --global core.hooksPath` |
| transcript 重复发送 | 删除 `~/.claude/state/langfuse_state.json` 重置 offset |
| 输出过长被截断 | 调大 `CC_LANGFUSE_MAX_CHARS`（默认 20000） |
| `ModuleNotFoundError` | `pip3.11 install langfuse opentelemetry-api opentelemetry-sdk` |

## 环境变量参考

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `TRACE_TO_LANGFUSE` | 是 | — | 设为 `true` 启用上报 |
| `LANGFUSE_PUBLIC_KEY` | 是 | — | Langfuse 项目 public key |
| `LANGFUSE_SECRET_KEY` | 是 | — | Langfuse 项目 secret key |
| `LANGFUSE_BASE_URL` | 是 | — | Langfuse 服务地址 |
| `CC_LANGFUSE_USER_ID` | 否 | git email / whoami | 覆盖用户身份 |
| `CC_LANGFUSE_MAX_CHARS` | 否 | `20000` | 单字段最大字符数 |
| `CC_LANGFUSE_DEBUG` | 否 | `false` | 设为 `true` 输出详细日志 |
