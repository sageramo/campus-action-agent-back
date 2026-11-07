# 司序·知所先后：后端

由第7组课程作品「口述行动链」升级。本地已实现时间候选、分钟容量与v2确认快照计算，尚未发布；Dify原工作流仍独立运行。

## 当前入口

`POST /api/parse`：通知及可选参照日，返回保留原文、结束值、精度与疑问的规则候选。`POST /api/conflicts`：阶段2兼容接口。`POST /api/v2/calculate`：直接接收v2快照，强校验来源、确认字段、区间和任务状态，返回带输入revision的只读结果。`POST /api/v2/extract`：独立Dify Workflow的本地代理，默认关闭，启用后只提交本次通知，严格检查提取结构与原文引文；错误不重试、不回退。`POST /api/v2/plan`：确认快照的有限排程与独立校验（40件/31天），只读且不调用模型，详见docs/planner_v1.md。`GET /api/health`的代码version为1.15，新增timetable_image_v3：`POST /api/v3/timetable`的`mode: image`直接读取格子式课表截图（base64 data URL，最大10MB），返回逐格转写与截图上读到的节次时间`periods_seen`。1.14（临时占用的冲突不再只查当前显示的一周；时间待定兜底词表加入选拔赛、初赛、复赛、决赛、晚会、演出、答辩）。1.13新增timetable_v3：`POST /api/v3/timetable`用DeepSeek把课表文字整理成课程，再由程序按第1周日期和节次时间表换算成例行占用（Key在启动时隐藏输入，可跳过）。1.12新增time_layers_v3：`POST /api/v3/calendar`按例行占用、自由时间、工作时间、临时占用四层生成日历并判断冲突，`POST /api/v3/extract`把通知直接转成可用的临时占用与工作，详见docs/time_layers_v3.md；v2接口保持不变。1.11仅改容量检查未覆盖约束的说明文字，1.10新增attendance_requirement_folding_v1；1.9的pending_event_supplement_v1、1.8的date_reminder_folding_v1、1.7的reminder_consolidation_v1、1.6的source_condition_guards_v1及既有notice_candidates、snapshot_v2、model_notice_candidates_v1、partial_fixed_start_v1和bounded_planning_v1继续保留；model_extraction仅返回enabled/configured布尔值，配置合规不等于实测连通。2026年10月31日已确认用户8765实际为1.6，角色通知实际复核通过；条件通知两次超时，仍待Dify运行记录定位。

确认与重算契约见 `docs/task_contract_v2.md`，旧接口口径见 `docs/time_capacity_v1_1.md`。`calculate`仅检查固定重叠与累计时长；`plan`可生成并独立校验支持范围内的具体安排，支持连续块与已确认依赖，不自动选择办理窗口。1.6补充指定角色、个人待办理范围及条件前提的原文提醒，不自动认定这些条件成立。1.7在模型已说明缺少时间时不再重复通用时间提醒，并为出自同一句原文的候选互相标注可能的总述/分项重叠，不自动合并或删除。1.8合并同义的年份与仅日期提醒：采用了推定年份时以“暂按某年解释”取代模型的同义缺年份提醒，未采用任何时间时不再显示推定年份，仅日期跨度只保留一条说明。1.9在原文写明讲座、会议、考试等活动“时间待定/另行通知”而模型没有任何候选引用该句时，补出一项无时间的“参加X（时间待定）”草稿并标明由程序补出；领取、补拍等另行通知不补。1.10把模型单列、无时间无地点、只讲签到签退入场着装等到场要求的固定事项，并入通知中唯一带开始时间的固定活动作为提醒；主活动不唯一时不并。

## 本地运行与验证

在本目录运行 `../.venv/bin/python -m campus_agent.server`，默认 `http://127.0.0.1:8765`；前端另在同级campus-action-agent使用HTTP服务，当前预览8873。生产依赖为requirements.txt，v2校验所需jsonschema及rfc3339-validator已转入生产依赖；完整开发版本见requirements-dev.lock.txt。

```bash
../.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
../.venv/bin/python scripts/check_regressions.py --output ../reports/phase4-regression-results.json
../.venv/bin/python scripts/check_dify_workflow.py
../.venv/bin/python scripts/check_model_pipeline.py --node /path/to/node
```

当前回归默认phase3-cases.json，保留原十四项断言并增加实际v2与Node状态门控检查。Node须在PATH或以--node明确路径；缺Node不会跳过检查而伪报通过。历史fixture可显式--cases tests/fixtures/phase1-cases.json回放，仍记录原三个未接入项；不要覆盖phase1/phase2留档。

`deploy/`为历史静态副本，正式本地工作台在同级前端仓库；发布前需明确边界，不能把历史页当作新工作台。根目录Git是本地来源快照，无远程推送。

模型配置名见env.example，程序不会自动加载.env。本轮仅允许实际loopback客户端，有Origin时还需命中精确允许列表；服务端每进程并发1、每分钟6次、每小时60次，重启重置，不代表金额预算或公网鉴权。不要把模型端点通过本机反向代理无鉴权转发到公网。导入与隐藏密钥启动方法见父目录reports/阶段4-Dify导入与接入操作.md；密钥不放前端、不发聊天、不存仓库。真实Dify导入与模型联调仍需账户操作，合成预期样例仅用于软件链路验证。

## 线上开放模型功能（1.20）

默认只有本机工作台能调用模型。要让 GitHub Pages 上的页面也能用模型提取、课表识别和结束时间估计，在 Render 服务的 Environment 里填写（不要写进代码或前端）：

| 变量 | 说明 |
|---|---|
| `SIXU_MODEL_ACCESS_CODE` | 访问口令，至少6位；只告诉评委和同学。页面第一次用模型时会要求输入，并保存在该浏览器 |
| `DEEPSEEK_API_KEY` | DeepSeek 的 Key（课表识别、结束时间估计） |
| `SIXU_MODEL_EXTRACTION_ENABLED` | 设为 `true` 才启用 Dify 模型提取 |
| `DIFY_API_KEY` | Dify 应用的 API Key；`DIFY_API_BASE` 默认 `https://api.dify.ai/v1` |
| `SIXU_MODEL_DAILY_LIMIT` / `SIXU_MODEL_IP_DAILY_LIMIT` | 每天全站 / 每个IP 的模型调用上限，默认 200 / 30 |
| `SIXU_PUBLIC_ORIGINS` | 允许调用的页面地址，默认 `https://sageramo.github.io` |

口令错 10 次锁定该 IP 一小时；超过每日上限返回 429，页面会提示改用规则提取。`/api/health` 的 `model_access.code_required` 表示是否已开启。
