# Dify 工作流对接说明

本项目建议继续沿用 Dify 做编排层，自研 Python 服务做确定性解析与冲突检测。

## 推荐工作流

```text
START
  ↓
输入类型分类器
  ├─ 校园办事/通知解析
  ├─ 活动策划
  ├─ 冲突检测
  └─ 普通咨询
  ↓
知识库检索
  ↓
HTTP 调用：/api/parse
  ↓
字段完整性判断
  ├─ 缺字段 → 追问用户
  └─ 字段足够 → 行动清单生成
  ↓
HTTP 调用：/api/conflicts
  ↓
最终回答
```

## HTTP 工具配置

本地演示地址：

```text
http://127.0.0.1:8765
```

解析通知：

```http
POST /api/parse
Content-Type: application/json

{
  "text": "{{用户输入或 OCR 文本}}"
}
```

冲突检测：

```http
POST /api/conflicts
Content-Type: application/json

{
  "now": "2026-06-17 12:00",
  "tasks": [
    {
      "task_id": "T1",
      "name": "高等数学期末复习",
      "category": "学习",
      "deadline": "2026-06-29 08:00",
      "estimated_hours": 14,
      "priority": 95,
      "must_do": true,
      "fixed_time": null
    }
  ]
}
```

## Dify 节点建议

### 1. 输入类型分类器

分类：

- `notice_parse`：用户粘贴通知、PDF/OCR 文本、办事说明。
- `activity_plan`：用户要求策划活动或生成执行单。
- `conflict_check`：用户提供多个任务，希望判断能否安排。
- `general_qa`：普通咨询。

### 2. 知识库检索

建议分库：

- 办事通知库
- 活动策划库
- 学校规章库
- 多时间点样例库

### 3. 通知解析 HTTP 节点

将用户输入和 OCR 文本传给 `/api/parse`，得到结构化 JSON。

### 4. 字段完整性判断

如果返回的 `missing_fields` 不为空，优先追问，不要直接编造。

### 5. 冲突检测 HTTP 节点

把已知任务转换为 `/api/conflicts` 所需格式，得到冲突报告和取舍建议。

### 6. 最终回答节点

最终回答应包含：

- 任务理解
- 关键时间
- 行动清单
- 缺失信息
- 冲突与风险
- 建议保留/调整/委托或放弃的事项

## 展示话术

我们不是把全部判断交给大模型，而是采用“Dify 编排 + 自研确定性工具”的混合架构：

- Dify 负责对话、分类、知识库和多轮追问。
- 自研工具负责文本清洗、时间抽取、冲突检测和任务取舍。
- 大模型负责解释、补全和自然语言表达。
