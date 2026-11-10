# 四层时间与日历接口（快照 v3）

2026年11月6日按用户要求设计。目标：少问、能导入就直接用；缺的信息以“未定义”存在，不挡住其他事项；在日历上分四层显示；只在例行、工作、临时三层之间判断冲突。v2 接口（`/api/v2/*`）保持不变，旧页面继续可用，直到前端改用 v3。

## 界面名称（书画，2026年11月6日用户确定）

| 层 | 界面名 | 说明 |
| --- | --- | --- |
| 例行占用 `routine` | 底色 | 每周照例的课与例会 |
| 自由时间 `free` | 留白 | 自己能支配的时间 |
| 工作时间 `work` + 约定 `appointment` | 落笔 | 在留白里落下约定与截止之事：为截止之事划出的工作时间，以及自己约好的事（如周末和朋友出去） |
| 临时占用 `temporary` | 点墨 | 临时、突发事项：讲座、考试、面试、突发事务 |
| 冲突 | 叠墨 | 两件事落在同一处 |
| 未定义 | 待书 | 还没写清楚的信息 |

接口字段名保持英文不变；具体原因与提示仍用平实中文。

## 四层时间

| 层 | 名称 | 含义 | 来源 | 日历表现建议 |
| --- | --- | --- | --- | --- |
| ① | 例行占用 `routine` | 每周固定占用：课表、每周例会、固定出行 | 用户写入或导入；可单双周、学期起止、跳过某天 | 实色块 |
| ② | 自由时间 `free` | 自己能支配的时间 | 自动推算：作息范围 − 例行 − 临时；可手动加减某一段 | 浅底色 |
| ③ | 工作时间 `work` | 从自由时间里划给有截止的工作的时段 | 程序按截止和预计用时生成，并独立校验 | 描边块，连到截止标记 |
| ④ | 临时占用 `temporary` | 一次性占用：讲座、考试、面试、突发事务 | 通知导入（有起止直接采用）或用户写入 | 斜纹或另一色块 |

截止时刻本身不是一段时间，以标记 `deadline` 显示在日历上。

## 快照 v3

```json
{
  "schema_version": 3,
  "revision": 0,
  "timezone": "Asia/Shanghai",
  "reference_time": "2026-11-06T09:00:00+08:00",
  "sources": [{"id": "s1", "kind": "pasted_notice", "text": "……", "published_at": null}],
  "daily_window": [{"start": "08:00", "end": "23:00"}, …共7项，周一到周日；某天为 null 表示当天不安排…],
  "routines_confirmed": true,
  "routines": [{"id": "r1", "title": "高等数学", "weekdays": [1, 3], "start": "08:00", "end": "09:40",
                "from": "2026-09-01", "until": "2027-01-10", "week_interval": 1, "skip_dates": [],
                "location": "主楼301", "source_id": null}],
  "free_adjustments": [{"id": "f1", "kind": "remove", "start_at": "…", "end_at": "…", "note": "回家"}],
  "events": [{"id": "e1", "title": "参加讲座", "start_at": "…", "end_at": "…", "location": "主楼405",
              "status": "active", "source_id": "s1", "evidence": [], "notes": []}],
  "tasks": [{"id": "t1", "title": "提交课程报告", "deadline_at": "…", "estimate_minutes": null,
             "splittable": true, "min_block_minutes": null, "earliest_start_at": null, "priority": null,
             "status": "active", "location": null, "materials": [], "source_id": "s1", "evidence": [], "notes": []}]
}
```

- `daily_window`：`null` 表示作息范围未定义，此时自由时间整体“未定义”，不生成工作时间。时刻为 `HH:MM`，结束可写 `24:00`，必须晚于开始。
- `routines_confirmed`：`false` 表示还没填课表等例行占用（也没确认“没有”）。此时仍会推算自由时间，但标注“未扣除例行占用，可能偏多”。
- `routines`：`weekdays` 为 1—7（周一到周日）；`week_interval` 为 1（每周）或 2（单双周，以 `from` 所在周为第1周）；`until` 可为 null；`skip_dates` 用于放假、调课。
- `events`：可选 `kind`：`temporary`（默认，点墨）或 `appointment`（约定，属于落笔，后端1.17起）。约定同样占用时间，与例行、临时、其他约定重叠都算冲突。`start_at`、`end_at` 都可为 null。有起止才占用时间；只有开始显示为“结束未定义”的标记；都没有是“时间待定”。`status` 为 `active` 或 `cancelled`。
- `tasks`：`estimate_minutes` 就是原来的“还需要多少分钟”，改名“预计用时”且可选。`priority`（0—100）可选，空时按 50 计并在结果中说明。`splittable` 默认 `true`；可拆时 `min_block_minutes` 为空则按 30 分钟（预计用时更短时按预计用时）计。`status` 为 `active`、`completed`、`cancelled`。
- 不再有 draft/confirmed 状态和逐项确认：有原文依据的项目直接采用，`evidence` 标明来源，可随时修改或删除。必做/可放弃的取舍改为：放弃就把状态设为 `cancelled`。
- 本版不含前置依赖与办理窗口内择时；v2 的这两项能力不带入 v3。

## 接口

### `POST /api/v3/calendar`

请求 `{"snapshot": {…v3…}, "range": {"from": "2026-11-09", "to": "2026-11-15"}}`。`range` 可省略，默认参照时刻所在的周一到周日；最长31天。只读，不调用模型，不保存清单。

响应：

```json
{
  "schema_version": 3, "revision": 0, "status": "clear | conflicts | incomplete",
  "range": {"from": "…", "to": "…"},
  "definition": {"free_time": "defined | undefined", "routines": "confirmed | unconfirmed"},
  "layers": {
    "routine":   [{"id": "r1@2026-11-09", "routine_id": "r1", "title": "…", "start": "…", "end": "…", "location": "…"}],
    "free":      [{"start": "…", "end": "…", "origin": "daily_window | added"}],
    "work":      [{"task_id": "t1", "title": "…", "start": "…", "end": "…", "deadline_at": "…"}],
    "appointment": [{"id": "a1", "title": "…", "start": "…", "end": "…", "location": "…"}],
    "temporary": [{"id": "e1", "title": "…", "start": "…", "end": "…", "location": "…"}]
  },
  "markers": [{"type": "deadline | event_start_only", "id": "t1", "title": "…", "at": "…", "state": "…"}],
  "tasks": [{"id": "t1", "state": "scheduled | estimate_undefined | deadline_undefined | free_time_undefined | short | overdue | beyond_horizon | not_attempted | completed | cancelled",
             "estimate_minutes": 120, "scheduled_minutes": 120, "assumptions": ["优先级未填，按50计。"]}],
  "conflicts": [{"type": "…", "layers": ["routine", "temporary"], "items": […], "start": "…", "end": "…", "minutes": 40, "reason": "…"}],
  "undefined": [{"type": "…", "id": "…", "title": "…", "reason": "…"}],
  "validation": {"passed": true, "errors": []},
  "warnings": [], "limitations": []
}
```

`status`：有冲突为 `conflicts`；无冲突但有未定义项为 `incomplete`；都没有为 `clear`。

冲突类型（只在 ①③④ 之间判断）：

| type | 含义 |
| --- | --- |
| `routine_overlap` | 两个例行占用重叠（例如课表冲突） |
| `temporary_routine_overlap` | 临时占用与例行占用重叠（例如讲座撞课） |
| `temporary_overlap` | 两个临时占用重叠 |
| `appointment_routine_overlap` | 约定与例行占用重叠（1.17） |
| `appointment_overlap` | 约定与临时占用或另一个约定重叠（1.17） |
| `work_shortage` | 截止前的自由时间不够完成预计用时，给出还差多少分钟 |
| `overdue` | 截止已过但工作仍是 active |

每个临时占用都在它自己的日期上检查与例行占用、与其他临时占用的冲突，即使不在当前显示的这一周（后端1.14起）；例行占用之间的重叠每周重复，只在显示范围内列出。自由时间与其他层不算冲突：它本来就是扣除①④后的剩余。工作时间只放在自由时间里，并用 v2 的独立校验器核对（不重叠、不越过截止、用时准确、连续块）。能排的先排，排不下的单独报 `work_shortage`，不会因为一件排不下就整体放弃。

未定义类型：`free_time_undefined`、`routines_unconfirmed`、`estimate_undefined`、`deadline_undefined`、`event_end_undefined`、`event_time_pending`、`beyond_horizon`（截止超出31天排程范围）、`task_limit`（一次超过40件工作或自由时间超过200段，较晚截止的暂不安排，状态为 `not_attempted`）。

规则提取的标题会去掉时间与“请于……前”等套话（如“提交课程报告”），“……举行”改写为“参加……”；同一句中识别出的地点填入临时占用。

### `POST /api/v3/extract`

请求 `{"text": "…", "reference_date": "2026-11-06", "mode": "rules | model"}`。返回可以直接放进 v3 快照的条目：

```json
{"source": {"id": "…", "kind": "pasted_notice", "text": "…", "published_at": null},
 "events": [{…v3 event…}], "tasks": [{…v3 task…}], "warnings": ["…"], "mode": "rules"}
```

- 有起止时间的活动 → `events`，直接采用；只有开始 → `events` 且 `end_at` 为 null。
- 有截止的工作 → `tasks`，`estimate_minutes` 为 null（预计用时未定义）。
- 办理窗口 → `tasks`，`earliest_start_at` 为窗口开始、`deadline_at` 为窗口结束。
- 没有时间 → 活动类为“时间待定”的 event，其余为截止未定义的 task。
- 每项的 `evidence` 保留原文片段，`notes` 保留提取提醒。`mode=model` 走现有 Dify 本机模型入口，同样只允许本机访问。

### `POST /api/v3/timetable`（课表识别，后端1.13）

把教务系统复制出的课表文字（或截图本地识别后的文字）变成例行占用。大模型（DeepSeek，关闭思考、JSON输出）只负责把文字整理成课程；日期和时刻由程序按下面两项换算，模型不猜：

- `term_start`：第1周周一的日期，如 `"2026-09-07"`。
- `periods`：节次时间表，如 `{"1": {"start": "08:00", "end": "08:45"}, "2": {"start": "08:55", "end": "09:40"}}`。原文直接写了时刻（如 14:00-15:40）的课不需要它。

请求两种模式：

- `{"mode": "model", "text": "…", "term_start": "…", "periods": {…}}`：调用一次模型。只允许本机访问，受请求额度限制；没有配置 DeepSeek Key 时返回 503 `model_not_configured`。
- `{"mode": "convert", "text": "…", "courses": […], "term_start": "…", "periods": {…}}`：不调用模型，只把上一次返回的 `courses` 重新换算。用户补填第1周日期或节次时间后用它，`text` 须与原来一致。

响应：

```json
{"source": {"id": "s-…", "kind": "timetable", "text": "…", "published_at": null},
 "courses": [{"title": "高等数学", "quote": "原文片段", "weekdays": [1], "start_period": 1, "end_period": 2,
              "start_time": null, "end_time": null, "weeks": [[1, 16]], "parity": "all | odd | even", "location": "主楼301"}],
 "routines": [{…可直接放进快照的 v3 routine…}],
 "drafts": [{"course": {…}, "problem": "第1-2节的上课时刻未定义，请补全节次时间表。"}],
 "warnings": ["「某课」未采用：原文依据不在课表文字中。"], "mode": "model"}
```

- 每门课都要有逐字出现在原文里的 `quote`，否则不采用并写进 `warnings`。
- 周次“1-4,6-16周”拆成两条 routine；单双周用 `week_interval: 2`，从对应的第一周开始；原文没写周次时从第1周开始、不设结束。
- 前端应先让用户核对 `courses` 和 `routines`（课表是整学期的数据，错一条影响每周），确认后把 `source` 和 `routines` 加进快照，并把 `routines_confirmed` 设为 `true`。
- 健康接口新增 `timetable_model: {"configured": true|false}`，能力列表新增 `timetable_v3`。
- **截图模式（后端1.15，能力 `timetable_image_v3`）**：`{"mode": "image", "image": "data:image/jpeg;base64,…", "term_start": "…", "periods": {…}}`。DeepSeek（`deepseek-flash`，图片放在用户消息中）按格子的列和行读出星期、节次和格子文字，并抄下截图侧边的节次时间。程序把每格写成“周一 第3-4节 格子原文”的转写作为 `source.text`，再照常换算；响应多一个 `periods_seen`。填了 `periods` 时以填写的为准；没填时用截图上读到的并在提醒中说明。截图只发送给模型、不保存；只允许本机访问，请求体上限15MB。

## 可以交给大模型的其他环节（尚未实现）

冲突、容量和工作时间必须由程序精确计算；大模型只适合“给建议，用户采纳后才写入”：

1. **预计用时建议**：按事项名称和原文给出建议用时与理由，用户一键采纳或修改。
2. **一句话添加**：“周三下午三点到五点面试”转成一条临时占用。

DeepSeek API Key 与 Dify Key 一样，只在启动本地服务时由用户隐藏输入，服务停止即清除，不写入文件、前端或聊天。

## 叠墨对策（1.18）

每次计算日历时，每处叠墨都会带上 `id`、一句规则说明 `advice` 和若干 `strategies`，不调用模型。每条对策有 `action`、`label`、`detail`、`params`、`recommended`，页面可一键采用：

| 叠墨 | 规则 | 推荐 |
|---|---|---|
| 约定压在底色上 / 约定与他事相叠 | 约定是自己定的，最容易挪；在附近 3 天的留白里找同样长的空档（避开已落笔的工作） | 改约到最近的空档 |
| 点墨压在底色上 | 点墨临时突发、底色每周有 | 这次缺课（在底色里记一次例外，提醒请假） |
| 两处点墨相叠 | 只能二选一 | 不推荐，由用户选 |
| 底色相叠 | 多半是课表录入有误 | 去铺纸核对课表 |
| 留白不够落笔 | 不能拆→允许拆开；空档太碎→每段最短 15 分钟；总量不够→在截止前某天作息首尾加一段留白，或压缩用时 | 依次取第一条适用的 |
| 已过截止 | 做完、取消或改截止 | 标记完成 |

`action` 取值：`skip_routine_date`、`cancel_event`、`move_event`、`update_task`、`add_free`、`review`（只跳转页面）。

## 估计结束时间（1.19）

很多通知只写开始时间。`POST /api/v3/estimate-end`，请求 `{"mode": "rules"|"model", "items": [{"id", "title", "start_at", "context"}]}`（最多30项），返回 `{"estimates": [{"id", "minutes", "end_at", "basis", "reason"}]}`。

- `rules`（默认）：按活动类型取常见时长：面试30分钟、会议60分钟、讲座90分钟、考试/答辩/培训120分钟、晚会150分钟、比赛180分钟，看不出类型按60分钟。
- `model`：DeepSeek 读通知原文（`context`），原文写了结束或时长就照原文算，否则按常见时长估计；时长限制在15—720分钟。仅限本机访问，计入模型请求额度。

结果只是建议：研墨页导入后，对只有开始时间的点墨问一句“大约几点结束？”，用户改好再保存；不保存就仍是待书。

底色（例行占用）不是必填项：没有底色时不再列为待书，叠墨照常判断。

## 叠墨对策的权衡与请假说明（1.21）

`POST /api/v3/advise`，请求 `{"mode": "rules"|"model", "type", "items": [{"title", "layer", "start", "end", "location"}], "strategies": [{"action", "label", "detail", "recommended"}], "advice", "context"}`，返回 `{"pick", "reason", "note", "basis"}`。

- 对策仍只由规则（strategies.py）生成，每条都能在页面上一键采用和撤销。
- `rules` 取规则推荐的那条；`model` 让 DeepSeek 按两件事本身权衡，只能回答对策编号，越界、非整数或没有理由都视为失败，本次不采用。
- 所选对策是“这次不上某门课”时，`note` 是给任课老师的请假说明草稿（模型可改写，缺省用模板），姓名、班级留作【】；司序不会替用户发送。
- `model` 模式与其他模型功能共用访问口令和每日上限。

