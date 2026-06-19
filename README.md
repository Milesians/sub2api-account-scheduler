# sub2api-account-scheduler

> 面向 sub2api 账号池的外部 7D 额度调度器。它通过 sub2api Admin API 定时读取账号用量，并动态调整受控账号的 `priority` 与 `load_factor`，让账号在 7D 窗口刷新前尽可能接近满用，同时把 5h、并发、429、运行时不可调度等请求级保护继续交给 sub2api 原生机制处理。

## 目录

- [项目定位](#项目定位)
- [核心能力](#核心能力)
- [工作原理](#工作原理)
- [快速开始：Docker Compose](#快速开始docker-compose)
- [从源码运行](#从源码运行)
- [配置说明](#配置说明)
- [调度策略说明](#调度策略说明)
- [Dashboard / UI](#dashboard--ui)
- [数据库与持久化](#数据库与持久化)
- [运行建议](#运行建议)
- [常见调参场景](#常见调参场景)
- [测试](#测试)
- [排障](#排障)
- [开发说明](#开发说明)
- [安全说明](#安全说明)

## 项目定位

`sub2api-account-scheduler` 是一个 **外部账号池节奏控制器**，不是请求级代理，也不会直接接管每个请求应该命中哪个账号。

它的职责是：

1. 定期从 sub2api Admin API 拉取账号列表和被动 usage 数据。
2. 只管理匹配 `platform`、账号类型白名单和 `account_name_pattern` 的账号。
3. 对缺失或过期 usage 的账号做限量 active probe。
4. 根据每个账号自己的 7D reset 时间计算 pacing / drain 决策。
5. 通过 Admin API 批量更新账号 `priority` 与 `load_factor`。
6. 用 SQLite 保存账号状态、usage 采样、决策日志和 UI 控制开关。

它不做：

- 不修改账号 token、key、proxy、分组等敏感配置。
- 不绕过 sub2api 原生并发、限流、过载、模型兼容、sticky session 等机制。
- 不直接指定某个请求使用哪个账号。
- 不把 `priority` 当精确流量比例；普通补量主要靠 `load_factor`，terminal drain 才会使用更低 priority 抢新会话。
- 默认不承担 5h 硬保护；`enable_5h_guard` 默认为 `false`，5h 防爆建议交给 sub2api 原生运行时保护。

## 核心能力

- **7D pacing**：普通阶段让账号使用率跟随线性目标，减少长期掉队和少数账号过热。
- **Terminal drain**：账号临近 7D reset 时进入冲刺模式，根据剩余 gap / required rate / pressure 尽量把额度用到 `drain_target_7d_utilization`。
- **priority + load_factor 双控制**：普通阶段使用同一 normal priority + 不同 load_factor 分散补量；terminal 阶段按 strong / mild / normal / done 使用不同档位。
- **数据新鲜度控制**：优先使用被动 usage；数据缺失或 stale 时按风险排序做 active probe。
- **冷账号防死锁**：usage 缺失不会直接降保护档，避免冷账号因为没有流量而永远没有新数据。
- **SQLite 持久化**：保存 EWMA burn、历史采样、决策日志、账号暂停状态、OpenAI 订阅信息缓存等。
- **Dashboard**：内置标准库 HTTPServer + Vue 前端构建产物，展示账号状态、最近决策、心跳、暂停开关和 Codex invite reset 辅助接口。
- **Docker 一键部署**：提供 `Dockerfile` 与 `docker-compose.yml`，支持 heartbeat healthcheck。

## 工作原理

### 一轮 tick 的流程

```text
load account_state
  ↓
GET /api/v1/admin/accounts?platform=...
  ↓
parse account usage / reset / schedulable state
  ↓
按 platform + account type + account_name_pattern 过滤受控账号
  ↓
排除 UI 手动暂停的账号
  ↓
对 usage 缺失或 stale 的账号做限量 active probe
  ↓
从 usage_sample 计算最近 5h burn
  ↓
policy.decide() 生成每个账号的目标 priority / load_factor
  ↓
POST /api/v1/admin/accounts/bulk-update
  ↓
保存 state / usage_sample / decision_log
  ↓
清理过期数据，写 heartbeat
```

### sub2api 调度语义

sub2api 中 `priority` 数值越小越优先。默认路径下，低 priority 账号会优先获得新会话；`load_factor` 则影响同档账号的 LoadRate 计算，让账号在调度视角中呈现更大或更小的“容量”。

本项目依赖这个语义做两件事：

- **普通 pacing 阶段**：大多数账号保持 `band_normal`，通过 `load_factor` 微调补量权重，避免单个账号因为被放到独占低 priority 而被打爆。
- **terminal drain 阶段**：离 7D reset 足够近时，欠量账号可以进入 `band_boost` / `band_mild`，更积极地吸收新会话，目标是在 reset 前尽量接近 drain target。

> 注意：sticky session / previous response 粘连通常不会因为 priority 调整立刻迁移。降权主要影响新会话，存量粘性流量仍可能继续消耗。

## 快速开始：Docker Compose

### 1. 准备环境变量

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
SUB2API_BASE_URL=https://your-sub2api.example.com
SUB2API_ADMIN_KEY=your-admin-api-key

# SQLite 与 heartbeat 路径，容器内默认即可
DB_PATH=/data/scheduler.db
HEARTBEAT_FILE=/data/last_tick

# 是否启动内置 UI
UI_ENABLED=true
UI_HOST=0.0.0.0
UI_PORT=18080

# 强烈建议生产环境修改；UI 里的敏感操作会校验它
SENSITIVE_ACTION_PASSWORD=change-me

# 建议生产环境设置；不设置时会派生自 SUB2API_ADMIN_KEY
UI_SESSION_SECRET=change-me-too
```

### 2. 编辑策略配置

默认配置在 `config.yaml`。至少确认这些字段：

```yaml
platform: openai                    # openai / anthropic；一个实例只管理一个平台
account_name_pattern: 'pay\d+'       # 只管理名称匹配的账号；空字符串表示全部受控

pacing_target_7d_utilization: 97.0
terminal_drain_enabled: true
terminal_window_hours: 36.0
drain_target_7d_utilization: 99.4
hard_cap_7d_utilization: 99.8

enable_5h_guard: false              # 默认让 sub2api 原生层负责 5h 保护
priority_bands: [1010, 1030, 1050, 1070, 1099]
```

### 3. 启动

```bash
docker compose up -d --build
```

查看日志：

```bash
docker logs -f sub2api-account-scheduler
```

查看健康状态：

```bash
docker inspect --format='{{json .State.Health}}' sub2api-account-scheduler | jq
```

### 4. 单轮试运行

生产首次接入建议先只跑一轮，观察日志和 `decision_log`：

```bash
docker compose run --rm scheduler --once
```

如果只想初始化 / 升级数据库表：

```bash
docker compose run --rm scheduler --migrate-db
```

## 从源码运行

后端需要 Python 3.13+。项目使用 `uv` 管理依赖。

```bash
cd backend
uv sync

export SUB2API_BASE_URL=https://your-sub2api.example.com
export SUB2API_ADMIN_KEY=your-admin-api-key
export CONFIG_PATH=../config.yaml
export DB_PATH=../data/scheduler.db
export HEARTBEAT_FILE=../data/last_tick

uv run python -m scheduler --once
```

持续运行：

```bash
uv run python -m scheduler
```

单独启动 UI：

```bash
uv run python -m scheduler --ui --ui-host 0.0.0.0 --ui-port 18080
```

前端开发：

```bash
cd frontend
npm ci
npm run dev
```

前端构建产物输出到后端包内：

```bash
npm run build
# 输出目录：backend/src/scheduler/frontend
```

## 配置说明

配置优先级：

```text
环境变量 > config.yaml > Config 内置默认值
```

敏感项只建议通过环境变量传入，尤其是 `SUB2API_ADMIN_KEY`。

### 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `SUB2API_BASE_URL` | 是 | - | sub2api 服务地址，不带尾部 `/`。 |
| `SUB2API_ADMIN_KEY` | 是 | - | sub2api Admin API Key，作为 `x-api-key` 请求头。 |
| `CONFIG_PATH` | 否 | `config.yaml` | 配置文件路径。 |
| `DB_PATH` | 否 | `/data/scheduler.db` | SQLite 数据库路径。 |
| `HEARTBEAT_FILE` | 否 | `/data/last_tick` | 每轮 tick 成功后写入的心跳文件。 |
| `UI_ENABLED` | 否 | `false` / compose 中为 `true` | 是否随 scheduler 后台启动 UI。 |
| `UI_HOST` | 否 | `0.0.0.0` | UI 监听地址。 |
| `UI_PORT` | 否 | `18080` | UI 监听端口。 |
| `UI_SESSION_SECRET` | 否 | 派生自 Admin Key | UI embedded session 签名密钥。 |
| `SENSITIVE_ACTION_PASSWORD` | 否 | `123456` | UI 中暂停/恢复、invite reset 等敏感操作密码；生产必须修改。 |
| `OPENAI_SUBSCRIPTION_BASE_URL` | 否 | `https://chatgpt.com/backend-api` | OpenAI 订阅信息查询 base URL。 |
| `ACCOUNT_PROFILE_TTL_MINUTES` | 否 | `720` | OpenAI profile/订阅缓存 TTL。 |
| `ACCOUNT_PROFILE_REFRESH_ENABLED` | 否 | `true` | 是否刷新 OpenAI profile/订阅缓存。 |
| `CODEX_INVITE_RESET_BASE_URL` | 否 | `https://chatgpt.com/backend-api` | Codex invite reset 辅助接口 base URL。 |

### 基础范围配置

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `platform` | `openai` in sample / `anthropic` in code default | 一个实例只管理一个平台，取值 `openai` 或 `anthropic`。 |
| `account_name_pattern` | `pay\d+` in sample | 账号名称正则过滤，`re.search` 匹配；空字符串表示全部匹配。 |
| `priority_bands` | `[1010, 1030, 1050, 1070, 1099]` | 五个受控档位，必须升序。 |
| `interval_minutes` | `15` in sample | 普通模式运行周期。 |
| `terminal_interval_minutes` | `15` | terminal 有活跃账号时的运行周期。 |

受控账号类型白名单：

| platform | 受控类型 |
|---|---|
| `openai` | `oauth` |
| `anthropic` | `oauth`, `setup-token` |

不符合白名单或名称正则的账号不会被探测、不会调档、不会写入本项目状态。

### 7D 目标配置

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `pacing_target_7d_utilization` | `97.0` | 普通 pacing 阶段的目标使用率。 |
| `drain_target_7d_utilization` | `99.4` | terminal drain 阶段的目标使用率。必须小于 hard cap。 |
| `hard_cap_7d_utilization` | `99.8` | 7D 硬保护阈值，达到后进入 `band_floor`。 |
| `target_7d_utilization` | `97.0` | legacy 兼容字段；未配置 pacing target 时才用于兼容。 |
| `protect_7d_utilization` | `97.0` | legacy/兼容字段；当前主策略以 pacing/drain target 为准。 |
| `safe_tail_hours` | `2.0` | pacing 预测中预留的尾部安全时间。 |
| `warmup_hours` | `24.0` | 窗口刚重置后的预热宽限，避免刚 reset 的账号被误判强烈欠量。 |

### Terminal drain 配置

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `terminal_drain_enabled` | `true` | 是否开启 terminal drain。 |
| `terminal_window_hours` | `36.0` | 距离 7D reset 小于等于该值时进入 terminal 模式。 |
| `terminal_final_margin_hours` | `0.25` | 冲刺目标预留尾部时间，默认 15 分钟。 |
| `terminal_min_runway_hours` | `0.25` | terminal 计算 required rate 时的最小跑道时间。 |
| `terminal_done_band_pp` | `0.10` | `7d >= drain_target - done_band` 后进入 done。 |
| `terminal_strong_gap_pp` | `1.5` | gap 达到该值进入 strong。 |
| `terminal_mild_gap_pp` | `0.4` | gap 达到该值进入 mild。 |
| `terminal_strong_required_rate_pph` | `0.35` | required rate 达到该值进入 strong。 |
| `terminal_mild_required_rate_pph` | `0.10` | required rate 达到该值进入 mild。 |
| `terminal_strong_pressure` | `1.8` | pressure 达到该值进入 strong。 |
| `terminal_mild_pressure` | `0.9` | pressure 达到该值进入 mild。 |
| `terminal_dynamic_load_factor_enabled` | `true` | 是否根据 gap/rate/pressure 动态计算 terminal load_factor。 |
| `terminal_strong_load_factor_multiplier` | `4.0` | strong 的 multiplier 上限。 |
| `terminal_mild_load_factor_multiplier` | `2.5` | mild 的 multiplier 上限。 |
| `terminal_normal_load_factor_multiplier` | `1.5` | terminal normal 的 multiplier 上限。 |
| `terminal_max_load_factor` | `100` | terminal load_factor 上限。 |

### Pacing boost / cooldown 配置

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `max_boost_ratio` | `0.15` | 普通 pacing 强 boost 最大比例。 |
| `mild_boost_ratio` | `0.35` | 普通 pacing mild boost 最大比例。 |
| `max_boost_min` | `1` | strong boost 最小名额。 |
| `boost_load_factor_multiplier` | `3.0` | pacing strong boost 的 load_factor 倍率。 |
| `mild_load_factor_multiplier` | `2.0` | pacing mild boost 的 load_factor 倍率。 |
| `max_load_factor` | `100` | pacing load_factor 上限。 |
| `strong_score_threshold` | `3.0` | strong boost catchup score 门槛。 |
| `strong_min_required_rate` | `0.6` | strong boost 最低 required rate，避免跑道充裕账号占用强推名额。 |
| `mild_score_threshold` | `1.0` | mild boost catchup score 门槛。 |
| `ahead_band_pp` | `3.0` | 使用率超过当前 pacing 目标多少百分点后进入 ahead protect。 |
| `cooldown_minutes` | `60` | 新 cooldown 持续时间。 |
| `cooldown_abs_rate_pph` | `1.2` | 绝对每小时增长 cap。 |
| `cooldown_required_rate_multiplier` | `2.5` | 根据 required rate 动态放大 cooldown cap。 |
| `cooldown_near_target_band_pp` | `2.0` | 接近目标时更容易触发 cooldown。 |
| `will_hit_goal_soon_hours` | `5.0` | 若按近期速度将在该时间内达到 pacing target，则保护。 |

### Active probe 配置

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `usage_stale_threshold_minutes` | `90` | 普通模式 usage 过期阈值。 |
| `max_active_probes_per_round` | `10` | 普通模式每轮最多 active probe 数。 |
| `terminal_usage_stale_threshold_minutes` | `20` | terminal 模式更严格的数据新鲜度阈值。 |
| `terminal_max_active_probes_per_round` | `50` | terminal 每轮 active probe 上限。 |
| `terminal_active_probe_ratio` | `0.50` | terminal 账号探测比例。 |
| `terminal_min_active_probes_per_round` | `20` | terminal active probe 最小名额。 |

### 5h 配置

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `enable_5h_guard` | `false` | 默认关闭，让 sub2api 原生运行时处理 5h 保护。 |
| `hard_cap_5h_utilization` | `98.0` | 仅当 `enable_5h_guard=true` 时生效，达到后进入 floor。 |

## 调度策略说明

### priority 档位

| priority | 名称 | 用途 |
|---:|---|---|
| `1010` | `band_boost` | terminal strong，临近 reset 且明显欠量。 |
| `1030` | `band_mild` | terminal mild，中度欠量。 |
| `1050` | `band_normal` | 默认 normal；普通 pacing 的 boost/mild 也保持此档。 |
| `1070` | `band_protect` | 已达 pacing target、ahead、cooldown 或 terminal done。 |
| `1099` | `band_floor` | hard cap 兜底保护。 |

建议把手动管理、不希望被本项目触碰的账号设置为名称不匹配 `account_name_pattern`，或者使用小于 1000 的 priority 并通过分组/名称过滤隔离。

### Pacing 模式

当账号距离 7D reset 还比较远时，策略目标是“平滑跟上 7D 进度”，而不是提前打满。

简化逻辑：

```text
if 7d >= hard_cap_7d:
    priority = 1099
elif 7d >= pacing_target_7d:
    priority = 1070
elif cooldown / ahead:
    priority = 1070
else:
    根据 pace_error、projected_gap、required_rate、recent_rate 计算 catchup_score
    strong/mild 账号仍保持 priority=1050，只提高 load_factor
```

普通 pacing 阶段不会把 boost 账号单独放进 `1010/1030`。这样可以避免某个落后账号独占全部新会话。

### Terminal drain 模式

当账号距离 7D reset 小于等于 `terminal_window_hours` 时，策略切换为“尽量用完”。

核心指标：

```text
gap = drain_target_7d_utilization - current_7d_used
deadline_h = max(remaining_h - terminal_final_margin_hours, terminal_min_runway_hours)
required_rate = gap / deadline_h
recent_rate = weighted(rate_1h, rate_5h)
pressure = required_rate / max(recent_rate, terminal_min_recent_rate_pph)
```

决策等级：

| level | 条件 | priority | load_factor |
|---|---|---:|---:|
| `strong` | gap / required_rate / pressure 任一达到 strong 门槛 | `1010` | 动态或 `base * strong_multiplier` |
| `mild` | 达到 mild 门槛 | `1030` | 动态或 `base * mild_multiplier` |
| `normal` | 轻微欠量 | `1050` | 动态或 `base * normal_multiplier` |
| `done` | 已接近 drain target | `1070` | `base` |

达到 `hard_cap_7d_utilization` 后直接进入 `1099`。

### 数据缺失 / stale 策略

- usage 缺失或 stale 时，优先在 runner 里 active probe。
- 非 terminal stale 默认 hold 当前 priority，避免冷账号死锁。
- terminal stale 时会把高于 base 的 load_factor 拉回 base；如果旧 7D 用量明显低于 drain target 且账号卡在 `1070/1099`，会先恢复到 `1050`，避免快刷新时继续卡保护档。

## Dashboard / UI

UI 可以随 scheduler 后台启动，也可以独立启动。

```yaml
ui_enabled: true
ui_host: 0.0.0.0
ui_port: 18080
```

Docker Compose 默认暴露：

```yaml
ports:
  - "${UI_PORT:-18080}:${UI_PORT:-18080}"
```

UI 后端提供：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/snapshot` | 返回 summary、账号状态、最近决策。 |
| `POST` | `/api/accounts/{id}/scheduler-control` | 暂停/恢复本项目对该账号的调度。 |
| `GET` | `/api/accounts/{id}/codex/invite-reset/status` | 查询 Codex invite reset 状态。 |
| `POST` | `/api/accounts/{id}/codex/invite-reset/invite` | 发送 invite reset。 |
| `POST` | `/api/accounts/{id}/codex/invite-reset/consume` | 消费 invite reset credit。 |

### UI 认证说明

当前 UI 的 `/` 与 `/api/*` 默认走 embedded auth：

- 请求需要携带 `token`、`src_host`、`ui_mode=embedded`、`user_id`，或等价请求头。
- 后端会调用 sub2api `/api/v1/user/profile` 校验 token 对应用户是否为 active admin。
- 校验成功后写入 `scheduler_embedded_admin` session cookie。

敏感操作还需要 JSON body 中携带：

```json
{
  "sensitive_password": "change-me"
}
```

生产环境务必设置 `SENSITIVE_ACTION_PASSWORD`，不要使用默认值 `123456`。

## 数据库与持久化

默认 SQLite 路径：

```text
/data/scheduler.db
```

主要表：

| 表 | 说明 |
|---|---|
| `account_state` | 每个账号的 last priority、last usage、EWMA、cooldown、probe failure、terminal level 等状态。 |
| `usage_sample` | usage 采样历史，用于计算近 5h burn 和排查趋势。 |
| `decision_log` | 每轮每账号决策，包含 priority / load_factor 变化、reason、pacing/drain 指标。 |
| `account_control` | UI 暂停/恢复开关，仅本项目内部使用，不写回 sub2api。 |
| `account_profile_cache` | OpenAI profile / subscription 展示缓存。 |
| `schema_migrations` | SQLite schema 迁移记录。 |

保留期配置：

```yaml
sample_retention_days: 14
decision_retention_days: 30
state_retention_days: 30
```

备份：

```bash
mkdir -p backup
sqlite3 data/scheduler.db ".backup 'backup/scheduler-$(date +%F-%H%M%S).db'"
```

## 运行建议

### 首次上线

1. 先设置较窄的 `account_name_pattern`，只接管少量测试账号。
2. 先跑 `--once`，确认只更新了预期账号。
3. 检查日志中的 `managed`、`eligible`、`reason`、`priority`、`load_factor`。
4. 再逐步扩大账号名称匹配范围。

### 与手动账号隔离

- 推荐手动账号名称不匹配 `account_name_pattern`。
- 如果使用 sub2api advanced scheduler，建议手动账号和受控账号分组隔离，避免 priority min-max 归一化压缩受控档位区分度。
- 需要人为优先的账号可以使用 `<1000` 的 priority，但仍建议名称过滤排除。

### 关于 5h 保护

本项目默认 `enable_5h_guard=false`。这意味着外部 scheduler 不会因为 5h 使用率而提前降档，5h 防爆应交由 sub2api 原生运行时机制承担。

如果你的 sub2api 部署没有可靠的 5h 保护，才考虑开启：

```yaml
enable_5h_guard: true
hard_cap_5h_utilization: 98.0
```

## 常见调参场景

### 7D 临近刷新仍然用不完

优先调这些：

```yaml
terminal_window_hours: 48.0
drain_target_7d_utilization: 99.5
terminal_done_band_pp: 0.02
terminal_strong_gap_pp: 1.0
terminal_strong_required_rate_pph: 0.25
terminal_strong_pressure: 1.3
terminal_strong_load_factor_multiplier: 5.0
terminal_max_active_probes_per_round: 80
terminal_active_probe_ratio: 0.75
terminal_interval_minutes: 10
```

还要确认：

- sub2api 账号本身 `concurrency` 是否足够。
- 总请求量是否足够覆盖账号池总剩余额度。
- 是否有大量 sticky session 仍在老账号上。
- OpenAI advanced scheduler 是否削弱了 priority 档位效果。

### terminal drain 太激进

```yaml
drain_target_7d_utilization: 99.2
terminal_done_band_pp: 0.10
terminal_strong_gap_pp: 2.0
terminal_strong_pressure: 2.2
terminal_strong_load_factor_multiplier: 3.0
terminal_max_active_probes_per_round: 30
```

### 普通阶段补量过度集中

```yaml
max_boost_ratio: 0.10
mild_boost_ratio: 0.25
boost_load_factor_multiplier: 2.0
mild_load_factor_multiplier: 1.5
cooldown_minutes: 90
```

### 大账号池 stale 太多

```yaml
max_active_probes_per_round: 30
terminal_max_active_probes_per_round: 100
terminal_min_active_probes_per_round: 30
terminal_active_probe_ratio: 0.75
usage_stale_threshold_minutes: 60
terminal_usage_stale_threshold_minutes: 15
```

### 只想观察，不想实际改 sub2api

当前代码没有 dry-run 开关。临时方案：

1. 使用极窄的 `account_name_pattern` 匹配不存在的账号。
2. 或在测试 sub2api 环境运行。
3. 或给本项目加 dry-run：跳过 `bulk_update_accounts()`，仍写 `decision_log`。

## 测试

后端单测：

```bash
cd backend
uv sync
uv run pytest -q
```

或不使用 uv：

```bash
cd backend
PYTHONPATH=src pytest -q
```

前端构建：

```bash
cd frontend
npm ci
npm run build
```

Docker 构建：

```bash
docker compose build
```

## 排障

### 启动时报 `SUB2API_BASE_URL and SUB2API_ADMIN_KEY are required`

确认 `.env` 被 docker compose 读取，或直接导出环境变量：

```bash
export SUB2API_BASE_URL=https://your-sub2api.example.com
export SUB2API_ADMIN_KEY=your-admin-api-key
```

### 日志里 `managed=0`

检查：

- `platform` 是否正确。
- 账号类型是否在白名单内。
- `account_name_pattern` 是否匹配账号名称。

可以临时设置：

```yaml
account_name_pattern: ''
```

### 账号一直 `no_data_hold` / `stale_hold`

检查：

- active probe 是否失败。
- sub2api Admin API 是否能访问 `/api/v1/admin/accounts/{id}/usage?source=active&force=true`。
- `max_active_probes_per_round` 是否太小。
- 账号是否没有真实流量，导致被动 usage 长期不存在。

### terminal 阶段仍然没有抢到流量

检查：

- sub2api 账号是否 `eligible`。
- 是否被 5h / 429 / overload / temp unschedulable 阻断。
- 是否有 advanced scheduler，priority 不再是硬隔离。
- `concurrency` 是否过低；`load_factor` 不会突破真实并发槽位。
- 总请求量是否不足。

### UI 打不开或返回认证错误

当前 UI 默认要求 embedded auth。确认访问时是否带上来自 sub2api 管理端的 token / src_host / ui_mode / user_id。敏感操作还需要 `SENSITIVE_ACTION_PASSWORD`。

### Docker healthcheck 不健康

healthcheck 依赖 `HEARTBEAT_FILE` 最近更新时间。检查：

```bash
docker exec -it sub2api-account-scheduler ls -l /data/last_tick
docker logs --tail=200 sub2api-account-scheduler
```

如果 scheduler 异常退出或 tick 长时间失败，heartbeat 不会刷新。

## 开发说明

### 目录结构

```text
.
├── backend/
│   ├── src/scheduler/
│   │   ├── __main__.py             # CLI 入口
│   │   ├── api.py                  # sub2api Admin API client 与账号解析
│   │   ├── config.py               # 配置加载与校验
│   │   ├── models.py               # AccountSnapshot / State / Decision 等模型
│   │   ├── policy.py               # 纯函数决策核心
│   │   ├── runner.py               # tick 编排
│   │   ├── store.py                # SQLite schema / 读写 / 迁移
│   │   ├── ui.py                   # Dashboard HTTP server
│   │   ├── openai_subscription.py  # OpenAI 订阅信息缓存/刷新
│   │   └── codex_invite.py         # Codex invite reset 辅助接口
│   └── tests/
├── frontend/
│   ├── src/
│   └── vite.config.ts
├── docs/
│   └── ACCOUNT_PRIORITY_7D_OPTIMIZATION_CN.md
├── config.yaml
├── docker-compose.yml
└── Dockerfile
```

### 代码分层

- `policy.py` 必须保持纯函数，不做 IO，便于单测覆盖策略边界。
- `runner.py` 负责所有外部编排：拉账号、probe、调用策略、bulk update、落库、心跳。
- `api.py` 只封装 sub2api Admin API 请求与字段归一化。
- `store.py` 负责 schema 迁移、状态和日志。
- UI 的暂停开关只写 `account_control`，不会改 sub2api 账号本身。

### 新增策略时的测试建议

至少覆盖：

- hard cap 直接进 floor。
- pacing boost / mild / normal / protect。
- terminal strong / mild / normal / done。
- stale/no-data hold 与 terminal stale normalize。
- load_factor 不残留，尤其是 protect/floor 回 normal 的防抖路径。
- active probe budget 与 terminal 优先级。
- config validation。

## 安全说明

- `SUB2API_ADMIN_KEY` 权限很高，不要写入 `config.yaml`，不要提交到 Git。
- Dashboard 暴露到公网前必须设置认证、反代访问控制和 `SENSITIVE_ACTION_PASSWORD`。
- `SENSITIVE_ACTION_PASSWORD` 默认值是 `123456`，生产必须覆盖。
- SQLite 包含账号决策历史和部分 profile 展示缓存，应限制文件权限并定期备份。
- 本项目会批量修改 sub2api 账号的 `priority` 与 `load_factor`，首次上线请从少量账号开始。

## License

当前仓库未显式声明 License。发布前建议补充适合项目的开源许可证。
