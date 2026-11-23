# 司序「排不排得开」后端契约（Backend 1.31）

> 适用后端版本：`1.31`  
> `/api/health` 新增能力声明：`capacity_plan_v1`、`agenda_chat_tools_v1`  
> 核心原则：**确定性规则算时间，模型只做自然语言理解与表述**；试算完全只读，不写回 `snapshot`。

---

## 一、`POST /api/v3/capacity/plan`（`capacity_plan_v1`）

用于在指定时间范围的剩余留白（由 `/api/v3/calendar` 同源计算，从 `now` 起算，扣除底色、点墨、约定、手动调整和已有落笔工作块，并按北京时间零点切日）内，一次性试算多项活动排不排得开，并在排不开时给出 2–3 个经模拟校验的取舍方案。本接口**不调用模型、不受公网模型配额门控**。

### 1. 请求字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `snapshot` | object | 是 | 标准 v3 清单（经 `validate_snapshot_v3` 校验，不修改） |
| `now` | string | 否 | RFC3339 带时区时刻；省略时取 `snapshot.reference_time`，不可显式传 `null` |
| `scope` | string \| object | 否 | 与前端 `agenda.js` 对齐：`today` / `tomorrow` / `weekend` / `next_weekend` / `weekday` / `week` / `next`（默认 `week`）；也可传 `weekday:0`..`weekday:6` |
| `weekday_index` (`weekdayIndex`) | int (0–6) | 条件 | `scope="weekday"` 时必填（`0=周一` … `6=周日`） |
| `weekday_next` (`weekdayNext`) | bool | 否 | `scope="weekday"` 时是否强制指下周（默认 `false`；若目标星期早于今天自动顺延到下周） |
| `from` / `to`（或 `range`） | string | 否 | 自定义起止，支持 `YYYY-MM-DD` 或完整 RFC3339 时间戳，最长 31 天；若跨越 `now` 自动从 `now` 起算 |
| `activities` | array | 是 | 1–20 项待试算活动（为空或缺少合法 `minutes` 返回 `400`） |
| `reserve_rest_minutes` | int | 否 | 要预留的休息时间（分钟，`0..10080`，默认 `0`）；排完后 `leftover_minutes < reserve_rest_minutes` 判为排不开 |

**`activities[]` 单项字段**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `title` | string | 是 | 1–120 字活动名 |
| `minutes` | int | 是 | 预计时长（分钟，`1..10080`） |
| `category` | string | 否 | `study` / `club` / `leisure` / `rest` / `other`（默认 `other`） |
| `splittable` | bool | 否 | 是否可拆成多段（默认 `false`） |
| `min_block_minutes` | int | 否 | 可拆时的最短单段时长（默认 `min(30, minutes)`；不可拆时等于 `minutes`） |
| `preferred` | string | 否 | 时段偏好：`weekend` / `evening`（18–24点） / `daytime`（08–18点） / `any`（默认 `any`；兼容 `morning` / `afternoon`） |

### 2. 返回字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `tool` | string | 固定 `"plan_activities"`（与 `/api/v3/chat` 的 `capacity` 卡片结构一致） |
| `now` | string | 向上取整到分钟的计算起点 |
| `scope` | object | `{type, label, from, to}` |
| `free_time_defined` | bool | `snapshot.daily_window` 是否已定义；为 `false` 时优雅返回 `feasible: false` |
| `available_minutes` / `initial_longest_free_minutes` | int | 排入前范围内总剩余留白与最长连续空档（分钟） |
| `requested_minutes` / `placed_minutes` | int | 活动请求总时长 / 实际放入总时长（分钟） |
| `feasible` | bool | 总体是否排得开（所有活动均放入且满足 `reserve_rest_minutes`） |
| `rest_satisfied` / `rest_shortfall_minutes` | bool / int | 预留休息是否满足及差额（分钟） |
| `placements` | array | 按输入顺序返回每项活动的 `status`（`placed`/`partial`/`unplaced`）、`placed_minutes`、`shortfall_minutes`、`matched_preferred`、`blocks[{start, end, minutes, label}]` 与可选 `note` |
| `shortfall` | array | 排不下项的缺口清单 `[{index, title, requested_minutes, placed_minutes, missing_minutes}]`；全排得开时为 `[]` |
| `leftover_minutes` / `longest_free_minutes` | int | 排完后剩余留白总量与最长连续空档（分钟） |
| `free_slots` | array | 排完后剩余可用空档 `[{start, end, minutes, label}]` |
| `tradeoffs` | array | 排不开时按规则生成的 2–3 个取舍方案 `[{id, kind, title, consequence, feasible, leftover_minutes, longest_free_minutes, changes, placements}]` |
| `warnings` | array | 复用 `agenda.js` 同口径提醒（待补时间、未估时任务、课表未确认等） |
| `summary` | string | 一句中文结论，前端可直接展示 |

### 3. 完整路演案例

场景：2026 年 11 月 24 日（周二）`14:15` 路演起点，已铺课表（周一/四高数 08:00–09:50、周二/五英语 10:10–12:00、周三专业课 14:00–15:00、周四社团例会 19:00–20:00，周末作息 09:00–23:00），25 日摄影社招新说明会 `14:30–15:30` 与专业课重叠 30 分钟，提交社团报名表（30 分钟，25 日 18:00 截止）已落笔在周二 `14:15–14:45`。

问：「**这周末出去玩 4 小时、写作业 3 小时、社团 2 小时排得开吗**」（预留 2 小时休息）

请求：

```json
{
  "snapshot": { "...路演案例 v3 快照..." },
  "now": "2026-11-24T14:15:00+08:00",
  "scope": "weekend",
  "activities": [
    {"title": "出去玩", "minutes": 240, "category": "leisure", "splittable": false, "preferred": "daytime"},
    {"title": "写作业", "minutes": 180, "category": "study", "splittable": true, "min_block_minutes": 60, "preferred": "any"},
    {"title": "社团活动", "minutes": 120, "category": "club", "splittable": false, "preferred": "evening"}
  ],
  "reserve_rest_minutes": 120
}
```

返回（节选实测结果）：

```json
{
  "tool": "plan_activities",
  "schema_version": 1,
  "now": "2026-11-24T14:15:00+08:00",
  "scope": {"type": "weekend", "label": "本周末", "from": "2026-11-28T00:00:00+08:00", "to": "2026-11-30T00:00:00+08:00"},
  "free_time_defined": true,
  "available_minutes": 1680,
  "initial_longest_free_minutes": 840,
  "reserve_rest_minutes": 120,
  "requested_minutes": 540,
  "placed_minutes": 540,
  "feasible": true,
  "rest_satisfied": true,
  "rest_shortfall_minutes": 0,
  "total_shortfall_minutes": 0,
  "placements": [
    {"index": 0, "title": "出去玩", "status": "placed", "matched_preferred": true, "blocks": [{"start": "2026-11-28T09:00:00+08:00", "end": "2026-11-28T13:00:00+08:00", "minutes": 240, "label": "11月28日 周六 09:00–13:00"}]},
    {"index": 1, "title": "写作业", "status": "placed", "matched_preferred": true, "blocks": [{"start": "2026-11-28T13:00:00+08:00", "end": "2026-11-28T16:00:00+08:00", "minutes": 180, "label": "11月28日 周六 13:00–16:00"}]},
    {"index": 2, "title": "社团活动", "status": "placed", "matched_preferred": true, "blocks": [{"start": "2026-11-28T18:00:00+08:00", "end": "2026-11-28T20:00:00+08:00", "minutes": 120, "label": "11月28日 周六 18:00–20:00"}]}
  ],
  "shortfall": [],
  "leftover_minutes": 1140,
  "longest_free_minutes": 840,
  "tradeoffs": [],
  "summary": "本周末排得开：3 项活动共需 9 小时（含预留休息 2 小时），全部可放入，排完后还剩 19 小时留白（最长连续 14 小时）。"
}
```

若本周末已有额外约定导致仅剩 7 小时留白（周六 09:00–13:00 与周日 19:00–22:00），同一组 9 小时活动会返回 `feasible: false`、`shortfall: [{"title": "社团活动", "missing_minutes": 120}]`，以及 3 个经重新试算验证的取舍方案：
1. `shift_to_other_slot`：「把「社团活动」改到 11月24日 周二 14:45–16:45」（周末内其余 2 项全部排得开，自动避开了 14:15–14:45 的报名表落笔）。
2. `trim_activity`：「把「出去玩」从 4 小时缩到 2 小时」（其余活动全部排得开）。
3. `trim_activity`：「把「写作业」从 3 小时缩到 1 小时」（其余活动全部排得开）。

### 4. 确定性排布与取舍规则

1. **排布顺序**：按「不可拆优先 → 时长由大到小 → 有时段偏好优先 → 输入原序」排入；不可拆活动先在满足 `preferred` 的子区间内择最早空档，不足时回退到其他可用空档并标 `matched_preferred: false`；若最早优先排不下，自动再试一次紧致匹配（tightest-fit）取缺口更小者。可拆活动分配时避免留下小于 `min_block_minutes` 的不可用尾段。
2. **取舍生成（排不开时）**：按以下四类规则构造候选并**全部重新调用同一排程器验证**（绝不编造后果数字）：
   - `split_activity`：将不可拆长活动（优先学习类/排不下项）改为可拆，解决“总量够但连续空档不够”。
   - `trim_activity`：按 `leisure → other → club → rest → study` 弹性顺序缩短单项时长以补足缺口。
   - `shift_to_other_slot` / `defer_activity`：优先在本周其余留白中为一项活动找到具体可落地空档（如周内空档），否则建议先移出当前范围。
   - `reduce_rest`：当活动本身放得下、仅不满足 `reserve_rest_minutes` 时，给出下调预留休息的方案。
3. **排序筛选**：优先保留 `feasible: true`、缺口更小、压缩时长更少且类型不重复的 2–3 个方案。

---

## 二、`POST /api/v3/chat` 工具扩展（`agenda_chat_tools_v1`）

### 1. 请求与兼容

- 请求字段：`mode: "model"`、`messages`（1–9 条交替对话，末条为 `user`）、可选 `snapshot`（v3 快照）、可选 `now`（RFC3339 时刻）。
- **向后兼容**：未传 `snapshot` 时与 `agenda_chat_v1` 完全一致，仅返回 `{"reply": "..."}`。
- **隐私与确定性**：传入 `snapshot` 时，原始清单**不发给模型**；后端向模型提供两个只读工具：
  1. `check_capacity(scope)`：计算范围内剩余留白、最长连续空档与可用空档列表（返回 `tool: "check_capacity"`）。
  2. `plan_activities(scope, activities, reserve_rest_minutes)`：直接调用 `plan_capacity`，与 `POST /api/v3/capacity/plan` 数值完全一致（返回 `tool: "plan_activities"`）。
- 口语换算引导：「半天 / 一上午 / 一下午」= 240 分钟，「一晚上 / 半晚」= 180 分钟；若用户未给活动时长，模型调用 `check_capacity` 报出真实空档并只追问一句（例如「出去玩大概几个小时？」）。

### 2. 返回结构

```json
{
  "reply": "本周末排得开：出去玩（4 小时）可安排在 11月28日 周六 09:00–13:00，写作业（3 小时）安排在 13:00–16:00，排完后还剩 21 小时留白（最长连续 14 小时）。",
  "capacity": {
    "tool": "plan_activities",
    "scope": { "type": "weekend", "label": "本周末", "from": "2026-11-28T00:00:00+08:00", "to": "2026-11-30T00:00:00+08:00" },
    "feasible": true,
    "available_minutes": 1680,
    "leftover_minutes": 1260,
    "longest_free_minutes": 840,
    "placements": [ "..." ],
    "shortfall": [],
    "tradeoffs": [],
    "summary": "..."
  }
}
```

普通闲聊未触发工具时，响应仍只返回 `{"reply": "..."}`（不含 `capacity` 字段）。
