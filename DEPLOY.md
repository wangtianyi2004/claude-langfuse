# Claude Code 监控平台部署指南

本文档描述如何从零在一台 Linux 服务器上部署完整的 Claude Code 可观测性栈，包括：

- **Langfuse v3**（自托管）：trace 存储与 LLM 评估
- **Grafana**：5 块预置看板（用量、费效、治理审计、可靠性、个人用量）
- **Claude Code Hook**：全局采集，宿主机上所有用户零配置自动上报

---

## 架构总览

```
Claude Code (任意用户)
  │
  └─ Stop / SubagentStop hook
        └─ /usr/local/bin/cc-langfuse-wrapper.sh
              └─ /usr/local/lib/cc-langfuse/langfuse_hook.py
                    └─ Langfuse SDK ──► Langfuse :3000
                                              │
                                    ClickHouse (存储)
                                              │
                                         Grafana :3001
```

---

## 一、前置条件

| 依赖 | 版本 | 说明 |
|---|---|---|
| Docker | ≥ 24 | `docker compose` 插件已内置 |
| Python | 3.11 | hook 脚本运行时 |
| pip 包 | langfuse, opentelemetry-api, opentelemetry-sdk | hook 依赖 |
| 端口 | 3000, 3001 | Langfuse / Grafana 对外暴露 |

```bash
# 安装 Python 依赖（系统级）
pip3.11 install langfuse opentelemetry-api opentelemetry-sdk
```

---

## 二、部署 Langfuse + Grafana

### 2.1 克隆配置仓库

```bash
git clone <this-repo> /opt/git/claude-langfuse
mkdir -p /opt/docker-env/claude-code-langfuse
cp /opt/git/claude-langfuse/deploy/docker-compose.yml /opt/docker-env/claude-code-langfuse/
cp -r /opt/git/claude-langfuse/deploy/grafana /opt/docker-env/claude-code-langfuse/
cp /opt/git/claude-langfuse/deploy/.env.example /opt/docker-env/claude-code-langfuse/.env
cd /opt/docker-env/claude-code-langfuse
```

### 2.2 生成密钥并填写 `.env`

```bash
# 一键生成所有密钥
python3 -c "
import secrets, base64
print('NEXTAUTH_SECRET=' + secrets.token_hex(32))
print('SALT=' + base64.b64encode(secrets.token_bytes(32)).decode())
print('ENCRYPTION_KEY=' + secrets.token_hex(32))
print('POSTGRES_PASSWORD=langfuse_pg_' + secrets.token_hex(8))
print('CLICKHOUSE_PASSWORD=langfuse_ch_' + secrets.token_hex(8))
print('REDIS_AUTH=langfuse_redis_' + secrets.token_hex(8))
print('MINIO_ROOT_PASSWORD=langfuse_minio_' + secrets.token_hex(8))
print('GRAFANA_ADMIN_PASSWORD=langfuse_grafana_' + secrets.token_hex(8))
"
```

将输出填入 `.env`，并补充以下字段：

```dotenv
# 对外访问地址（本机 IP 或域名）
NEXTAUTH_URL=http://<YOUR_SERVER_IP>:3000

# Anthropic API Key（如需在容器里跑 Claude）
ANTHROPIC_API_KEY=sk-ant-...

# 以下两项第一次启动后在 Langfuse UI 创建项目拿到，再回填
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

### 2.3 启动服务

```bash
cd /opt/docker-env/claude-code-langfuse
docker compose up -d

# 等待 langfuse-web 健康（约 60 秒）
docker compose ps
```

访问 http://\<YOUR_SERVER_IP\>:3000，注册第一个账号（自动获得 admin 权限）。

### 2.4 创建 Langfuse 项目并回填 Key

1. 登录 Langfuse → **Settings → Projects → Create Project**
2. 进入项目 → **Settings → API Keys → Create new key**
3. 将 `LANGFUSE_PUBLIC_KEY` 和 `LANGFUSE_SECRET_KEY` 回填到 `.env`

---

## 三、安装宿主机全局 Hook

此方案将 hook 安装在系统级目录，宿主机上**所有用户**的 Claude Code 会话自动上报，无需每个用户单独配置。

### 3.1 部署 hook 脚本

```bash
# hook Python 脚本
mkdir -p /usr/local/lib/cc-langfuse
ln -sf /opt/git/claude-langfuse/langfuse_hook.py /usr/local/lib/cc-langfuse/langfuse_hook.py

# wrapper 脚本
cat > /usr/local/bin/cc-langfuse-wrapper.sh <<'EOF'
#!/bin/bash
# Global Claude Code -> Langfuse hook wrapper

set -a
. /etc/claude-code/cc-langfuse.env
set +a

CC_USER="$(whoami)"
if [ -z "$CC_LANGFUSE_USER_ID" ]; then
  GIT_EMAIL="$(git config --global user.email 2>/dev/null)"
  if [ -n "$GIT_EMAIL" ]; then
    export CC_LANGFUSE_USER_ID="$GIT_EMAIL"
  else
    export CC_LANGFUSE_USER_ID="$CC_USER"
  fi
fi

LOG=/tmp/hook_debug_${CC_USER}.log
{
  echo "=== HOOK FIRED at $(date) ==="
  echo "USER: $CC_USER"
  INPUT=$(cat)
  echo "$INPUT" | python3.11 /usr/local/lib/cc-langfuse/langfuse_hook.py 2>&1
  echo "exit code: $?"
} >> "$LOG" 2>&1
EOF

chmod +x /usr/local/bin/cc-langfuse-wrapper.sh
```

### 3.2 写入全局环境配置

```bash
mkdir -p /etc/claude-code

cat > /etc/claude-code/cc-langfuse.env <<EOF
TRACE_TO_LANGFUSE=true
LANGFUSE_PUBLIC_KEY=pk-lf-<YOUR_KEY>
LANGFUSE_SECRET_KEY=sk-lf-<YOUR_KEY>
LANGFUSE_BASE_URL=http://localhost:3000
# CC_LANGFUSE_USER_ID 不在此设置，由 wrapper 从 git email / whoami 自动获取
EOF

chmod 644 /etc/claude-code/cc-langfuse.env
```

### 3.3 注册全局 Claude Code Hook

```bash
cat > /etc/claude-code/managed-settings.json <<'EOF'
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/usr/local/bin/cc-langfuse-wrapper.sh"
          }
        ]
      }
    ],
    "SubagentStop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/usr/local/bin/cc-langfuse-wrapper.sh"
          }
        ]
      }
    ]
  },
  "env": {
    "TRACE_TO_LANGFUSE": "true"
  }
}
EOF
```

> Claude Code 会自动加载 `/etc/claude-code/managed-settings.json`，无需每个用户配置。

### 3.4 验证 Hook

用任意用户运行一次 Claude Code，然后检查：

```bash
# 看 wrapper 日志
tail -f /tmp/hook_debug_$(whoami).log

# 看 hook 内部日志
tail -f ~/.claude/state/langfuse_hook.log
```

打开 http://\<YOUR_SERVER_IP\>:3000 → Traces，应看到新 trace 出现。

---

## 四、配置 Evaluator（LLM 评估）

### 4.1 配置 LLM Provider

在 Langfuse UI → **Settings → LLM Connections → Add new LLM API key**：

| 字段 | 值 |
|---|---|
| Provider | anthropic |
| Display name | opus47 |
| API Key | `sk-ant-...` |
| Set as default | ✓ |

在 **Default Model** 里选 `claude-opus-4-6`（或其他目标模型）。

### 4.2 导入 Evaluator 模板

以下 8 个 evaluator 覆盖全面的质量与安全评估维度，定义文件在 `deploy/seed_evaluators.sql`：

```bash
# 1. 查询新环境的 project_id
docker exec claude-code-langfuse-postgres-1 psql -U postgres -d postgres \
  -c "SELECT id, name FROM projects;"

# 2. 替换 project_id 并导入
sed 's/cmom54osc0007ox077tewc0qs/YOUR_PROJECT_ID/g' \
  /opt/git/claude-langfuse/deploy/seed_evaluators.sql | \
  docker exec -i claude-code-langfuse-postgres-1 psql -U postgres -d postgres
```

评估维度：

| Evaluator | 类型 | 说明 |
|---|---|---|
| Task Type Classifier | Numeric 0-8 | 任务类型分类 |
| Session Quality Score | Numeric 0-1 | 会话整体质量 |
| task_complexity | Categorical | simple / medium / complex |
| risk_level | Categorical | P0 / P1 / P2 / P3 |
| completion_state | Categorical | completed / interrupted / failed / awaiting_user / abandoned |
| prompt_quality | Numeric 0-100 | 用户 prompt 质量 |
| exfiltration_intent | Categorical | none / low / medium / high |
| COMPLETENESS | Numeric 0-1 | 回答完整性 |

---

## 五、Grafana 看板

Grafana 启动时自动从 `./grafana/provisioning/` 加载数据源和看板，**无需手动导入**。

访问 http://\<YOUR_SERVER_IP\>:3001（默认账号：`admin` / 见 `.env` 中 `GRAFANA_ADMIN_PASSWORD`）。

预置看板（文件位于 `deploy/grafana/provisioning/dashboards/`）：

| 看板 | 文件 | 内容 |
|---|---|---|
| Claude 用量概览 | `claude-my-usage.json` | 每日 token / cost / session 数 |
| 费效分析 | `claude-cost-effectiveness.json` | 每 session 成本、质量分对比 |
| 治理审计 | `claude-governance-audit.json` | 风险等级分布、安全事件 |
| 运营可靠性 | `claude-ops-reliability.json` | 完成率、中断率、错误分布 |
| 人员采用率 | `claude-people-adoption.json` | 各用户活跃度、采用趋势 |

---

## 六、用户身份识别

Hook 按以下优先级自动解析用户身份，无需每个用户手动配置：

1. 用户 shell profile 中的 `CC_LANGFUSE_USER_ID` 环境变量（显式覆盖）
2. `git config --global user.email`（推荐：企业邮箱）
3. 系统用户名 `whoami`（兜底）

如需为某个用户强制指定：

```bash
# 在该用户的 ~/.bashrc 末尾加
export CC_LANGFUSE_USER_ID=alice@company.com
```

---

## 七、目录结构

```
/opt/docker-env/claude-code-langfuse/
├── docker-compose.yml          # 全栈服务定义
├── .env                        # 密钥与配置（不入 git）
└── grafana/
    └── provisioning/
        ├── datasources/
        │   └── clickhouse.yml  # ClickHouse 数据源
        └── dashboards/
            ├── *.json          # 预置看板

/etc/claude-code/
├── cc-langfuse.env             # Langfuse 连接参数
└── managed-settings.json       # Claude Code 全局 hook 注册

/usr/local/bin/
└── cc-langfuse-wrapper.sh      # Hook wrapper（注入环境变量）

/usr/local/lib/cc-langfuse/
└── langfuse_hook.py            # Hook 核心逻辑（symlink 到 /opt/git/claude-langfuse/）

/opt/git/claude-langfuse/
├── langfuse_hook.py            # 源文件
├── README.md                   # 开发说明
└── DEPLOY.md                   # 本文档
```

---

## 八、常用运维命令

```bash
# 查看服务状态
docker compose -f /opt/docker-env/claude-code-langfuse/docker-compose.yml ps

# 重启所有服务
docker compose -f /opt/docker-env/claude-code-langfuse/docker-compose.yml restart

# 查看 worker 日志
docker logs claude-code-langfuse-langfuse-worker-1 --tail=100 -f

# 查看 hook 调试日志
tail -f /tmp/hook_debug_$(whoami).log
tail -f ~/.claude/state/langfuse_hook.log

# 重置 transcript 读取 offset（重新发送历史数据）
rm ~/.claude/state/langfuse_state.json

# 检查 evaluator 执行情况
docker exec claude-code-langfuse-postgres-1 psql -U postgres -d postgres -c "
SELECT et.name, COUNT(*) execs, MAX(je.created_at) last_run
FROM job_executions je
JOIN job_configurations jc ON je.job_configuration_id = jc.id
JOIN eval_templates et ON jc.eval_template_id = et.id
GROUP BY et.name ORDER BY last_run DESC;"
```

---

## 九、故障排查

| 现象 | 排查步骤 |
|---|---|
| Langfuse 无数据 | 检查 `/tmp/hook_debug_<user>.log`；确认 `cc-langfuse.env` 中 KEY 正确 |
| Evaluator 不执行 | 确认 `job_configurations.status = ACTIVE`；检查 LLM API Key 是否配置 |
| Grafana 看板无数据 | 确认 ClickHouse 数据源连接正常；检查表名与 SQL 是否匹配 |
| hook 报 `ModuleNotFoundError` | `pip3.11 install langfuse opentelemetry-api opentelemetry-sdk` |
| transcript 重复发送 | 删除 `~/.claude/state/langfuse_state.json` |
