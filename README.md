# claude-langfuse

把 Claude Code 的会话、turn、工具调用、git commit 等事件采集到 [Langfuse](https://langfuse.com) 的一个 hook 脚本，并按 **feature branch** 维度打 tag，便于在 Langfuse 里按分支过滤、追溯。

## 它做了什么

`langfuse_hook.py` 作为 Claude Code 的 hook，监听以下事件并发送到 Langfuse：

| Claude 事件 | 在 Langfuse 中的对象 | 备注 |
|---|---|---|
| `SessionStart` / `SessionEnd` | `event` | 会话起止 marker |
| `UserPromptSubmit` | `event` | 用户每次提交 prompt |
| `Stop`（每个 turn 结束） | `span` + `generation` + `tool` | tail transcript jsonl，按 turn 拆分；包含 token usage、cost、LOC、tool 决策等 |
| `PostToolUse(Bash, git commit)` | `event` (`GitCommit`) | commit SHA、branch、message、改动文件列表 |

每一条 trace 都会自动打上：
- `branch:<name>` — 当前 git 分支
- `repo:<name>` — 仓库名
- `claude-code` + 事件类型 tag

同时，hook 把 `{trace_id, session_id, branch, head_sha}` 写到 `<repo>/.claude/active-session.json`，供 git 的 `prepare-commit-msg` 钩子消费——把 Langfuse trace 链接作为 trailer 注入 commit message。

## 安装

### 1. 依赖

```bash
pip install langfuse opentelemetry-api opentelemetry-sdk
```

> 用 `python3.11` 即可（wrapper 脚本里硬编码了这个版本）；要换版本就改 `lfhook_wrapper.sh`。

### 2. 放置脚本

```bash
git clone <this-repo> /opt/git/claude-langfuse
ln -s /opt/git/claude-langfuse/langfuse_hook.py ~/langfuse_hook.py
mkdir -p ~/.claude/hooks
ln -s ~/langfuse_hook.py ~/.claude/hooks/langfuse_hook.py
```

### 3. 配置环境变量（写进 `~/.bashrc`）

```bash
export TRACE_TO_LANGFUSE=true
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_HOST=http://your-langfuse-host:3000   # 或 https://cloud.langfuse.com
# 可选
# export CC_LANGFUSE_DEBUG=true
# export CC_LANGFUSE_USER_ID=alice
# export CC_LANGFUSE_MAX_CHARS=20000
```

### 4. 创建 wrapper（`~/.claude/hooks/lfhook_wrapper.sh`）

Wrapper 的作用是无论 Claude Code 启动时 shell 环境如何，都能拿到 `LANGFUSE_*` 变量。

```bash
#!/bin/bash
set -a
[ -f ~/.bashrc ] && source ~/.bashrc 2>/dev/null
set +a
export TRACE_TO_LANGFUSE=true

INPUT=$(cat)
echo "$INPUT" | python3.11 ~/.claude/hooks/langfuse_hook.py 2>>/tmp/hook_debug.log
```

```bash
chmod +x ~/.claude/hooks/lfhook_wrapper.sh
```

### 5. 注册 Claude Code hook（`~/.claude/settings.json`）

```json
{
  "hooks": {
    "Stop":             [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/lfhook_wrapper.sh" }] }],
    "PostToolUse":      [{ "matcher": "Bash",
                           "hooks": [{ "type": "command", "command": "~/.claude/hooks/lfhook_wrapper.sh" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/lfhook_wrapper.sh" }] }],
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/lfhook_wrapper.sh" }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/lfhook_wrapper.sh" }] }]
  }
}
```

> Claude Code 不展开 `~`——填**绝对路径**，例如 `/root/.claude/hooks/lfhook_wrapper.sh`。

| Hook | 必要性 | 作用 |
|---|---|---|
| `Stop` | 必装 | tail transcript，emit per-turn span |
| `PostToolUse` (matcher: Bash) | 提交监控必装 | 触发 `GitCommit` 事件 |
| `UserPromptSubmit` | 推荐 | 让 `active-session.json` 在 commit 前就写入 trace_id |
| `SessionStart` / `SessionEnd` | 可选 | 会话生命周期 marker |

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
host = (os.environ.get('LANGFUSE_HOST')
        or os.environ.get('CC_LANGFUSE_BASE_URL')
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

> ⚠️ `core.hooksPath` 是**覆盖式**的——一旦设置，所有 repo 的 `.git/hooks/*` 都不再被调用。如果某些 repo 依赖自定义 hook，需要把它们一并放到 `~/.config/git/hooks/`，或写一个 dispatcher 转发。

### 3. 回滚

```bash
git config --global --unset core.hooksPath
```

## 数据流

```
Claude 在 feature 分支跑活
  │
  ├─ Stop / UserPromptSubmit / SessionStart...
  │     └─ langfuse_hook.py
  │            ├─ emit span/event 到 Langfuse  (tags: branch:feature/foo, repo:bar)
  │            └─ 刷新 <repo>/.claude/active-session.json (trace_id, branch, sha)
  │
  └─ Claude / 用户跑 `git commit`
        ├─ Claude PostToolUse(Bash) → langfuse_hook.py → emit GitCommit event
        └─ git 自身 prepare-commit-msg → 注入 commit message trailer:
             Langfuse-Trace: http://.../trace/<id>
             Claude-Branch:  feature/foo
             Claude-Session: <sid>
```

## 在 Langfuse 里按分支查询

- **看某个 feature 分支的所有 Claude 活动**：tag 过滤 `branch:feature/your-branch`
- **只看提交事件**：tag 过滤 `commit`
- **从 `git log` 反查**：commit message 里的 `Langfuse-Trace:` URL 直接打开 trace

## 调优 / 故障排查

| 现象 | 排查 |
|---|---|
| Langfuse 里没数据 | `tail -f /tmp/hook_debug.log` 看 wrapper 输出；`tail -f ~/.claude/state/langfuse_hook.log` 看脚本日志 |
| commit message 没有 trailer | 确认 `<repo>/.claude/active-session.json` 存在且有 `trace_id`；`git config --global --get core.hooksPath` 应指向 `~/.config/git/hooks` |
| transcript 重复发送 | 删 `~/.claude/state/langfuse_state.json` 重置 offset |
| 输出过长被截断 | 调大 `CC_LANGFUSE_MAX_CHARS`（默认 20000） |

## 文件清单

| 路径 | 用途 |
|---|---|
| `langfuse_hook.py` | 核心 hook 脚本 |
| `~/.claude/hooks/lfhook_wrapper.sh` | bash wrapper（注入环境变量） |
| `~/.claude/settings.json` | Claude Code hook 注册 |
| `~/.config/git/hooks/prepare-commit-msg` | git 全局 hook，注入 trace 链接 |
| `<repo>/.claude/active-session.json` | hook 与 git 之间的桥梁，运行时写入 |
| `~/.claude/state/langfuse_state.json` | 各 session 的 transcript 读取 offset |
| `~/.claude/state/langfuse_hook.log` | 脚本运行日志 |
| `/tmp/hook_debug.log` | wrapper 的 stdin/stdout 调试日志 |
