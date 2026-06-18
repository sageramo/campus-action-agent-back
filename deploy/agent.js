const appShell = document.getElementById("appShell");
const statusEl = document.getElementById("copyStatus");
const resetButton = document.getElementById("resetHint");
const sidebarToggle = document.getElementById("sidebarToggle");
const apiStatus = document.getElementById("apiStatus");
const apiOutput = document.getElementById("apiOutput");
const healthButton = document.getElementById("runHealthCheck");
const parseButton = document.getElementById("runParseDemo");
const conflictButton = document.getElementById("runConflictDemo");
const difyUrl = "https://udify.app/chat/719EqByTRKYYG25i";
const localApiBase = "http://127.0.0.1:8765";

function getApiBase() {
  if (window.CAMPUS_AGENT_API) return window.CAMPUS_AGENT_API.replace(/\/$/, "");
  if (localStorage.getItem("campusAgentApi")) return localStorage.getItem("campusAgentApi").replace(/\/$/, "");
  if (location.protocol === "file:") return localApiBase;
  return location.origin;
}

function setSidebarCollapsed(collapsed) {
  if (!appShell || !sidebarToggle) return;

  appShell.classList.toggle("sidebar-collapsed", collapsed);
  sidebarToggle.setAttribute("aria-expanded", String(!collapsed));
  sidebarToggle.setAttribute("aria-label", collapsed ? "expand sidebar" : "collapse sidebar");
  localStorage.setItem("sidebarCollapsed", collapsed ? "1" : "0");
}

async function copyPrompt(prompt) {
  if (!statusEl) return;

  try {
    await navigator.clipboard.writeText(prompt);
    statusEl.textContent = "Copied";
  } catch {
    statusEl.textContent = "Copy manually";
  }

  window.setTimeout(() => {
    statusEl.textContent = "Click to copy";
  }, 2200);
}

document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    copyPrompt(button.dataset.prompt);
  });
});

if (resetButton) {
  resetButton.addEventListener("click", () => {
    window.open(difyUrl, "_blank", "noopener,noreferrer");
  });
}

if (sidebarToggle && appShell) {
  const savedState = localStorage.getItem("sidebarCollapsed");
  setSidebarCollapsed(savedState === null ? true : savedState === "1");

  sidebarToggle.addEventListener("click", () => {
    setSidebarCollapsed(!appShell.classList.contains("sidebar-collapsed"));
  });
}

function showApiResult(title, data) {
  if (!apiOutput) return;
  apiOutput.textContent = `${title}\n\n${JSON.stringify(data, null, 2)}`;
}

function showApiError(error) {
  if (!apiOutput) return;
  apiOutput.textContent = `后端 API 暂未连接。\n\n请先启动或部署 Python 后端：\npython -m campus_agent.server\n\n如果后端部署在其他域名，可在浏览器控制台设置：\nlocalStorage.setItem("campusAgentApi", "https://你的后端域名")\n\n错误信息：${error.message || error}`;
}

async function apiFetch(path, options = {}) {
  const base = getApiBase();
  const response = await fetch(`${base}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  if (apiStatus) apiStatus.textContent = `API 已连接：${base}`;
  return response.json();
}

async function runHealthCheck() {
  try {
    showApiResult("正在检查 API 连接...", {});
    const data = await apiFetch("/api/health");
    showApiResult("GET /api/health", data);
  } catch (error) {
    showApiError(error);
  }
}

async function runParseDemo() {
  const text = "请于2026年5月25日15:00前完成报名。现场抽签时间为2026年5月27日中午12:30，地点为体育部105会议室。比赛时间为2026年5月30日8点至18点。";
  try {
    showApiResult("正在调用通知解析 API...", { text });
    const data = await apiFetch("/api/parse", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    showApiResult("POST /api/parse", data);
  } catch (error) {
    showApiError(error);
  }
}

async function runConflictDemo() {
  const payload = {
    now: "2026-06-15 08:00",
    tasks: [
      { task_id: "T1", name: "高数复习", category: "学习", priority: 95, estimated_hours: 12, must_do: true, deadline: "2026-06-29 09:00" },
      { task_id: "T2", name: "概率论复习", category: "学习", priority: 90, estimated_hours: 12, must_do: true, deadline: "2026-06-29 09:00" },
      { task_id: "T3", name: "物理复习", category: "学习", priority: 88, estimated_hours: 12, must_do: true, deadline: "2026-06-29 09:00" },
      { task_id: "T4", name: "离散数学复习", category: "学习", priority: 88, estimated_hours: 12, must_do: true, deadline: "2026-06-29 09:00" },
      { task_id: "T5", name: "英语复习", category: "学习", priority: 82, estimated_hours: 7.5, must_do: true, deadline: "2026-06-29 09:00" },
      { task_id: "T6", name: "马原复习", category: "学习", priority: 82, estimated_hours: 7.5, must_do: true, deadline: "2026-06-29 09:00" },
      { task_id: "T7", name: "大广赛作品提交", category: "竞赛", priority: 70, estimated_hours: 20, must_do: false, deadline: "2026-06-18 16:00" },
      { task_id: "T8", name: "学生工作例会", category: "学生工作", priority: 45, estimated_hours: 1.5, must_do: false, fixed_time: { start: "2026-06-18 19:00", end: "2026-06-18 20:30" } },
    ],
  };
  try {
    showApiResult("正在调用冲突检测 API...", payload);
    const data = await apiFetch("/api/conflicts", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    showApiResult("POST /api/conflicts", data);
  } catch (error) {
    showApiError(error);
  }
}

if (healthButton) healthButton.addEventListener("click", runHealthCheck);
if (parseButton) parseButton.addEventListener("click", runParseDemo);
if (conflictButton) conflictButton.addEventListener("click", runConflictDemo);
