> 历史课程版技术说明：保留原作记录。当前本地v1.1已更换部分函数和容量口径，现行说明见 [time_capacity_v1_1.md](time_capacity_v1_1.md)；原说明不能用于宣称新版本的实现或能力。

# 自研技术与后端说明

> 历史技术说明：正文保留课程原型的部署与接线描述，部分状态已过时；当前改造阶段与运行限制以仓库README和工作区升级记录为准。

本文用于解释“司序·知所先后”（原课程作品“口述行动链”）项目中哪些部分是自研实现、分别由哪些代码完成、在智能体中如何发挥作用，以及当前公开版本和本地后端之间的关系。

## 一句话说明

本项目不是只有 Dify 工作流。当前系统由三层组成：

1. **Dify 工作流层**：负责对话入口、问题分类、知识库检索和自然语言生成。
2. **自研 Python 后端层**：负责文本清洗、规则解析、多时间点识别、冲突检测和 API 输出。
3. **Netlify 前端展示层**：负责公开访问、包装 Dify 智能体，并展示自研算法说明页。

目前公开 Netlify 页面主要承载“产品前端 + Dify 智能体 + 静态算法展示”。Python 后端运行在本地，用于演示和验证自研算法；如果后续将后端部署为公网 API，Dify 的 HTTP 节点就可以直接调用这些算法。

---

## 1. 文本读取与清洗

代码位置：

- `campus_agent/text_io.py`

实现内容：

- 读取 `.txt`、`.pdf`、`.docx`、`.xlsx`、`.json`、`.jsonl`、`.md`。
- 修复部分群通知复制后出现的中文编码错位。
- 将不同格式资料统一转成纯文本，供解析器和知识库整理脚本使用。

核心函数：

- `fix_mojibake(text)`
- `read_text_file(path)`
- `collect_documents(root)`

在项目中的作用：

- 用于整理真实学校通知、活动策划、学校规章。
- 用于生成 Dify 可上传的合并知识库文件。
- 用于后端解析接口读取用户材料。

对应脚本：

- `scripts/export_dify_knowledge.py`

该脚本把 `data/` 下的真实资料合并成：

- `dify_upload/办事通知库.md`
- `dify_upload/活动策划库.md`
- `dify_upload/学校规章库.md`
- `dify_upload/时间冲突案例库.md`

这些文件已经上传到 Dify 知识库。

---

## 2. 时间正则抽取与轻量 NLP

代码位置：

- `campus_agent/time_parser.py`

实现内容：

- 用正则表达式抽取日期和时间。
- 支持常见校园通知格式，例如：
  - `2026年5月25日15:00`
  - `6月12日16:00`
  - `5.13-5.21`
  - `8:00-18:00`
  - `12:30`
- 根据时间前后的关键词判断时间语义。

核心函数：

- `extract_time_candidates(text)`
- `infer_time_type(context)`
- `infer_by_proximity(prefix, suffix)`
- `normalize_match(match)`

可识别的时间语义：

- 报名截止
- 材料提交
- 活动时间
- 考试时间
- 选课时间
- 面试时间
- 抽签/签到时间
- 结果/公示时间

在项目中的作用：

- 解决老师指出的“一个通知里有多个时间点，大模型容易张冠李戴”的问题。
- 在 Dify 回答中，多时间点解析能力通过提示词和知识库体现；在本地后端中，时间识别由该模块确定性完成。
- Netlify 的 `algorithm.html` 页面展示了这类规则解析思路和示例。

---

## 3. 通知结构化解析

代码位置：

- `campus_agent/extractor.py`
- `campus_agent/models.py`

实现内容：

- 将一段通知文本转为统一任务结构。
- 抽取任务类别、标题、时间点、地点、负责部门、材料、行动步骤、缺失字段。

核心函数：

- `parse_notice_text(text, source, source_category)`
- `parse_notice_file(path, source_category)`
- `infer_category(text, source_category)`
- `extract_materials(text)`
- `missing_fields(task)`

输出结构示例：

```json
{
  "task_name": "羽毛球公开赛通知",
  "category": "竞赛/活动",
  "time_points": [
    {
      "time_type": "报名截止",
      "value": "2026-05-25 15:00",
      "evidence": "请于2026年5月25日15:00前完成报名"
    }
  ],
  "location": "体育部105会议室",
  "materials": [],
  "missing_fields": ["比赛地点"],
  "steps": ["确认关键时间点", "在报名截止前完成报名"]
}
```

在项目中的作用：

- 它是“通知变行动清单”的自研核心。
- 本地 API `/api/parse` 会调用该模块。
- 如果后续把 API 暴露到公网，Dify 的 HTTP 节点可以调用 `/api/parse`，让 Dify 回答基于确定性解析结果。

---

## 4. 冲突检测与劝退机制

代码位置：

- `campus_agent/conflict.py`

实现内容：

检测四类冲突：

1. `hard_time_overlap`：固定时间重叠。
2. `capacity_shortage`：截止前可用时间不足。
3. `exam_review_pressure`：期末复习压力过高。
4. `sequence_risk`：前置材料或准备任务风险。

核心函数：

- `detect_conflicts(tasks_data, now)`
- `working_capacity(start, end)`
- `recommend(tasks, conflicts)`

在项目中的作用：

- 支撑老师建议中的“时间冲突检测与劝退机制”。
- 能输出“必须保留 / 建议调整 / 建议委托或放弃”。
- Dify 中的冲突检测分支通过提示词复现了同样的规则逻辑；本地后端则给出可程序化的结构化冲突判断。

典型案例：

```text
6月29日开始期末考试，前两周复习高数、概率论、马原、英语、物理、离散数学。
同时6月18日前处理大广赛，6月18日晚有学生工作例会。
```

系统估算：

- 复习约 63 小时。
- 大广赛和会议约 21.5 小时。
- 两周默认可用时间约 72 小时。
- 缺口约 12.5 小时。

输出建议：

- 必须保留：六门考试复习。
- 建议调整：大广赛拆分、提前处理。
- 建议委托或放弃：非核心学生工作例会。

---

## 5. 后端 API

代码位置：

- `campus_agent/server.py`

实现方式：

- 使用 Python 标准库 `ThreadingHTTPServer`。
- 不依赖 FastAPI/Flask，减少环境安装风险。
- 同时提供静态页面和 JSON API。

主要接口：

```text
GET  /api/health
GET  /api/summary
GET  /api/multitime
GET  /api/conflict-cases
GET  /api/analyze-case?id=CF-003
POST /api/parse
POST /api/conflicts
```

接口用途：

- `/api/parse`：输入通知文本，输出结构化行动任务。
- `/api/conflicts`：输入任务列表，输出冲突和取舍建议。
- `/api/summary`：输出数据集统计。
- `/api/analyze-case`：分析内置冲突案例。

当前状态：

- 本地可运行：

```powershell
python -m campus_agent.server
```

- 本地访问：

```text
http://127.0.0.1:8765
```

重要说明：

由于 `cloud.dify.ai` 无法访问本机的 `127.0.0.1`，所以当前 Dify 发布版没有直接调用本地 Python API。若要让 Dify 真正调用这些自研接口，需要将后端部署到公网，例如：

- Netlify Functions
- Render
- Railway
- Cloudflare Tunnel 临时映射
- 学校服务器或云服务器

---

## 6. Netlify 前端

代码位置：

- `web/deploy/index.html`
- `web/deploy/agent.css`
- `web/deploy/agent.js`
- `web/deploy/algorithm.html`
- `web/deploy/algorithm.css`

实现内容：

- `index.html`：产品化前端，嵌入 Dify 智能体。
- `algorithm.html`：公开可访问的自研算法展示页。
- `agent.js`：一键复制四个演示问题。

公开访问：

```text
https://verbal-action-chain.netlify.app
```

自研算法演示页：

```text
https://verbal-action-chain.netlify.app/algorithm.html
```

在项目中的作用：

- 避免答辩时只展示裸 Dify 页面。
- 让老师和同学通过公开链接体验产品。
- 通过算法页说明项目的技术实现，不只是低代码搭建。

---

## 7. Dify 工作流和自研算法的关系

当前 Dify 已实现：

- 用户输入。
- 问题分类器。
- 四类知识库检索。
- 四条 LLM 分支。
- 直接回复。
- 发布公开聊天应用。

Dify 中实际运用的自研思想：

- 多时间点区分规则写入了提示词。
- 期末复习默认估算规则写入了冲突检测分支提示词。
- 知识库内容由自研脚本整理生成。
- 冲突案例和多时间点样本由自研数据文件提供。

当前没有完全打通的部分：

- Dify 尚未通过 HTTP 节点调用本地 `/api/parse` 和 `/api/conflicts`。
- 原因是 Dify 云端无法访问本机地址。

这意味着：

- **Dify 展示的是智能体体验和工作流。**
- **本地 Python 后端展示的是自研算法能力。**
- **Netlify 算法页展示的是自研逻辑说明和案例。**

如果后续部署后端，完整线上链路应为：

```text
Netlify 前端
  ↓
Dify 智能体
  ↓
HTTP 节点调用公网 API
  ↓
自研 parse/conflicts 服务
  ↓
Dify 汇总解释并回复用户
```

---

## 8. 答辩时如何讲技术含量

建议话术：

> 我们没有把所有任务都交给大模型，而是采用 Dify 编排和自研规则引擎结合的方式。Dify 负责对话、分类、知识库检索和自然语言生成；自研部分负责文本清洗、规则抽取、多时间点语义识别和冲突检测。这样可以降低大模型在时间和材料字段上张冠李戴的风险。

可以重点展示：

1. Dify 工作流截图：说明智能体编排。
2. Netlify 首页：说明产品化前端。
3. Dify 四个测试案例：说明用户体验。
4. `algorithm.html`：说明自研算法逻辑。
5. 本地后端代码或接口：说明后端真实存在。

---

## 9. 当前项目边界

已经完成：

- Dify 智能体发布。
- Netlify 前端部署。
- 自研算法演示页。
- Python 后端与 API。
- 真实数据整理与知识库构建。
- 多时间点和冲突案例标注。

仍可优化：

- 将 Python API 部署为公网服务。
- 让 Dify HTTP 节点直接调用 `/api/parse` 与 `/api/conflicts`。
- 增加 OCR 图片识别。
- 增加日历视图和任务拖拽。
- 增加更严格的评估指标。

---

## 结论

这个项目不是只有工作流和前端包装。它已经包含：

- 数据处理脚本。
- 规则解析算法。
- 时间语义识别。
- 冲突检测与劝退机制。
- Python 后端 API。
- Netlify 前端。
- Dify 智能体工作流。

只是当前为了保证一周内稳定演示，线上公开版本以 Dify 和静态前端为主；自研算法以本地后端和公开算法说明页展示。后续只要把 Python API 云端化，就能把所有自研算法完整接入 Dify 工作流。
