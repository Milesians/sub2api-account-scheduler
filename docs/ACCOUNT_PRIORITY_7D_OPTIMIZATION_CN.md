# 账号 7day 额度优先级动态优化设计

## 目标

通过管理员接口定时查询账号 5h / 7day 用量，并动态调整账号 `priority`，让账号在 7day 刷新前尽量接近满用，同时避免单个账号被快速打满。

适用平台（每个控制器实例管理一个平台，由 `platform` 配置）：

- `anthropic`：Claude OAuth / Setup-Token 订阅账号
- `openai`：ChatGPT Codex OAuth 订阅账号（同样有 5h / 7day 双窗口）

可验证目标：

- 初期：账号 7day 到期前使用率收敛到 `90% - 99%`，运行拿到实际方差后再收紧
- 数据支撑后：收敛到 `95% - 99.2%`
- 不因为补量策略显著增加 5h 触顶
- 不因为补量策略显著增加 429
- 不让少数账号长期承担大部分流量
- 不侵入 sub2api 请求级调度逻辑，只通过 Admin API 更新 `priority`

## 非目标

- 不修改账号 key、token、状态或分组
- 不绕过 sub2api 原有并发、限流、过载、LRU 机制
- 不直接指定每个请求使用哪个账号
- 不把 `priority` 当成精确流量权重
- 不用小时级 priority 做 5h 硬保护（硬保护交给 sub2api 请求级机制）

## sub2api 实际调度机制（源码核实）

`priority` 的语义：数值越小，账号越优先。

### Anthropic 主路径：分层过滤

Anthropic 账号的非粘性选择只有一条主路径（`gateway_service.go` 分层过滤）：

```text
1. filterByMinPriority: 只保留 priority 最小的账号集合
2. filterByMinLoadRate: 在其中保留负载率最低的集合
3. selectByLRU: 选最久未使用的账号（同组内随机打乱）
```

只有负载批量查询失败时才退化为 legacy 路径（priority + LRU）。

关键特性：**priority 是严格档位，不是权重**。只要低档位（数值小）还有可用账号，
高档位一个新会话都分不到。priority 10 与 20 的差别是"全有或全无"的排队顺序，
不是 2:1 的流量比例。

### OpenAI 路径：默认排序首键 / 可选评分制

OpenAI 账号（`openai_account_scheduler.go` / `openai_gateway_service.go`）有两条路径：

**默认路径**（`openai_advanced_scheduler_enabled` 未开启，出厂默认）：
粘性会话之后，候选按 `(priority asc, loadRate asc, lastUsedAt asc)` 排序
并在同序组内随机打乱。priority 是首要排序键——**效果与 Anthropic 的严格
档位等价**：低档位打满前高档位分不到新会话。本方案的档位语义直接成立。

**advanced 路径**（后台设置开启后）：评分制加权
`score = w_p·priorityFactor + w_load·… + w_queue·… + w_err·… + w_ttft·…`
（默认权重 1.0 / 1.0 / 0.7 / 0.8 / 0.5），其中 priorityFactor 是候选集内
**min-max 归一化**，之后 TopK(7) 加权随机选择。此时档位退化为"单调偏好"：
方向仍正确（数值小者得分高），但不再是硬隔离——补量档不能吸走全部新会话，
保护档也会继续少量接活，收敛速度变慢。两点影响：

- 档位绝对值与间距无关紧要（归一化只看相对位置），1000 基准不受影响
- 若手动账号（priority < 1000）与受控账号同组参与调度，min-max 会被手动
  账号拉宽区间，受控档位之间的区分度被压缩——advanced 模式下建议
  手动账号与受控账号分组隔离

### 粘性会话的强约束（两平台一致）

- Anthropic `stickySessionTTL = 1h` 且每次选中续期：活跃会话基本永久粘住账号；
  OpenAI 同样有 sticky session（外加 `previous_response_id` 粘连），结论相同
- 粘性路径**完全绕过 priority**
- 只有账号变为不可调度（限流 / 状态异常 / 窗口费用打满 / 临时不可调度）
  才会触发解绑

因此：**priority 调整只影响新会话的分配**。把账号降到 1070 / 1099 卸不掉
存量粘性流量，收敛速度受新会话到达率限制，控制器必须接受这个滞后。

### sub2api 已有的请求级硬保护

以下机制是请求级实时生效的，比小时级控制器粒度细得多：

| 机制 | 平台范围 | 行为 |
|---|---|---|
| `window_cost_limit` 三态 | 仅 Anthropic OAuth/SetupToken | 费用 < 阈值可调度；阈值~阈值+预留仅粘性；超过不可调度 |
| RPM 红黄区 | 仅 Anthropic OAuth/SetupToken | 黄区仅粘性，红区不可调度 |
| unified-status rejected | 仅 Anthropic | 自动限流到 5h reset |
| 429 / overload 处理 | 两平台 | 自动设置 `rate_limit_reset_at` / `overload_until` |

平台差异决定了控制器 5h 规则的职责：

- **Anthropic**：5h 硬保护应通过给账号配置 `window_cost_limit` 等实现，
  控制器的 5h 规则只作为最后兜底
- **OpenAI**：**没有费用窗口三态这类请求级 5h 保护**（只有 429/403 自动限流
  兜底），控制器的 `hard_cap_5h_utilization` 是主要兜底保护，必要时应调低
  这个阈值

## 核心结论

不能简单执行：

```text
7day 用量最低的账号 -> priority = 1
```

由于 priority 是严格档位，这会让该账号吸走全部新会话直到被打满，导致：

- 单账号 5h 快速触顶
- 粘性会话固化热点（1h TTL 续期，降档也卸不掉）
- 429 增加、可调度账号数量下降

正确方式是：

```text
外部控制器负责把账号放入有限 priority 档位（控制节奏）
sub2api 原有调度负责同档位内的负载、LRU 分散
sub2api 请求级机制（window_cost_limit / RPM / 限流）负责硬保护
```

## 总体方案

新增一个外部小时级控制器：`7D Pacing Controller`。

控制器每小时执行一次：

1. 一次 `GET /api/v1/admin/accounts` 拉取账号列表（含被动用量数据）
2. 按 `account_name_pattern`（正则）过滤出**受控账号**，
   名称不匹配的账号完全不参与（不探测、不调档、不入库）
3. 仅对被动数据缺失/过期且影响决策的账号做 active 探测
4. 计算每个账号是否落后于 7day 使用节奏
5. 预测账号到期时的最终使用率
6. 结合 5h、7day、最近小时消耗做保护
7. 将账号映射到有限 priority 档位
8. 按档位通过 bulk-update 批量更新 `priority`

控制器只调整 `priority`，不直接参与单请求调度。

## 数据获取

### 主数据源：账号列表（一次调用）

```http
GET /api/v1/admin/accounts?platform={anthropic|openai}
```

通用字段（两平台一致）：`priority`、`type`、`schedulable` / `status`、
`rate_limit_reset_at` / `overload_until`、`temp_unschedulable_until`。

被动用量字段按平台不同（**注意单位差异**）：

| 数据 | anthropic | openai (Codex) |
|---|---|---|
| 5h 使用率 | `extra.session_window_utilization`（0-1） | `extra.codex_5h_used_percent`（0-100） |
| 7d 使用率 | `extra.passive_usage_7d_utilization`（0-1） | `extra.codex_7d_used_percent`（0-100） |
| 7d 重置时间 | `extra.passive_usage_7d_reset`（Unix 秒） | `extra.codex_7d_reset_at`（RFC3339） |
| 5h 重置时间 | 账号字段 `session_window_end` | `extra.codex_5h_reset_at`（RFC3339） |
| 采样时间 | `extra.passive_usage_sampled_at`（RFC3339） | `extra.codex_usage_updated_at`（RFC3339） |

被动数据均来自请求响应头被动采样（anthropic: `anthropic-ratelimit-unified-*`；
openai: Codex rate-limit 头），**零额外成本**，但只有近期有流量的账号才有
新鲜数据。控制器内部统一换算为 0-100 百分比。

受控账号类型白名单（其余类型无 5h/7d 窗口概念，直接排除）：

```text
anthropic -> oauth / setup-token
openai    -> oauth（apikey / upstream 账号无 codex 窗口数据）
```

### 补充数据源：active 探测（按需、少量）

被动数据缺失或超过 `usage_stale_threshold_minutes` 时：

```http
GET /api/v1/admin/accounts/:id/usage?source=active&force=true
```

注意：

- active force 会真实调用 Anthropic 接口，**禁止每小时全量 force**
- 只探测"数据过期且会影响档位决策"的账号
- 每轮 active 探测数量设上限（如 10 个），超出部分下轮再探测
- 主动查询结果会自动回写被动缓存（`syncActiveToPassive`），下轮 list 即可见

### 冷账号探测（重要）

被动数据来源于流量，**没有流量的账号没有被动数据**。而最落后、最需要补量的
恰恰是冷账号。因此 usage 缺失时的处理必须是：

```text
usage 缺失/过期 -> 优先 active 探测（占用本轮探测名额）
               -> 探测成功则正常参与档位计算
               -> 探测失败才保守处理（保持当前档位，不降档）
```

禁止"usage 缺失 -> 直接降到保护档"——这会让冷账号更没流量、数据永远缺失，
形成死锁。

## priority 档位

采用有限档位，保留同档位内的负载分散和 LRU 能力。由于档位是严格排队顺序
（相邻档位仅在高档全部打满时才有区别），档位过多没有意义，采用 5 档。

档位基准放在 **1000**：手动添加、不受控制器管理的账号使用 < 1000 的
priority 即可，既不会与受控档位冲突，也天然比受控账号更优先
（sub2api priority 数值越小越优先）；配合名称过滤，控制器不会触碰它们。

| priority | 含义 | 说明 |
|---:|---|---|
| 1010 | 补量 | 落后账号，受 boost 名额限制 |
| 1030 | 轻微落后 | 温和补量，等待轮换 |
| 1050 | 正常 | 默认节奏 |
| 1070 | 超前保护 | 减少新会话分配 |
| 1099 | 兜底保护 | 接近上限，不应继续吸流（注意：存量粘性流量仍会继续） |

## 控制器状态

控制器需要保存每个账号的历史采样，用于计算消耗速度和防止热点。

```json
{
  "account_id": {
    "last_priority": 1050,
    "last_7d_used": 42.3,
    "last_5h_used": 30.1,
    "last_7d_reset_at": "2026-06-15T10:00:00Z",
    "last_sampled_at": "2026-06-12T10:00:00Z",
    "hourly_burn_ewma": 0.35,
    "cooldown_until": null,
    "last_boost_at": "2026-06-12T09:00:00Z",
    "probe_failures": 0
  }
}
```

持久化采用 SQLite 单文件库，上述字段落在 `account_state` 表（见「工程实现」章节）。

## 7day 节奏计算

每个账号的 7day reset 时间可能不同，所以必须按账号自己的窗口计算。

参数：

```text
window_hours = 168
target_end = 97.0        # 初期保守值，运行观察后再上调
hard_cap_7d = 99.2
```

当前窗口进度会预留 `safe_tail_hours`，避免把补量留到 reset 前最后几小时：

```text
remaining_hours = (passive_usage_7d_reset - now) / 3600
runway_hours = max(remaining_hours - safe_tail_hours, 1)
elapsed_hours = clamp(window_hours - remaining_hours, 0, window_hours)
target_now = target_end * min(elapsed_hours / (window_hours - safe_tail_hours), 1)
```

当前节奏差：

```text
pace_error = target_now - current_7d_used
```

`pace_error > 0` 表示账号低于当前应有进度，需要补量。

## 到期预测

用最近采样计算小时消耗速度：

```text
hourly_burn = (current_7d_used - last_7d_used) / hours_since_last_sample
```

用 EWMA 平滑：

```text
hourly_burn_ewma = 0.7 * old_hourly_burn_ewma + 0.3 * hourly_burn
```

同时计算近 5 小时平均速度（来自 `usage_sample` 历史）并与最近 1 小时速度加权：

```text
recent_rate = 0.65 * rate_1h + 0.35 * rate_5h
projected_final = current_7d_used + recent_rate * runway_hours
projected_gap = target_end - projected_final
required_rate = max(0, target_end - current_7d_used) / runway_hours
```

补量分数：

```text
catchup_score =
  urgency * (
    max(0, pace_error) +
    0.7 * max(0, projected_gap) +
    6.0 * max(0, required_rate - recent_rate)
  )
```

`urgency = 1 + clamp((12 - remaining_hours) / 12, 0, 1)`，越接近 reset 越放大缺量风险。
`catchup_score >= 3.0` 进入 strong 候选，`>= 1.0` 进入 mild 候选，并受池级名额限制。

## 热点保护

为了防止账号被快速打满，需要限制单小时增长。

理论需要速度：

```text
required_rate = (target_end - current_7d_used) / max(remaining_hours, 1)
```

动态 cooldown 上限：

```text
safe_hour_cap = max(1.2, required_rate * 2.5)
```

如果最近一小时 7day 增长超过 `safe_hour_cap`，且账号不是明显落后状态：

```text
target_priority = 1070
cooldown = 1 hour
```

即使账号仍然落后，也先冷却一轮。注意冷却只能挡住新会话，
存量粘性流量会继续消耗，所以 `safe_hour_cap` 要留有余量。

## 5h 规则

anthropic 平台 5h 硬保护由 sub2api 请求级机制承担（给账号配置
`window_cost_limit`、`base_rpm`，以及 unified-status rejected 自动限流），
控制器规则仅为参考与兜底；**openai 平台没有请求级费用保护，本规则即主保护**。
控制器的 5h 规则只做两件事：

```text
if five_hour_used >= 92:
    禁止进入 priority 1010 / 1030（不主动吸流）

if five_hour_used >= 98:
    priority = 1099（兜底，与请求级保护冗余）
```

如果 5h 距离 reset 小于 30 分钟，可以放宽"禁止 boost"限制，
但不放宽 98 兜底。

## 7day 保护

```text
if seven_day_used >= 99.2:
    priority = 1099

else if seven_day_used >= 97:
    priority = 1070

else if seven_day_used >= 95:
    禁止进入 priority 1010
```

注意 Anthropic 还有独立的 `seven_day_sonnet` 窗口（active 查询返回
`seven_day_sonnet` 字段，**openai 无此窗口**）。混合模型流量下 Sonnet
窗口可能先满，保护判断取两个 7d 窗口的 **max**；至少要纳入监控。

源码核实：`syncActiveToPassive` 只回写 5h / 7d 两个窗口，**被动缓存不含
sonnet**。因此 sonnet 用量仅在该账号被 active 探测的轮次可得（直接解析
探测响应），其余轮次按 0 处理——sonnet 保护是机会性的，主保护靠 7d 总窗口。

## boost 名额限制

每小时只允许一部分账号进入补量档。

```text
max_boosted = max(1, ceil(eligible_accounts * 0.15))
```

例如有 40 个可调度账号，则每小时最多 6 个账号进入 `priority=1010`。
其它落后账号最多进入 `priority=1030`，等待下一轮轮换。

由于档位是严格排队，补量档内必须保持多个账号（依靠档内负载/LRU 分散），
禁止只放 1 个账号进 1010 档（除非 eligible 总数就是 1）。

## 防抖策略

默认每小时最多移动一个档位：

```text
1010 <-> 1030 <-> 1050 <-> 1070 <-> 1099
```

两个例外允许跳档：

1. **硬保护**：触发 99 / 70 保护规则时直接到位
2. **窗口尾部紧急补量**：`remaining_hours < 12` 且明显落后时，
   允许直接进入排名允许的最高补量档（逐档爬升 3 小时太慢，来不及）

## 决策流程

```pseudo
accounts = GET /api/v1/admin/accounts?platform={cfg.platform}  # 一次调用（分页拉全）
managed  = filter(accounts, type 在平台白名单 && name 匹配 account_name_pattern)
eligible = filter(managed, schedulable && status==active && 未限流/过载)

# 第一步：补数据
stale = [a for a in eligible if passive 数据缺失或过期]，按 sampled_at 最旧优先
for a in stale[:max_active_probes]:
    usage = GET /accounts/:id/usage?source=active&force=true
    if 成功: 探测结果直接合并进本轮快照（含 sonnet 窗口）
    else:   probe_failures += 1   # 数据仍缺失 -> 保持当前档位，不降档

# 第二步：硬规则（允许跳档直接到位）
for a in eligible:
    if 数据缺失或超过 stale 阈值:
        target[a] = current[a]; continue          # no_data_hold / stale_hold

    update hourly_burn_ewma(a)   # 7d reset 翻转或间隔过短则跳过本次 burn
    seven_day = max(a.seven_day_used, a.seven_day_sonnet_used or 0)

    if seven_day >= 99.2 or a.five_hour_used >= 98:
        target[a] = 1099; continue
    if seven_day >= 97:
        target[a] = 1070; continue
    if a.cooldown_until > now:
        target[a] = max(current[a], 1070); continue
    if recent_hour_burn(a) > safe_hour_cap(a) and a 没有明显落后:
        target[a] = 1070; a.cooldown_until = now + 1h; continue
    if 首次接管且 priority 不在受控档位:
        target[a] = 1050; continue

    compute catchup_score(a)

# 第三步：排名分档
ranked = sort remaining by (-catchup_score, seven_day, remaining_hours, last_boost_at)
strong_candidates = [a for a in ranked if catchup_score(a) >= 3.0]
mild_candidates = [a for a in ranked if catchup_score(a) >= 1.0]

for a in remaining:
    if a in strong 名额:
        target[a] = 1010                         # 仅 1 个 strong 候选时降为 1030
    else if a in mild 名额:
        target[a] = 1030
    else:
        target[a] = 1050

    # 防抖：每小时一档；窗口尾部（remaining < 12h）补量方向允许跳档
    if not (remaining_hours(a) < 12 and target[a] < current[a]):
        target[a] = one_band_step(current[a], target[a])

# 第四步：按档位批量更新（只发送档位变化的账号）
for band in [1010, 1030, 1050, 1070, 1099]:
    ids = [a.id for a in eligible if target[a] == band and target[a] != current[a]]
    if ids:
        POST /api/v1/admin/accounts/bulk-update {account_ids: ids, priority: band}
```

每轮最多 5 次 bulk-update 调用 + 少量 active 探测，而不是逐账号 N 次请求。

## Admin API 汇总

认证（源码核实）：请求带 `x-api-key: <admin-api-key>` header。
list 接口分页（`page` / `page_size`，单页上限 1000），响应统一包装为
`{"code": 0, "message": "...", "data": {...}}`，列表数据在 `data.items`。

| 用途 | 接口 |
|---|---|
| 拉取账号+被动用量 | `GET /api/v1/admin/accounts?platform=anthropic&page=N&page_size=1000` |
| 按需主动探测 | `GET /api/v1/admin/accounts/:id/usage?source=active&force=true` |
| 批量更新档位 | `POST /api/v1/admin/accounts/bulk-update` |

字段单位注意：anthropic 被动字段 `extra.*_utilization` 是 **0-1**；
openai 被动字段 `codex_*_used_percent` 与两平台 active 探测响应的
`utilization` 都是 **0-100**。控制器内部统一为 0-100。

bulk-update 请求体：

```http
POST /api/v1/admin/accounts/bulk-update
Content-Type: application/json

{
  "account_ids": [12, 15, 23],
  "priority": 30
}
```

`priority` 为指针语义的局部更新，不会覆盖其它字段（单账号
`PUT /api/v1/admin/accounts/:id` 同理，可作为零散更新备用）。

priority 更新通过 scheduler outbox 同步到调度快照，默认 1 秒内生效。

## 推荐参数

```yaml
platform: anthropic              # anthropic / openai，每个实例管理一个平台
account_name_pattern: ""         # 受控账号名称过滤（正则）；空 = 全部受控
interval_minutes: 60
target_7d_utilization: 97.0      # 初期保守值，运行观察后再上调
hard_cap_7d_utilization: 99.2
hard_cap_5h_utilization: 98.0
protect_7d_utilization: 97.0
max_boost_ratio: 0.15
mild_boost_ratio: 0.35
max_boost_min: 1
max_active_probes_per_round: 10
usage_stale_threshold_minutes: 90
cooldown_minutes: 60
safe_tail_hours: 2.0
strong_score_threshold: 3.0
mild_score_threshold: 1.0
ahead_band_pp: 3.0
cooldown_abs_rate_pph: 1.2
cooldown_required_rate_multiplier: 2.5
cooldown_near_target_band_pp: 2.0
will_hit_goal_soon_hours: 5.0
emergency_window_hours: 12       # 剩余小时低于此值允许跳档补量
emergency_projected_end_threshold: 94.0
emergency_final_gap_pp: 5.0
emergency_rate_gap_pph: 0.8
priority_bands: [1010, 1030, 1050, 1070, 1099]   # 基准 1000，手动账号用 < 1000

# 存储与老化（工程参数）
db_path: /data/scheduler.db
heartbeat_file: /data/last_tick
sample_retention_days: 14      # usage_sample 保留期
decision_retention_days: 30    # decision_log 保留期
state_retention_days: 30       # 账号消失后 account_state 行保留期
```

## 工程实现

### 技术栈

| 项 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.13 | |
| 依赖/构建 | uv（`uv.lock` 锁定，构建后端 `uv_build`） | |
| HTTP | requests | 每小时一轮、串行少量请求，同步足够 |
| 存储 | SQLite（标准库 `sqlite3`） | 单文件、单实例，不用 ORM |
| 配置 | PyYAML + 环境变量 | 敏感项仅环境变量 |
| 调度 | 进程内循环 sleep | 不引入 APScheduler / cron |
| 测试 | pytest | 重点覆盖 policy 纯函数 |

刻意不引入：Web 框架、ORM、消息队列、分布式锁（YAGNI）。

### 项目结构

```text
sub2api-account-scheduler/
├── pyproject.toml
├── uv.lock
├── config.yaml              # 策略参数（即「推荐参数」，非敏感）
├── .env.example             # SUB2API_BASE_URL / SUB2API_ADMIN_KEY
├── Dockerfile
├── docker-compose.yml
├── docs/
├── src/scheduler/
│   ├── __init__.py
│   ├── __main__.py          # python -m scheduler [--once|--migrate-db] [--config PATH]
│   ├── models.py            # AccountSnapshot / AccountState / Decision
│   ├── config.py            # 加载 config.yaml，env 覆盖
│   ├── api.py               # Admin API 客户端（list / active 探测 / bulk-update）
│   ├── store.py             # SQLite：建表、状态读写、采样、决策日志、老化
│   ├── policy.py            # 档位决策纯函数（无 IO）
│   └── runner.py            # tick 编排、主循环、心跳
└── tests/
    ├── test_policy.py
    └── mock_server.py       # 本地联调用 mock sub2api
```

`policy.py` 保持纯函数：输入账号快照 + 历史状态 + 配置 + now，输出目标
档位与原因，决策逻辑全部可离线单测；IO 集中在 `api.py` / `store.py`。

### 配置与环境变量

优先级：环境变量 > `config.yaml` > 内置默认值。

| 环境变量 | 必填 | 说明 |
|---|---|---|
| `SUB2API_BASE_URL` | 是 | sub2api 服务地址 |
| `SUB2API_ADMIN_KEY` | 是 | 仅允许环境变量传入，不写配置文件、不打日志 |
| `DB_PATH` | 否 | 覆盖 `db_path`，默认 `/data/scheduler.db` |
| `HEARTBEAT_FILE` | 否 | 覆盖 `heartbeat_file`，容器 healthcheck 读同名变量 |
| `CONFIG_PATH` | 否 | config.yaml 路径，默认 `./config.yaml`（等价 `--config`） |

### SQLite 存储设计

连接初始化：

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
```

时间一律存 UTC ISO8601 文本（如 `2026-06-12T10:00:00Z`），老化与窗口查询统一用
Python 计算 cutoff 后按同格式字符串比较。

迁移采用轻量内置版本表，不引入 ORM / Alembic：

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);
```

启动 `Store` 时会自动应用缺失迁移；也可以显式运行：

```bash
python -m scheduler --migrate-db
```

核心表：

```sql
-- 每账号控制状态：UPSERT 覆盖，行数 = 账号数，不随时间增长
CREATE TABLE IF NOT EXISTS account_state (
    account_id        INTEGER PRIMARY KEY,
    last_priority     INTEGER,
    last_7d_used      REAL,
    last_5h_used      REAL,
    last_7d_reset_at  TEXT,
    last_sampled_at   TEXT,
    hourly_burn_ewma  REAL NOT NULL DEFAULT 0,
    cooldown_until    TEXT,
    last_boost_at     TEXT,
    probe_failures    INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL
);

-- 用量采样历史：用于观测回溯和近 5h burn 计算，按期老化
CREATE TABLE IF NOT EXISTS usage_sample (
    account_id            INTEGER NOT NULL,
    sampled_at            TEXT    NOT NULL,
    seven_day_used        REAL,
    seven_day_sonnet_used REAL,
    five_hour_used        REAL,
    recent_hour_burn      REAL,
    recent_5h_burn        REAL,
    seven_day_reset_at    TEXT,
    source                TEXT NOT NULL,        -- passive / active
    PRIMARY KEY (account_id, sampled_at)
);

-- 决策日志：每轮决策全量记录，按期老化
CREATE TABLE IF NOT EXISTS decision_log (
    id               INTEGER PRIMARY KEY,
    run_id           TEXT    NOT NULL,          -- 每轮 tick 一个 UUID
    account_id       INTEGER NOT NULL,
    decided_at       TEXT    NOT NULL,
    current_priority INTEGER,
    target_priority  INTEGER,
    catchup_score    REAL,
    reason           TEXT,
    seven_day_used        REAL,
    seven_day_sonnet_used REAL,
    five_hour_used        REAL,
    recent_hour_burn      REAL,
    recent_5h_burn        REAL,
    safe_hour_cap         REAL,
    target_now            REAL,
    projected_end         REAL,
    required_rate         REAL,
    recent_rate           REAL,
    remaining_hours       REAL,
    usage_source          TEXT
);
CREATE INDEX IF NOT EXISTS idx_decision_account
    ON decision_log(account_id, decided_at);
```

决策依赖 `account_state`（上次采样基线 + EWMA + last_boost_at）和
`usage_sample` 的近 5 小时采样。`usage_sample` 被老化后不会破坏安全保护，
只是短期内退回 EWMA 作为平滑速度近似。

### 数据老化

控制器数据全部是临时数据，必须有界。每轮 tick 末尾顺带执行清理，
不设独立清理任务：

```sql
DELETE FROM usage_sample  WHERE sampled_at < :sample_cutoff_iso;
DELETE FROM decision_log  WHERE decided_at < :decision_cutoff_iso;
DELETE FROM account_state WHERE updated_at < :state_cutoff_iso;
```

| 表 | 保留期 | 增长速度 | 保留期依据 |
|---|---|---|---|
| `usage_sample` | 14 天 | 每账号每小时 1 行 | 覆盖完整 7d 窗口 + 一倍余量 |
| `decision_log` | 30 天 | 每账号每小时 1 行 | 满足上线观察期的回溯需求 |
| `account_state` | 30 天未更新 | 不增长 | 清理已在 sub2api 删除的账号残留 |

规模估算（按 40 账号）：`usage_sample` 稳态约 1.3 万行，`decision_log`
约 2.9 万行，库文件个位数 MB。DELETE 释放的页面由 SQLite 自动复用，
无需 VACUUM、无需额外索引调优；保留期全部参数化（见「推荐参数」）。

### 实现边界

策略章节之外，实现时必须处理的边界：

- **平台字段差异**：解析层（`api.py`）按 platform 分支处理单位（0-1 vs
  0-100）与时间格式（Unix 秒 vs RFC3339）；决策层（`policy.py`）完全
  平台无关。
- **7d 窗口翻转**：`seven_day_reset_at` 与上次记录不一致说明窗口已重置，
  本次跳过 burn 计算（避免负增量污染 EWMA），仅刷新基线；EWMA 保留旧值
  （消耗速度跨窗口大体连续）。
- **负增量**：reset 未变但用量回落（active 校正、上游抖动），按
  `burn = max(0, delta) / Δh` 处理。
- **首次见到的账号**：无历史状态，EWMA 从 0 起步，首轮只采样建立基线，
  不参与 boost 排名（数据不足）。
- **时区**：进程、SQLite、日志全部 UTC；sub2api 返回的 Unix 秒 /
  RFC3339 字段统一先转 UTC 再落库。

### 运行形态

```bash
python -m scheduler          # 常驻：启动即跑首轮，之后每 interval_minutes 一轮（Docker 默认）
python -m scheduler --once   # 只跑一轮即退出（本地调试、外部 cron 备用）
```

- 常驻循环用普通 sleep，无需整点对齐
- 每轮 tick 成功结束后 touch `heartbeat_file`，供容器 healthcheck 判活
- 「同一时刻只允许一个实例」由部署保证：compose 单服务单副本，
  代码不实现分布式锁

### uv 与 pyproject

目标 `pyproject.toml`（当前文件中与控制器无关的依赖——claude-agent-sdk、
opentelemetry-api、reportlab 等——在实现阶段移除）：

```toml
[project]
name = "sub2api-account-scheduler"
version = "0.1.0"
description = "7D pacing controller for sub2api accounts"
requires-python = ">=3.13"
dependencies = [
    "requests>=2.32",
    "pyyaml>=6.0",
]

[dependency-groups]
dev = ["pytest>=8"]

[build-system]
requires = ["uv_build>=0.8,<0.9"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "scheduler"
```

本地开发：

```bash
uv sync                                                  # 安装依赖（含 dev）
DB_PATH=./data/scheduler.db uv run python -m scheduler --once   # 本地单轮（直接生效）
uv run pytest                                            # policy 单测
```

联调可用 `tests/mock_server.py` 模拟 sub2api（含名称过滤、冷账号探测、
bulk-update 回显）：

```bash
uv run python tests/mock_server.py 18923 &
SUB2API_BASE_URL=http://127.0.0.1:18923 SUB2API_ADMIN_KEY=test \
  DB_PATH=./data/dev.db HEARTBEAT_FILE=./data/last_tick \
  uv run python -m scheduler --once
```

### Docker 部署

`Dockerfile`（多阶段：构建层用 uv，运行层纯 Python）：

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=builder /app/.venv /app/.venv
COPY config.yaml .
HEALTHCHECK --interval=5m --timeout=10s --start-period=5m CMD \
  python -c "import os,sys,time; p=os.getenv('HEARTBEAT_FILE','/data/last_tick'); sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<7200 else 1)"
ENTRYPOINT ["python", "-m", "scheduler"]
```

healthcheck：心跳文件超过 2 个调度周期（7200s，按 `interval_minutes: 60`
计）未更新即 unhealthy；若调大 interval 需同步调整。

`docker-compose.yml`：

```yaml
services:
  scheduler:
    build: .
    container_name: sub2api-account-scheduler
    restart: unless-stopped
    env_file: .env                           # ADMIN_KEY 等敏感项只进 env
    volumes:
      - ./data:/data                         # SQLite 库 + 心跳文件
      - ./config.yaml:/app/config.yaml:ro    # 策略参数（改后重启生效）
```

部署要点：

- `/data` 必须挂卷，否则重启丢 EWMA / 冷却状态（可自愈但收敛变慢）
- `.dockerignore` 排除 `data/`、`.venv/`，避免本地库文件打进镜像
- 停用：`docker compose down`（已设置的 priority 保持不变）

## 决策日志

每轮对每个账号记录一条决策（日志输出 + `decision_log` 表）：

```text
account_id
current_priority
target_priority
seven_day_used
seven_day_sonnet_used
five_hour_used
catchup_score
recent_hour_burn
safe_hour_cap
usage_source (passive/active/stale)
reason
```

上线初期结合决策日志观察至少 24 小时，确认策略没有造成异常集中，
并据此校准 `target_7d_utilization`。

## 安全要求

- Admin key 只能通过环境变量传入
- Admin key 不写入配置文件、代码、日志
- 日志不打印 access token、refresh token、api key
- 每轮更新前后记录 priority 变化原因
- 更新失败不要高频重试
- active 探测每轮设上限，禁止全量 force
- 同一时刻只允许一个控制器实例执行

## 可观测指标

需要持续观察：

```text
每个账号 7day 使用率（含 sonnet 窗口）
每个账号 5h 使用率
每小时 7day 增量
priority 分布
被动数据新鲜度（passive_usage_sampled_at 距今）
active 探测次数 / 失败数
429 次数
rate_limit_reset_at 数量
overload_until 数量
可调度账号数量
```

理想状态：

```text
账号 7day 使用率逐渐收敛
大部分账号在 reset 前达到 90% - 99%（初期目标）
priority=1010 的账号数量稳定受控
priority=1099 的账号数量不持续扩大
429 没有明显增加
active 探测次数低且稳定
```

异常状态：

```text
单账号一小时 7day 增量过高
大量账号进入 priority=1099
大量账号被动数据长期缺失（说明冷账号探测失效）
429 明显上升
可调度账号数量明显下降
```

## 实施

不设 dry-run，控制器上线即真实更新 priority。上线前确认：

- anthropic：给被管理账号配置 `window_cost_limit` 等请求级硬保护
  （兜底不依赖控制器）
- openai：确认 advanced scheduler 开关状态（默认关闭 = 严格档位语义；
  开启 = 评分制，档位仅为单调偏好且建议手动账号分组隔离）；
  没有请求级 5h 费用保护，必要时调低 `hard_cap_5h_utilization`
- 多平台同时管理：每平台一个控制器实例（独立 config.yaml 与 `DB_PATH`）
- Admin key 仅通过环境变量注入

上线后 24-48 小时结合决策日志与可观测指标重点确认：

- 能正确识别落后、正常、超前账号
- 冷账号能通过 active 探测拿到数据，不被误降档
- `priority=1010` 账号数量稳定受控，没有长期热点账号
- 5h 触顶、429 没有明显增加
- 重启后 EWMA、冷却等状态能从 SQLite 正确恢复，老化清理按期执行

运行稳定后根据实际方差收紧 `target_7d_utilization`（97 → 98 以上），
固化参数并增加告警。

异常处置：`docker compose down` 停止控制器（已设置的 priority 保持不变），
必要时手工 bulk-update 将账号恢复到正常档 1050。

## 总结

这套方案的关键点：

1. **priority 是严格档位信号，不是流量权重**——控制器只做账号分层，
   档内分散交给 sub2api 原生的负载率 + LRU。
2. **priority 只影响新会话**——粘性会话 1h TTL 续期且绕过 priority，
   降档卸不掉存量流量，控制目标和参数都要为这个滞后留余量。
3. **硬保护靠请求级机制**——`window_cost_limit`、RPM、自动限流是实时的；
   小时级控制器只负责节奏，5h 规则仅作"禁止 boost"参考和兜底。
4. **数据获取走被动优先**——一次 list 拿全量被动数据，active 探测
   按需、限量，并优先用于冷账号，避免"缺数据就降档"的死锁。
5. **双平台同构但保护不同**——openai(Codex OAuth) 同样有 5h/7d 窗口与
   被动采样，决策逻辑完全复用；但它没有请求级费用硬保护（控制器 5h 规则
   升级为主保护），且 advanced 评分调度开启时档位退化为单调偏好。
