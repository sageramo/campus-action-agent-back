# 司序·知所先后后端 API 接入说明

> 历史技术说明：正文保留课程原型的部署与接线描述，部分状态已过时；当前改造阶段与运行限制以仓库README和工作区升级记录为准。

## 本地启动

在项目根目录运行：

```powershell
python -m campus_agent.server
```

默认地址：

```text
http://127.0.0.1:8765
```

常用接口：

- `GET /api/health`：健康检查。
- `GET /api/summary`：数据集概览。
- `POST /api/parse`：解析通知文本，抽取时间点、地点、部门、材料和行动步骤。
- `POST /api/conflicts`：检测任务冲突、容量不足和期末复习压力。

## 前端如何连接 API

前端会优先读取：

```js
window.CAMPUS_AGENT_API
```

配置文件位置：

```text
web/deploy/api-config.js
```

如果前端和后端部署在同一个域名，可以保持空字符串：

```js
window.CAMPUS_AGENT_API = "";
```

如果后端单独部署到 Render 或 Railway，需要改成后端公网地址：

```js
window.CAMPUS_AGENT_API = "https://你的后端域名";
```

## Render 部署

项目已经包含：

```text
requirements.txt
render.yaml
```

Render 上选择 New Web Service，连接 GitHub 仓库即可。关键配置：

- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `python -m campus_agent.server`
- Environment variable: `HOST=0.0.0.0`

Render 会自动提供 `PORT`，后端代码会读取这个端口。

## 汇报时可以这样说明

本项目不是只有 Dify 工作流。系统拆成三层：

1. 前端展示层：`web/deploy/`，提供产品化界面和 API 演示。
2. 智能体编排层：Dify 分类器、知识库检索和分支 Prompt。
3. 自研后端层：`campus_agent/`，提供规则解析、时间语义分类和冲突检测 API。

其中自研后端通过 `/api/parse` 和 `/api/conflicts` 接入前端，可独立于 Dify 运行。
