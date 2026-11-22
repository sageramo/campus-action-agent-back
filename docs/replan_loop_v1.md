# 状态变化 / 打断重排闭环接口契约（Backend `1.29` · `replan_loop_v1`）

本契约基于 Claude 线上整合基线 `frontend/roadshow` (`32619f0`，含后端 `1.28` + 网页 `2026.11.21.50`) 构建，面向 **Claude（前端接入）** 与 **GPT/Codex（独立验收）**。所有重排与方案对比核心均由确定性求解器离线完成（复用 `/api/v3/solve`、`/api/v3/calendar` 与 `calibration.py`，零新增 Python 依赖）；DeepSeek 仅作为可选辅助，用于自然语言状态变更草稿提取或 5 分钟起步动作生成，且遵循既有公网口令与配额门控。

- **后端版本**：`1.29`（`/api/health` 新增能力声明 `focus_cognitive_scheduling_v1`、`replan_loop_v1`）
- **三组固定机器可读样例**：[`examples/replan-fixtures.v1.json`](../examples/replan-fixtures.v1.json)（已由 `tests/test_replan_loop.py` 通过真 HTTP 往返逐字回归核验，并验证确认后 `/api/v3/calendar` 逐段等于预览 `after`）

---

## 一、数据归属、硬约束与取舍边界

### 1. 数据归属（v3 快照契约不破坏）
- **`focus_windows`（专注时段）与 `task_profiles`（任务认知负荷）不在 v3 snapshot 中持久化**：由前端本地保存在浏览器（`localStorage`），每次调用 `/api/v3/solve` 或 `/api/v3/replan/preview` 时按需传入。若后续需要跨设备写入快照，将升级快照契约版本（v4）并提前与前端对齐。
- **`disruptions`（临时打断）与 `rest_blocks`（用户锁定的临时休息）**：确认后写入 v3 snapshot——打断事件写入 `events[]`，锁定休息写入 `free_adjustments[]`（`kind: "remove"`, `note: "锁定休息：..."`）；所选方案中各任务的排程落点通过写入 `task.earliest_start_at` 固化，确保画卷 `/api/v3/calendar` 重算结果与所选方案逐段一致。

### 2. 硬约束 vs 软目标（重要口径约定）
- **硬约束（任何方案绝不违背）**：任务截止时间 (`deadline_at`)、已确认课表 (`routines`) 与约定/临时事件 (`events`)、前置依赖 (`dependencies`)、跨楼通勤缓冲 (`transit_minutes`)、以及**用户显式锁定的临时休息区间 (`rest_blocks` 且 `locked: true`)**。
- **推荐排序规则（“先保底线，再少改，最后看专注”）**：
  1. **先过底线 (`metrics.passes_baseline`)**：最紧任务的 `deadline_buffer_minutes >= 底线`（存在有效 `calibration_history` 时，底线为 `(中位膨胀系数 - 1) × 任务 estimate_minutes`；无历史样本时默认 `30 分钟`），且 `cognitive_load="high"` 的任务不得落入 `level="low"` 专注窗口；
  2. **再少改**：所有过底线方案中，优先推荐 `moved_minutes` 最小的；
  3. **最后看专注**：`moved_minutes` 并列时，推荐 `focus_score` 更高的；
  4. **兜底**：若无方案过底线，则推荐 `min_deadline_buffer_minutes` 最大的方案，并在 `decision_reason` 中明确写明「余量不足底线」。
  5. 每个备选方案均携带 **`tradeoff_vs_recommended`**（相对推荐方案的代价与换取收益，推荐方案本身为 `null`，例如 `"多改 450 分钟，换来「整理社团招新素材」排进晚间轻度整理。"`），前端可直接渲染在卡片上。

---

## 二、接口一览

### 1. `POST /api/v3/solve`（向后兼容扩展）
原有请求体完全不变；新增全部可选字段：`focus_windows`、`task_profiles`、`rest_blocks`、`baseline_starts`、`objective`（`"default" | "focus_first" | "buffer_first" | "minimal_change"`）。响应新增 `focus_summary`、`missing_windows`、`algorithm.focus_window_count`、`algorithm.locked_rest_count`。

### 2. `POST /api/v3/replan/parse-state`（可选：一句话转待确认状态变更草稿）
- **请求**：`{ snapshot?, text, now?, mode?: "rules"|"model" }`（默认 `mode="rules"` 零模型离线；`mode="model"` 可选调用 DeepSeek）。
- **响应（200）**：`{ schema_version: 1, mode_used, needs_confirmation: true, parsed_changes: { disruptions, rest_blocks, focus_windows, task_updates }, summary, warnings }`。严格只读，绝不直接修改快照。

### 3. `POST /api/v3/replan/preview`（核心：状态变更 → 确定性多方案重排预览）
- **请求**：`{ snapshot, range?, now?, disruptions?, rest_blocks?, focus_windows?, task_profiles?, task_updates?, dependencies?, transit_minutes?, calibration_history?, apply_calibration?, include_starter?, starter_mode? }`。
- **顶层决策门控字段（后端统一定，前端不另写一套）**：
  - `needs_decision: boolean`：当没有任何打断/锁定休息/任务更新/校准变化，或推荐方案已过底线且 `moved_tasks === 0`（本次休息/打断未挤动任何已排任务，即使另有专注匹配备选方案）时，一律返回 `false`（前端不弹取舍卡）；仅在存在任务被挤动移动、无方案过底线或截止前无解时返回 `true`。
  - `decision_reason: string`：中文原因说明。
  - `focus_hint: string | null`：无任务被挤动但存在更贴合专注时段的备选微调空间时，在此字段返回温和轻提示（不弹取舍卡）；其余情况为 `null`。
- **可行响应（200，`status: "ready"`）**：
  - `feasible: true`, `confirmable: true`, `needs_decision`, `decision_reason`, `focus_hint`, `base_revision`, `snapshot_fingerprint`, `recommended_plan_id`, `candidate_plans[]`, `omitted_plans[]`, `missing_windows: []`, `calibration_notes`, `five_minute_starter`。
  - 每个 `candidate_plans[i]` 包含：
    - `plan_id`、`name`（若某保留方案的 `moved_minutes === 0` 且 `moved_tasks === 0`，其 `name` 统一为 `"保持原排法"`）、`objective`、`summary`、`calendar_verified: true`
    - `metrics`: `{ scheduled_tasks, moved_tasks, moved_minutes, focus_score, min_deadline_buffer_minutes, total_deadline_buffer_minutes, locked_rest_minutes, transit_warnings }`
    - `task_changes[]`: 每项任务含 `before`、`after`、`changed`、`moved_minutes`、`move_reason`、`deadline_at`、`deadline_buffer_minutes`、`focus_match`
    - `expected_schedule`、`preview_token`、`preview_snapshot`（已落实 `earliest_start_at` 且在预览阶段即经 `/api/v3/calendar` 验证逐段一致）
- **截止前无解响应（200，`status: "infeasible"`）**：
  - `feasible: false`, `confirmable: false`, `needs_decision: true`, `candidate_plans: []`, `missing_windows[]`（含 `task_id`, `title`, `window_start`, `deadline_at`, `required_minutes`, `available_minutes`, `longest_contiguous_minutes`, `shortfall_minutes`, `blocking_items[]`, `summary`）。

### 4. `POST /api/v3/replan/confirm`（核心：版本核对 + 画卷逐段核对 + 原子确认）
- **请求**：
  ```json
  {
    "snapshot": { "...客户端当前最新 v3 snapshot..." },
    "range": { "from": "2026-11-23", "to": "2026-11-29" },
    "now": "可选真实当前时刻 ISO8601",
    "base_revision": 4,
    "snapshot_fingerprint": "sha256:...",
    "plan_id": "focus_matched",
    "preview_token": "rpv1_...",
    "preview_snapshot": { "...所选方案的 preview_snapshot..." },
    "expected_schedule": [{ "task_id": "...", "start": "ISO", "end": "ISO" }]
  }
  ```
- **成功响应（200）**：`confirmed: true`, `previous_revision`, `new_revision`, `snapshot`, `calendar_validation`, `calendar_match: { matched: true, verified_tasks, mismatches: [] }`, `summary`。
- **错误响应（HTTP `409 Conflict`）**：
  - `code: "stale_preview"`：清单在预览后发生过版本或内容变更（含 `expected_revision`, `current_revision`, `requires_repreview: true`）；
  - `code: "preview_tampered"`：传入的 `preview_snapshot` 或 `expected_schedule` 与 `preview_token` 不匹配（预览内容被篡改或损坏），拒绝确认；
  - `code: "schedule_mismatch"`：写回快照经 `/api/v3/calendar` 重算与 `expected_schedule` 存在偏差（含 `mismatches[]` 逐任务说明预期与实际差异），拒绝确认。

### 5. `POST /api/v3/starter`（五分钟起步动作）
- 复用 `calibration.py` / `estimate-memory.js` 的 7 类 `KINDS`（展示 / 填报 / 写作 / 复习 / 作品 / 阅读 / 作业）输出具体起步动作；
- 响应包含 `resting: boolean`、`resting_label`、`resting_until`：当 `now` 处于用户锁定的休息区间内时，`resting: true` 且措辞自动切为“想歇就歇”，方便前端温和呈现。
- 后端 `1.30` 新增能力声明 `starter_material_v1`。请求可选传入 `material_text`（最多 6000 字纯文本）。有材料时响应追加 `material_brief.excerpts`，其中最多三条，每条最多 160 字，逐字取自材料，按原文顺序呈现。规则模式优先选择含任务要求的片段，再据任务类别给出第一步。未传材料沿用原响应。
- 网页「笔墨 → 落笔 → 五分钟起步」在用户点击生成时发送材料，使用规则模式。材料、摘录和草稿在当前任务卡里临时展示，清单和日历排程不写入任何新内容。调用方若显式选 `mode=model`，模型只收到选出的摘录，调用沿用已有口令与配额门控。

---

## 三、三组固定验收样例摘要（完整 JSON 见 `examples/replan-fixtures.v1.json`）

1. **`success_replan`（成功重排 + 画卷逐段一致）**：
   - 基线：`revision = 4`，周三 `08:00–09:50` 高数课；`t-math-hw`（完成高数证明题，90m，不可拆，`high`）原排 `09:50–11:20`；`t-slides`（整理社团招新素材，60m，`low`）原排 `11:20–12:20`。
   - 状态变化：临时打断 `临时项目复盘会 10:00–11:30` + 锁定休息 `午休恢复 12:30–14:00` + 专注时段 `14:00–16:00（高）` 与 `19:00–21:00（低）`。
   - 预览输出：`needs_decision: true`、`recommended_plan_id: "buffer_first"`（两套方案均过底线，按“先保底线、再少改、最后看专注”推荐总移动更少的 `buffer_first`：`moved_minutes = 260`、`focus_score = 330`、`min_buffer = 330`；备选 `focus_matched`：`moved_minutes = 710`、`focus_score = 390`、`min_buffer = 120`，`tradeoff_vs_recommended` 为 `"多改 450 分钟，换来「整理社团招新素材」排进晚间轻度整理。"`）；`omitted_plans` 明确说明 `minimal_change` 与 `buffer_first` 完全一致已去重；`five_minute_starter` 识别最早任务为 `kind: "作品"`, `resting: false`。
   - 确认推荐方案 `buffer_first`（并在回归测试中同时确认备选 `focus_matched`）后均返回 `HTTP 200`、`new_revision = 5`、`calendar_match.matched = true`，用 `/api/v3/calendar` 重算确认后快照，工作块与所选方案逐段完全一致、叠墨为 `0`。
2. **`infeasible_before_deadline`（截止前无解）**：
   - 专业课 + 临时辅导员谈话 + 锁定喘息缓冲占去 3 小时，18:00 截止前仅剩 60 分钟而任务需 120 分钟 → `status: "infeasible"`、`needs_decision: true`、`missing_windows[0]` 明确给出缺口 `60 分钟` 与阻塞项。
3. **`stale_snapshot_conflict`（预览后清单已变）**：
   - 预览基于 `revision = 4`，确认时传入 `revision = 5` 的最新快照 → 返回 `HTTP 409`、`code: "stale_preview"`、`expected_revision = 4`、`current_revision = 5`、`requires_repreview = true`。
