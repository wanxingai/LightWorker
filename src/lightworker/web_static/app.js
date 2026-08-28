const ACTIVE_STATUSES = new Set(["created", "preparing", "running"]);
const STEP_LABELS = {
  route: "选择执行能力",
  general: "执行通用任务",
  analysis: "资料检索与分析",
  intake: "理解任务",
  context: "收集任务上下文",
  plan: "制定执行计划",
  execute: "执行任务",
  edit: "编辑文件",
  review: "复核变更",
  agentic_loop: "动态 Agentic Loop",
};
const STATUS_LABELS = {
  created: "已创建",
  preparing: "准备中",
  running: "执行中",
  succeeded: "已完成",
  needs_attention: "需要处理",
  failed: "失败",
  interrupted: "已中断",
  paused: "已暂停",
  waiting_input: "等待补充",
  waiting_approval: "等待确认",
  budget_limited: "预算已用尽",
  cancelled: "已取消",
  success: "完成",
  pending: "等待",
  skipped: "跳过",
  waiting_approval: "等待确认",
  verification_passed: "通过",
  verification_failed: "未通过",
};

const state = {
  runs: [],
  currentRunId: null,
  currentRun: null,
  pollTimer: null,
  approvalRequestId: null,
  renderToken: 0,
  eventSource: null,
  eventRefreshTimer: null,
  resourcesRunId: null,
  elapsedTimer: null,
};

const byId = (id) => document.getElementById(id);

function setMarkdownContent(element, markdown, citations = []) {
  if (!element) return;
  const source = String(markdown || "");
  if (window.LightWorkerMarkdown?.setContent) {
    window.LightWorkerMarkdown.setContent(element, source);
    enhanceCitations(element, citations);
    return;
  }
  element.dataset.rawMarkdown = source;
  element.textContent = source;
}

function canonicalSourceUrl(value) {
  try {
    const url = new URL(String(value || ""), window.location.href);
    if (!/^https?:$/.test(url.protocol)) return "";
    url.hash = "";
    return url.href;
  } catch (_) {
    return "";
  }
}

function citationButton(citation) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "citation-badge";
  button.textContent = `来源 ${citation.id}`;
  button.setAttribute("aria-label", `查看引用来源 ${citation.id}：${citation.title || citation.site || citation.url}`);
  button.setAttribute("aria-expanded", "false");
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    showCitationPopover(button, citation);
  });
  return button;
}

function enhanceCitations(element, citations) {
  const values = Array.isArray(citations) ? citations : [];
  const byUrl = new Map(values.map((item) => [canonicalSourceUrl(item.url), item]).filter(([url]) => url));
  const used = new Set();
  const anchors = [...element.querySelectorAll('a[href^="http://"], a[href^="https://"]')];
  anchors.forEach((anchor) => {
    const url = canonicalSourceUrl(anchor.href);
    let citation = byUrl.get(url);
    if (!citation) {
      citation = {
        id: values.length + used.size + 1,
        url: anchor.href,
        title: anchor.textContent.trim() || new URL(anchor.href).hostname,
        site: new URL(anchor.href).hostname.replace(/^www\./, ""),
        excerpt: "该来源由回答中的链接提供。",
      };
    }
    anchor.classList.add("citation-source-link");
    anchor.after(citationButton(citation));
    used.add(citation.id);
  });

  const unmatched = values.filter((item) => !used.has(item.id));
  if (!anchors.length && unmatched.length) {
    const target = element.querySelector("p:last-of-type, li:last-of-type") || element.lastElementChild || element;
    const tail = document.createElement("span");
    tail.className = "citation-tail";
    unmatched.forEach((item) => tail.append(citationButton(item)));
    target.append(" ", tail);
  }
}

function showCitationPopover(button, citation) {
  document.querySelectorAll(".citation-badge[aria-expanded='true']").forEach((item) => {
    if (item !== button) item.setAttribute("aria-expanded", "false");
  });
  button.setAttribute("aria-expanded", "true");
  const popover = byId("citationPopover");
  const sourceDate = citation.published_at
    ? citation.published_at
    : citation.observed_at
      ? `访问于 ${formatTime(citation.observed_at, true)}`
      : "";
  byId("citationMeta").textContent = [citation.site || "引用来源", sourceDate].filter(Boolean).join(" · ");
  byId("citationTitle").textContent = citation.title || citation.site || "引用文档";
  byId("citationExcerpt").textContent = citation.excerpt || "该来源没有可展示的内容摘要，请打开原文查看。";
  byId("citationLink").href = citation.url;
  popover.classList.remove("is-hidden");
  window.requestAnimationFrame(() => {
    const trigger = button.getBoundingClientRect();
    const width = popover.offsetWidth;
    const height = popover.offsetHeight;
    const left = Math.max(12, Math.min(trigger.left, window.innerWidth - width - 12));
    const below = trigger.bottom + 8;
    const top = below + height <= window.innerHeight - 12 ? below : Math.max(12, trigger.top - height - 8);
    popover.style.left = `${left}px`;
    popover.style.top = `${top}px`;
  });
}

function closeCitationPopover() {
  byId("citationPopover").classList.add("is-hidden");
  document.querySelectorAll(".citation-badge[aria-expanded='true']").forEach((item) => {
    item.setAttribute("aria-expanded", "false");
  });
}

function feedbackStorageKey(runId, role) {
  return `lightworker.feedback.${runId}.${role}`;
}

function savedFeedback(runId, role) {
  try {
    return window.localStorage.getItem(feedbackStorageKey(runId, role)) || "";
  } catch (_) {
    return "";
  }
}

function saveFeedback(runId, role, value) {
  try {
    if (value) window.localStorage.setItem(feedbackStorageKey(runId, role), value);
    else window.localStorage.removeItem(feedbackStorageKey(runId, role));
  } catch (_) {
    // Feedback remains active for the current render when storage is unavailable.
  }
}

function mountMessageActions(container, { text, createdAt, runId, role }) {
  container.replaceChildren();
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "message-action-button";
  copy.textContent = "复制";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(String(text || ""));
      showToast("已复制");
    } catch (_) {
      showToast("浏览器未允许复制", true);
    }
  });

  const like = document.createElement("button");
  like.type = "button";
  like.className = "message-action-button";
  like.textContent = "点赞";
  const complaint = document.createElement("button");
  complaint.type = "button";
  complaint.className = "message-action-button";
  complaint.textContent = "投诉";
  let selected = savedFeedback(runId, role);
  const refresh = () => {
    like.classList.toggle("is-active", selected === "like");
    complaint.classList.toggle("is-active", selected === "complaint");
    like.setAttribute("aria-pressed", String(selected === "like"));
    complaint.setAttribute("aria-pressed", String(selected === "complaint"));
  };
  like.addEventListener("click", () => {
    selected = selected === "like" ? "" : "like";
    saveFeedback(runId, role, selected);
    refresh();
    showToast(selected ? "已记录点赞" : "已取消点赞");
  });
  complaint.addEventListener("click", () => {
    selected = selected === "complaint" ? "" : "complaint";
    saveFeedback(runId, role, selected);
    refresh();
    showToast(selected ? "已记录投诉" : "已取消投诉");
  });
  refresh();

  const time = document.createElement("time");
  time.className = "message-created-at";
  time.dateTime = createdAt || "";
  time.textContent = formatTime(createdAt, true);
  container.append(copy, like, complaint, time);
  container.classList.remove("is-hidden");
}

function setSidebarCollapsed(collapsed, { persist = true } = {}) {
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  byId("sidebarCollapseButton").setAttribute("aria-expanded", String(!collapsed));
  byId("sidebarExpandButton").setAttribute("aria-expanded", String(!collapsed));
  if (!persist) return;
  try {
    window.localStorage.setItem("lightworker.sidebarCollapsed", collapsed ? "1" : "0");
  } catch (_) {
    // Keep the current state when storage is unavailable.
  }
}

function restoreSidebarState() {
  let collapsed = false;
  try {
    collapsed = window.localStorage.getItem("lightworker.sidebarCollapsed") === "1";
  } catch (_) {
    collapsed = false;
  }
  setSidebarCollapsed(collapsed, { persist: false });
}

function clearMarkdownContent(element) {
  if (!element) return;
  element.replaceChildren();
  delete element.dataset.rawMarkdown;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    cache: "no-store",
    ...options,
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch (_) {
      // Keep the HTTP status when the response is not JSON.
    }
    throw new Error(message);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response.text();
}

function showToast(message, error = false) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.classList.toggle("is-error", error);
  toast.classList.add("is-visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("is-visible"), 3200);
}

function formatTime(value, includeSeconds = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: includeSeconds ? "2-digit" : undefined,
  }).format(date);
}

function duration(value) {
  if (!value || Number.isNaN(Number(value))) return "";
  const milliseconds = Number(value);
  return milliseconds < 1000 ? `${Math.round(milliseconds)}ms` : `${(milliseconds / 1000).toFixed(1)}s`;
}

function elapsedDuration(value) {
  const totalSeconds = Math.max(0, Math.round(Number(value || 0) / 1000));
  const seconds = totalSeconds % 60;
  const totalMinutes = Math.floor(totalSeconds / 60);
  const minutes = totalMinutes % 60;
  const hours = Math.floor(totalMinutes / 60);
  if (hours) return `${hours}h ${minutes}m ${seconds}s`;
  if (totalMinutes) return `${totalMinutes}m ${seconds}s`;
  return `${seconds}s`;
}

function runElapsedMilliseconds(run, now = Date.now()) {
  const startedAt = Date.parse(run?.created_at || "");
  if (Number.isNaN(startedAt)) return 0;
  const active = isBusy(run) || Boolean(run?.approval_request);
  const endedAt = active ? now : Date.parse(run?.updated_at || "");
  return Math.max(0, (Number.isNaN(endedAt) ? now : endedAt) - startedAt);
}

function statusLabel(value) {
  return STATUS_LABELS[value] || value || "等待";
}

function stepLabel(name) {
  if (STEP_LABELS[name]) return STEP_LABELS[name];
  if (name.startsWith("verify_")) return `运行验证 ${Number(name.split("_")[1]) + 1}`;
  if (name.startsWith("repair_")) return `修复验证问题 ${name.split("_")[1]}`;
  return name || "执行阶段";
}

function isBusy(run) {
  return Boolean(
    run &&
      (ACTIVE_STATUSES.has(run.status) || ["queued", "running"].includes(run.job?.state)),
  );
}

function queuedMessages(run) {
  return Array.isArray(run?.message_queue)
    ? run.message_queue.filter((item) => ["pending", "running"].includes(item.status))
    : [];
}

function hasQueuedMessages(run) {
  return queuedMessages(run).some((item) => item.run_id !== run?.run_id);
}

function nearBottom(element) {
  return element.scrollHeight - element.scrollTop - element.clientHeight < 160;
}

function scrollConversation(force = false) {
  const scroll = byId("chatScroll");
  if (force || nearBottom(scroll)) {
    window.requestAnimationFrame(() => scroll.scrollTo({ top: scroll.scrollHeight, behavior: force ? "auto" : "smooth" }));
  }
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    byId("healthDot").classList.add("is-online");
    byId("healthLabel").textContent = health.model_configured ? "本地服务正常" : "模型配置缺失";
    byId("modelName").textContent = health.model || "未配置模型";
    byId("composerModel").textContent = health.model || "未配置模型";
  } catch (error) {
    byId("healthLabel").textContent = "本地服务不可用";
    byId("composerModel").textContent = "模型不可用";
    showToast(error.message, true);
  }
}

async function loadRuns({ quiet = false } = {}) {
  try {
    state.runs = await api("/api/runs");
    renderRunList();
    if (state.currentRunId) await loadRun(state.currentRunId, { quiet: true });
  } catch (error) {
    if (!quiet) showToast(`加载任务失败：${error.message}`, true);
  }
}

function renderRunList() {
  const list = byId("runList");
  list.replaceChildren();
  if (!state.runs.length) {
    const empty = document.createElement("p");
    empty.className = "run-card-meta";
    empty.textContent = "还没有任务记录";
    list.append(empty);
    return;
  }

  state.runs.forEach((run) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = `run-card${run.run_id === state.currentRunId ? " is-active" : ""}`;
    card.addEventListener("click", () => {
      document.body.classList.remove("sidebar-open");
      loadRun(run.run_id);
    });

    const title = document.createElement("span");
    title.className = "run-card-title";
    title.textContent = run.task;

    const meta = document.createElement("span");
    meta.className = "run-card-meta";
    const dot = document.createElement("span");
    dot.className = `mini-dot status-${run.status}`;
    const status = document.createElement("span");
    status.textContent = statusLabel(run.status);
    const time = document.createElement("span");
    time.textContent = formatTime(run.updated_at);
    meta.append(dot, status, time);
    card.append(title, meta);
    list.append(card);
  });
}

async function loadRun(runId, { quiet = false } = {}) {
  const changed = state.currentRunId !== runId;
  state.currentRunId = runId;
  renderRunList();
  try {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
    if (state.currentRunId !== runId) return;
    const dispatched = queuedMessages(run).find(
      (item) => item.status === "running" && item.run_id && item.run_id !== runId,
    );
    if (dispatched) {
      state.currentRunId = dispatched.run_id;
      await loadRun(dispatched.run_id, { quiet: true });
      return;
    }
    state.currentRun = run;
    await renderRun(run, { forceScroll: changed });
    connectEventStream(run);
    schedulePoll();
  } catch (error) {
    if (error.message.includes("run not found")) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = window.setTimeout(() => loadRun(runId, { quiet: true }), 700);
      return;
    }
    if (!quiet) showToast(`加载任务详情失败：${error.message}`, true);
  }
}

function connectEventStream(run) {
  if (!isBusy(run)) {
    if (state.eventSource) state.eventSource.close();
    state.eventSource = null;
    return;
  }
  const expected = `/api/runs/${encodeURIComponent(run.run_id)}/events`;
  if (state.eventSource?.url?.endsWith(expected)) return;
  if (state.eventSource) state.eventSource.close();
  const source = new EventSource(expected);
  source.addEventListener("update", () => {
    window.clearTimeout(state.eventRefreshTimer);
    state.eventRefreshTimer = window.setTimeout(() => loadRun(run.run_id, { quiet: true }), 160);
  });
  source.onerror = () => {
    source.close();
    if (state.eventSource === source) state.eventSource = null;
  };
  state.eventSource = source;
}

async function renderRun(run, { forceScroll = false } = {}) {
  const renderToken = ++state.renderToken;
  byId("welcomeState").classList.add("is-hidden");
  byId("conversation").classList.remove("is-hidden");
  const turns = run.conversation || [];
  byId("chatTitle").textContent = run.conversation_title || run.task;
  byId("chatMeta").textContent = `${turns.length || 1} 轮对话 · ${run.source_mode === "empty" ? "受管理空目录" : run.repo} · ${run.run_id.slice(0, 8)}`;
  renderConversationHistory(turns, run.run_id);
  byId("userPrompt").textContent = run.task;
  byId("userPromptMeta").textContent = `${formatTime(run.created_at, true)} · ${run.source_mode === "empty" ? "空目录" : "已有仓库"}`;
  mountMessageActions(byId("currentUserActions"), {
    text: run.task,
    createdAt: run.created_at,
    runId: run.run_id,
    role: "user",
  });
  byId("currentAssistantActions").replaceChildren();
  byId("currentAssistantActions").classList.add("is-hidden");

  const runStatus = byId("runStatus");
  runStatus.className = `run-status status-${run.status}`;
  runStatus.textContent = statusLabel(run.status);
  runStatus.classList.remove("is-hidden");

  renderActions(run);
  renderRuntimeInspector(run);
  renderActivity(run.activity || [], run);
  renderVerification(isBusy(run) ? [] : (run.verification || []));
  renderError(run);
  updateAssistantIntro(run);
  updateProcessPanel(run);
  updateApproval(run.approval_request);
  renderMessageQueue(run);
  updateComposerMode(run);

  byId("summaryBlock").classList.add("is-hidden");
  byId("diffBlock").classList.add("is-hidden");
  clearMarkdownContent(byId("summaryContent"));
  byId("diffContent").textContent = "";

  const jobs = [];
  if (!isBusy(run) && run.artifacts?.summary) jobs.push(loadSummary(run.run_id, renderToken));
  if (!isBusy(run) && run.has_changes) jobs.push(loadDiff(run.run_id, renderToken));
  await Promise.allSettled(jobs);
  scrollConversation(forceScroll);
}

function renderRuntimeInspector(run) {
  const inspector = byId("runtimeInspector");
  const hasGoal = Boolean(run.goal);
  const agents = run.agent_tree?.agents || [];
  const screenshots = run.browser_artifacts || [];
  inspector.classList.toggle("is-hidden", !run.unified_mode && !hasGoal && !agents.length && !screenshots.length);
  byId("goalPanel").classList.toggle("is-hidden", !hasGoal);
  byId("agentPanel").classList.toggle("is-hidden", !agents.length);
  byId("browserPanel").classList.toggle("is-hidden", !screenshots.length);
  byId("goalContent").textContent = hasGoal ? JSON.stringify(run.goal, null, 2) : "";

  const tree = byId("agentTreeContent");
  tree.replaceChildren();
  agents.forEach((agent) => {
    const item = document.createElement("div");
    item.className = `agent-node status-${agent.status || "pending"}`;
    item.style.marginLeft = `${Math.max(Number(agent.depth || 1) - 1, 0) * 18}px`;
    const title = document.createElement("strong");
    title.textContent = `${agent.role || "agent"} · ${agent.status || "pending"}`;
    const task = document.createElement("span");
    task.textContent = agent.task || "";
    item.append(title, task);
    if (agent.result?.trim()) {
      const details = document.createElement("details");
      details.className = "agent-result-details";
      const summary = document.createElement("summary");
      summary.textContent = agent.recovered ? "查看输出 · 已恢复" : "查看输出";
      const result = document.createElement("div");
      result.className = "markdown-text agent-result";
      setMarkdownContent(result, agent.result.trim());
      details.append(summary, result);
      item.append(details);
    }
    tree.append(item);
  });

  const gallery = byId("browserGallery");
  gallery.replaceChildren();
  screenshots.forEach((name) => {
    const link = document.createElement("a");
    link.href = `/api/runs/${encodeURIComponent(run.run_id)}/browser/${encodeURIComponent(name)}`;
    link.target = "_blank";
    const image = document.createElement("img");
    image.src = link.href;
    image.alt = name;
    link.append(image);
    gallery.append(link);
  });

  if (state.resourcesRunId !== run.run_id) {
    state.resourcesRunId = run.run_id;
    const resources = byId("resourcesContent");
    resources.replaceChildren();
    const button = document.createElement("button");
    button.className = "text-button";
    button.type = "button";
    button.textContent = "加载资源";
    button.addEventListener("click", () => loadRuntimeResources(run.run_id));
    resources.append(button);
  }
}

async function loadRuntimeResources(runId) {
  const root = byId("resourcesContent");
  root.textContent = "正在加载…";
  try {
    const [memory, skills, rag, mcp] = await Promise.all([
      api(`/api/runs/${encodeURIComponent(runId)}/memory`),
      api(`/api/runs/${encodeURIComponent(runId)}/skills`),
      api(`/api/runs/${encodeURIComponent(runId)}/rag`),
      api("/api/mcp"),
    ]);
    if (state.currentRunId !== runId) return;
    root.replaceChildren();
    root.append(resourceHeading(`Memory (${memory.length})`));
    memory.forEach((item) => {
      const row = resourceRow(`${item.status} · ${item.kind}`, item.content);
      if (item.status === "candidate") {
        row.append(resourceButton("提升", async () => {
          await api(`/api/runs/${encodeURIComponent(runId)}/memory/${encodeURIComponent(item.id)}/promote`, { method: "POST", body: "{}" });
          await loadRuntimeResources(runId);
        }));
      }
      row.append(resourceButton("删除", async () => {
        await api(`/api/runs/${encodeURIComponent(runId)}/memory/${encodeURIComponent(item.id)}`, { method: "DELETE" });
        await loadRuntimeResources(runId);
      }));
      root.append(row);
    });

    root.append(resourceHeading(`Skills (${skills.skills?.length || 0})`));
    (skills.skills || []).forEach((item) => root.append(resourceRow(`${item.name} · ${item.source}`, item.description)));

    root.append(resourceHeading(`RAG (${rag.length})`));
    const ingest = document.createElement("div");
    ingest.className = "resource-ingest";
    const input = document.createElement("input");
    input.placeholder = "工作区文档路径，例如 docs/guide.md";
    const ingestButton = resourceButton("索引", async () => {
      const paths = input.value.split("\n").map((value) => value.trim()).filter(Boolean);
      if (!paths.length) return;
      await api(`/api/runs/${encodeURIComponent(runId)}/rag`, { method: "POST", body: JSON.stringify({ paths }) });
      await loadRuntimeResources(runId);
    });
    ingest.append(input, ingestButton);
    root.append(ingest);
    rag.forEach((item) => {
      const row = resourceRow(item.path, `${item.chunks} chunks`);
      row.append(resourceButton("移除", async () => {
        await api(`/api/runs/${encodeURIComponent(runId)}/rag?path=${encodeURIComponent(item.path)}`, { method: "DELETE" });
        await loadRuntimeResources(runId);
      }));
      root.append(row);
    });

    const servers = Object.entries(mcp.servers || {});
    root.append(resourceHeading(`MCP (${servers.length})`));
    servers.forEach(([name, item]) => root.append(resourceRow(name, `${item.transport} · ${item.disabled ? "disabled" : "enabled"}`)));
  } catch (error) {
    root.textContent = `资源加载失败：${error.message}`;
  }
}

function resourceHeading(text) {
  const heading = document.createElement("strong");
  heading.className = "resource-heading";
  heading.textContent = text;
  return heading;
}

function resourceRow(title, description) {
  const row = document.createElement("div");
  row.className = "resource-row";
  const copy = document.createElement("span");
  const strong = document.createElement("strong");
  strong.textContent = title;
  const detail = document.createElement("small");
  detail.textContent = description || "";
  copy.append(strong, detail);
  row.append(copy);
  return row;
}

function resourceButton(label, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "text-button";
  button.textContent = label;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await handler();
    } catch (error) {
      showToast(`资源操作失败：${error.message}`, true);
      button.disabled = false;
    }
  });
  return button;
}

function renderConversationHistory(turns, currentRunId) {
  const history = byId("conversationHistory");
  history.replaceChildren();
  turns.filter((turn) => turn.run_id !== currentRunId).forEach((turn) => {
    const user = document.createElement("article");
    user.className = "message message-user history-message";
    const userAvatar = document.createElement("div");
    userAvatar.className = "message-avatar";
    userAvatar.textContent = "你";
    const userBody = document.createElement("div");
    userBody.className = "message-body";
    const userLabel = document.createElement("div");
    userLabel.className = "message-label";
    userLabel.textContent = "你";
    const prompt = document.createElement("div");
    prompt.className = "user-prompt";
    prompt.textContent = turn.message;
    const userMeta = document.createElement("div");
    userMeta.className = "message-meta";
    userMeta.textContent = `${formatTime(turn.created_at, true)} · 历史轮次`;
    userBody.append(userLabel, prompt, userMeta);
    user.append(userAvatar, userBody);

    const assistant = document.createElement("article");
    assistant.className = "message message-assistant history-message";
    const assistantAvatar = document.createElement("div");
    assistantAvatar.className = "message-avatar assistant-avatar";
    assistantAvatar.textContent = "L";
    const assistantBody = document.createElement("div");
    assistantBody.className = "message-body";
    const assistantLabel = document.createElement("div");
    assistantLabel.className = "message-label";
    assistantLabel.textContent = "LightWorker";
    const intro = document.createElement("div");
    intro.className = "assistant-intro";
    intro.textContent = `上一轮状态：${statusLabel(turn.status)}`;
    assistantBody.append(assistantLabel, intro);
    if (turn.summary?.trim()) {
      const block = document.createElement("section");
      block.className = "answer-block history-answer";
      const heading = document.createElement("div");
      heading.className = "answer-heading";
      const title = document.createElement("strong");
      title.textContent = "本轮总结";
      heading.append(title);
      const content = document.createElement("div");
      content.className = "markdown-text";
      setMarkdownContent(content, turn.summary.trim(), turn.citations || []);
      block.append(heading, content);
      assistantBody.append(block);
    } else if (turn.error) {
      const error = document.createElement("div");
      error.className = "run-error";
      error.textContent = turn.error;
      assistantBody.append(error);
    }
    if (turn.diff?.trim()) {
      const diff = document.createElement("details");
      diff.className = "history-diff";
      const summary = document.createElement("summary");
      summary.textContent = "查看本轮文件 diff";
      const content = document.createElement("pre");
      content.textContent = turn.diff;
      diff.append(summary, content);
      assistantBody.append(diff);
    }
    const assistantText = turn.summary?.trim() || turn.error || "";
    if (assistantText) {
      const actions = document.createElement("div");
      actions.className = "message-actions";
      mountMessageActions(actions, {
        text: assistantText,
        createdAt: turn.updated_at,
        runId: turn.run_id,
        role: "assistant",
      });
      assistantBody.append(actions);
    }
    assistant.append(assistantAvatar, assistantBody);
    const userActions = document.createElement("div");
    userActions.className = "message-actions";
    mountMessageActions(userActions, {
      text: turn.message,
      createdAt: turn.created_at,
      runId: turn.run_id,
      role: "user",
    });
    userBody.append(userActions);
    history.append(user, assistant);
  });
}

function updateAssistantIntro(run) {
  let message = "正在准备隔离工作区…";
  if (run.approval_request) {
    message = "执行已暂停，等待你确认高风险操作。之前的执行记录保留在下方。";
  } else if (run.job?.state === "queued") {
    message = "任务已进入本地队列，稍后开始执行。";
  } else if (isBusy(run)) {
    const current = run.current_step
      ? `当前正在${stepLabel(run.current_step.replace(/^approval:/, ""))}。`
      : "任务已经开始处理。";
    message = `${current}已开始的阶段和工具调用会按执行顺序逐行显示在下方。`;
  } else if (run.status === "succeeded") {
    message = run.unified_mode
      ? run.has_changes
        ? "任务已完成。下面是执行过程、最终回答和实际文件变更。"
        : "任务已完成。下面是执行过程和最终回答；本轮没有文件变更。"
      : run.general_only || run.analysis_only
      ? "任务已完成。下面是执行过程和最终回答。"
      : run.answer_only
      ? "已结合对话中的补充资料完成回答；本轮没有编辑文件。"
      : "任务已经完成。下面是执行过程、验证证据和最终变更。";
  } else if (isOutOfScope(run)) {
    message = "任务已停止：这个请求不属于 LightWorker 的编码执行范围。";
  } else if (run.status === "failed") {
    message = "任务执行失败。你可以展开阶段记录查看最后的有效输出。";
  } else if (run.status === "needs_attention") {
    message = "任务已停止，需要检查下面的执行结果后再决定下一步。";
  } else if (run.status === "interrupted") {
    message = "任务已中断，可以从保存的检查点恢复。";
  }
  byId("assistantIntro").textContent = message;
}

function updateProcessPanel(run) {
  const panel = byId("processPanel");
  const successful = run.status === "succeeded" && !isBusy(run) && !run.approval_request;
  const waiting = Boolean(run.approval_request);
  panel.classList.toggle("is-complete", successful);
  panel.classList.toggle("is-active", isBusy(run));
  panel.classList.toggle("needs-attention", !successful && !isBusy(run));
  panel.open = !successful;

  let label = "处理中";
  if (successful) label = "已处理";
  else if (waiting) label = "等待确认";
  else if (run.status === "paused") label = "已暂停";
  else if (["failed", "needs_attention", "interrupted", "budget_limited", "cancelled"].includes(run.status)) {
    label = "处理未完成";
  }
  byId("processLabel").textContent = label;

  const progress = byId("processProgress");
  const showProgress = isBusy(run) || waiting;
  progress.classList.toggle("is-hidden", !showProgress);
  if (showProgress) {
    const activity = (run.activity || []).filter((step) => step.status !== "skipped");
    const toolCount = activity.reduce((total, step) => total + (step.tools?.length || 0), 0);
    const currentIndex = Math.max(
      activity.findIndex((step) => ["running", "waiting_approval"].includes(step.status)) + 1,
      activity.filter((step) => step.status === "success").length,
      1,
    );
    const stage = run.unified_mode
      ? stepLabel(run.current_step?.replace(/^approval:/, "") || "agentic_loop")
      : `第 ${Math.min(currentIndex, Math.max(activity.length, 1))}/${Math.max(activity.length, 1)} 阶段`;
    progress.textContent = `${stage} · ${toolCount} 次工具调用`;
  }

  const updateElapsed = () => {
    if (state.currentRunId !== run.run_id) return;
    byId("processDuration").textContent = elapsedDuration(runElapsedMilliseconds(run));
  };
  updateElapsed();
  window.clearInterval(state.elapsedTimer);
  state.elapsedTimer = null;
  if (isBusy(run) || waiting) {
    state.elapsedTimer = window.setInterval(updateElapsed, 1000);
  }
}

function renderActions(run) {
  const busy = isBusy(run);
  const hasApproval = Boolean(run.approval_request);
  const outOfScope = isOutOfScope(run);
  byId("pauseButton").classList.toggle("is-hidden", !busy);
  byId("cancelButton").classList.toggle("is-hidden", !busy);
  byId("resumeButton").classList.toggle(
    "is-hidden",
    busy || hasApproval || outOfScope || !["failed", "interrupted", "paused", "needs_attention"].includes(run.status),
  );
  byId("rerunButton").classList.toggle(
    "is-hidden",
    busy || hasApproval || outOfScope || run.answer_only || run.general_only || run.analysis_only || ACTIVE_STATUSES.has(run.status),
  );
}

function renderActivity(activity, run) {
  const list = byId("activityList");
  list.replaceChildren();
  const activeProcess = isBusy(run) || Boolean(run.approval_request);
  const terminal = !activeProcess;
  const visible = activity.filter((step) => {
    if (activeProcess && step.status === "pending") return false;
    if (terminal && step.status === "pending") return false;
    return !(step.status === "skipped" && String(step.error || "").includes("cancelled before execution"));
  });

  if (!visible.length && activeProcess) {
    const pending = document.createElement("details");
    pending.className = "activity-step status-running";
    pending.open = true;
    const summary = document.createElement("summary");
    const title = document.createElement("span");
    title.className = "step-title";
    const marker = document.createElement("span");
    marker.className = "step-state";
    const text = document.createElement("span");
    text.textContent = run.job?.state === "queued" ? "等待本地 Worker" : "启动执行环境";
    title.append(marker, text);
    summary.append(title);
    pending.append(summary);
    list.append(pending);
    return;
  }

  visible.forEach((step, index) => {
    const displayStatus = activityDisplayStatus(step);
    const details = document.createElement("details");
    const agenticStream = step.name === "agentic_loop" && Boolean(
      step.tools?.length || step.notices?.length || step.output || step.error,
    );
    const visualStatus = displayStatus === "verification_failed" ? "failed" :
      displayStatus === "verification_passed" ? "success" : displayStatus;
    details.className = `activity-step status-${visualStatus}${agenticStream ? " agentic-stream" : ""}`;
    details.open = agenticStream || ["running", "failed", "waiting_approval", "verification_failed"].includes(displayStatus);

    const summary = document.createElement("summary");
    const title = document.createElement("span");
    title.className = "step-title";
    const marker = document.createElement("span");
    marker.className = "step-state";
    const text = document.createElement("span");
    text.textContent = stepLabel(step.name);
    title.append(marker, text);

    const meta = document.createElement("span");
    meta.className = "step-summary-meta";
    const toolCount = step.tools?.length || 0;
    meta.textContent = [statusLabel(displayStatus), duration(step.duration_ms), toolCount ? `${toolCount} 次工具调用` : ""]
      .filter(Boolean)
      .join(" · ");
    summary.append(title, meta);
    details.append(summary);

    const content = document.createElement("div");
    content.className = "step-details";
    if (step.tools?.length) content.append(renderTools(step.tools));
    const formattedOutput = formatStepOutput(step);
    if (formattedOutput) {
      const output = document.createElement("pre");
      output.className = "step-output";
      output.textContent = formattedOutput;
      content.append(output);
    }
    (step.notices || []).forEach((notice) => {
      const line = document.createElement("div");
      line.className = "notice-line";
      line.textContent = notice.message;
      content.append(line);
    });
    if (step.error && !String(step.error).includes("cancelled before execution")) {
      const error = document.createElement("div");
      error.className = "notice-line";
      error.textContent = step.error;
      content.append(error);
    }
    if (!content.childElementCount) {
      const empty = document.createElement("div");
      empty.className = "notice-line";
      empty.textContent = step.status === "pending" ? "等待前置阶段完成" : "此阶段没有额外输出";
      content.append(empty);
    }
    details.append(content);
    list.append(details);

    if (index === visible.length - 1 && step.status === "success" && activeProcess) details.open = true;
  });
}

function activityDisplayStatus(step) {
  if (step.status !== "success") return step.status;
  if (step.verification_passed === true) return "verification_passed";
  if (step.verification_passed === false) return "verification_failed";
  return step.status;
}

function renderTools(tools) {
  const list = document.createElement("div");
  list.className = "tool-list";
  tools.forEach((tool) => {
    const details = document.createElement("details");
    details.className = "tool-event";
    const summary = document.createElement("summary");
    const copy = document.createElement("span");
    copy.className = "tool-event-copy";
    const activity = toolActivity(tool);
    const kind = document.createElement("span");
    kind.className = "tool-kind";
    kind.textContent = activity.kind;
    const name = document.createElement("span");
    name.className = "tool-event-name";
    name.textContent = activity.label;
    const technicalName = document.createElement("code");
    technicalName.textContent = tool.name;
    copy.append(kind, name, technicalName);
    const meta = document.createElement("span");
    meta.className = "tool-event-meta";
    meta.textContent = tool.output === null ? "运行中" : duration(tool.latency_ms) || "完成";
    summary.append(copy, meta);
    const output = document.createElement("pre");
    const parts = [];
    if (tool.arguments) parts.push(`参数\n${tool.arguments}`);
    if (tool.output !== null && tool.output !== "") parts.push(`输出\n${tool.output}`);
    output.textContent = parts.join("\n\n") || "无文本输出";
    details.append(summary, output);
    list.append(details);
  });
  return list;
}

function toolActivity(tool) {
  const name = String(tool?.name || "tool").toLowerCase();
  if (name.includes("screenshot") || name.includes("image")) return { kind: "图像", label: "查看了图像" };
  if (name.includes("browser_open") || name.includes("navigate")) return { kind: "浏览器", label: "打开了网页" };
  if (name.includes("browser") || name.includes("http") || name.includes("search_web") || name.includes("web_search")) {
    return { kind: "网络", label: "读取了外部资料" };
  }
  if (name.includes("goal")) return { kind: "目标", label: "更新了任务目标" };
  if (name.includes("rag") || name.includes("memory")) return { kind: "知识", label: "读取了长期资料" };
  if (name.includes("shell") || name.includes("command") || name.includes("exec")) {
    return { kind: "命令", label: "运行了命令" };
  }
  if (name.includes("patch") || name.includes("write") || name.includes("edit")) {
    return { kind: "编辑", label: "修改了文件" };
  }
  if (name.includes("read") || name.includes("file") || name.includes("list") || name.includes("search_text")) {
    return { kind: "文件", label: "读取了工作区文件" };
  }
  if (name.includes("agent")) return { kind: "代理", label: "委托了子 Agent" };
  if (name.includes("git") || name.includes("diff")) return { kind: "变更", label: "检查了文件变更" };
  return { kind: "工具", label: "调用了工具" };
}

function renderVerification(results) {
  const card = byId("verificationCard");
  if (!results.length) {
    card.classList.add("is-hidden");
    card.replaceChildren();
    return;
  }
  const passed = results.filter((result) => result.passed || !result.required).length;
  const allPassed = passed === results.length;
  card.classList.remove("is-hidden");
  card.classList.toggle("has-failure", !allPassed);
  card.replaceChildren();
  const title = document.createElement("strong");
  title.textContent = allPassed ? "验证通过" : "验证未全部通过";
  const meta = document.createElement("span");
  meta.textContent = `${passed}/${results.length} 项通过 · ${results.map((item) => item.name).join("、")}`;
  card.append(title, meta);
}

function renderError(run) {
  const error = byId("runError");
  if (run.error && !run.approval_request && !isOutOfScope(run)) {
    error.textContent = run.error;
    error.classList.remove("is-hidden");
  } else {
    error.textContent = "";
    error.classList.add("is-hidden");
  }
}

function isOutOfScope(run) {
  const message = String(run?.error || "");
  return message.includes("不是编码任务") || message.includes("非编码任务");
}

function formatStepOutput(step) {
  if (!step.output) return "";
  let value;
  try {
    value = JSON.parse(step.output);
  } catch (_) {
    return step.output;
  }
  if (step.name === "plan" && value?.summary) {
    const lines = [value.summary.zh || value.summary.en || ""];
    (value.items || []).forEach((item, index) => {
      const description = item?.description?.zh || item?.description?.en || "";
      if (description) lines.push(`${index + 1}. ${description}`);
    });
    return lines.filter(Boolean).join("\n\n");
  }
  if (step.name === "review" && value?.summary) {
    const lines = [value.summary.zh || value.summary.en || ""];
    (value.changes || []).forEach((item) => lines.push(`变更：${item.zh || item.en || ""}`));
    (value.verification || []).forEach((item) => lines.push(`验证：${item.zh || item.en || ""}`));
    return lines.filter(Boolean).join("\n");
  }
  if (step.name.startsWith("verify_") && Array.isArray(value?.results)) {
    return value.results
      .map((item) => `${item.passed ? "通过" : "失败"} · ${item.name}\n${item.output_excerpt || ""}`.trim())
      .join("\n\n");
  }
  return step.output;
}

async function loadSummary(runId, renderToken) {
  try {
    const content = await api(`/api/runs/${encodeURIComponent(runId)}/artifacts/summary`);
    if (state.currentRunId !== runId || state.renderToken !== renderToken) return;
    setMarkdownContent(byId("summaryContent"), content.trim(), state.currentRun?.citations || []);
    byId("summaryBlock").classList.remove("is-hidden");
    mountMessageActions(byId("currentAssistantActions"), {
      text: content.trim(),
      createdAt: state.currentRun?.updated_at,
      runId,
      role: "assistant",
    });
  } catch (error) {
    showToast(`加载任务总结失败：${error.message}`, true);
  }
}

async function loadDiff(runId, renderToken) {
  try {
    const content = await api(`/api/runs/${encodeURIComponent(runId)}/artifacts/diff`);
    if (state.currentRunId !== runId || state.renderToken !== renderToken || !content.trim()) return;
    byId("diffContent").textContent = content;
    const files = (content.match(/^diff --git /gm) || []).length;
    const added = (content.match(/^\+(?!\+\+)/gm) || []).length;
    const removed = (content.match(/^-(?!--)/gm) || []).length;
    byId("diffStats").textContent = `${files || 1} 个文件 · +${added} / -${removed}`;
    byId("diffBlock").classList.remove("is-hidden");
  } catch (error) {
    showToast(`加载 diff 失败：${error.message}`, true);
  }
}

function updateApproval(approval) {
  const dialog = byId("approvalDialog");
  if (!approval) {
    state.approvalRequestId = null;
    if (dialog.open) dialog.close();
    return;
  }
  if (state.approvalRequestId === approval.request_id && dialog.open) return;
  state.approvalRequestId = approval.request_id;
  byId("approvalTitle").textContent = approval.title;
  byId("approvalDescription").textContent = approval.description;
  byId("approvalAction").textContent = approval.requested_action;
  byId("approvalNote").value = "";
  if (!dialog.open) dialog.showModal();
}

function showNewTask() {
  state.currentRunId = null;
  state.currentRun = null;
  state.renderToken += 1;
  state.approvalRequestId = null;
  window.clearTimeout(state.pollTimer);
  window.clearInterval(state.elapsedTimer);
  state.elapsedTimer = null;
  if (state.eventSource) state.eventSource.close();
  state.eventSource = null;
  renderRunList();
  byId("welcomeState").classList.remove("is-hidden");
  byId("conversation").classList.add("is-hidden");
  byId("chatTitle").textContent = "新任务";
  byId("chatMeta").textContent = "本机隔离执行";
  byId("runStatus").classList.add("is-hidden");
  byId("pauseButton").classList.add("is-hidden");
  byId("cancelButton").classList.add("is-hidden");
  byId("resumeButton").classList.add("is-hidden");
  byId("rerunButton").classList.add("is-hidden");
  byId("messageQueuePanel").classList.add("is-hidden");
  byId("messageQueueList").replaceChildren();
  if (byId("approvalDialog").open) byId("approvalDialog").close();
  byId("taskInput").value = "";
  updateComposerMode(null);
  autoGrowComposer();
  byId("taskInput").focus();
  document.body.classList.remove("sidebar-open");
}

function lines(value) {
  return value.split("\n").map((item) => item.trim()).filter(Boolean);
}

function renderMessageQueue(run) {
  const panel = byId("messageQueuePanel");
  const list = byId("messageQueueList");
  const items = queuedMessages(run).filter(
    (item) => !(item.status === "running" && item.run_id === run?.run_id),
  );
  list.replaceChildren();
  panel.classList.toggle("is-hidden", !items.length);
  byId("messageQueueCount").textContent = `${items.length} 条等待`;
  items.forEach((item, index) => {
    const row = document.createElement("article");
    row.className = `message-queue-item status-${item.status}`;
    const position = document.createElement("span");
    position.className = "message-queue-position";
    position.textContent = String(index + 1);
    const content = document.createElement("div");
    content.className = "message-queue-content";
    const message = document.createElement("p");
    message.textContent = item.message || "";
    const meta = document.createElement("span");
    meta.textContent = item.status === "running"
      ? "正在开始下一轮"
      : `等待执行 · ${formatTime(item.created_at, true)}`;
    content.append(message, meta);
    row.append(position, content);
    if (item.status === "pending" && isBusy(run)) {
      const guide = document.createElement("button");
      guide.type = "button";
      guide.className = "guide-button";
      guide.textContent = "引导";
      guide.setAttribute("aria-label", `立即用第 ${index + 1} 条消息引导当前任务`);
      guide.addEventListener("click", () => guideQueuedMessage(item.id, guide));
      row.append(guide);
    }
    list.append(row);
  });
}

async function guideQueuedMessage(itemId, button) {
  if (!state.currentRunId || !itemId) return;
  button.disabled = true;
  button.textContent = "发送中";
  try {
    await api(
      `/api/runs/${encodeURIComponent(state.currentRunId)}/queue/${encodeURIComponent(itemId)}/guide`,
      { method: "POST", body: "{}" },
    );
    showToast("已作为引导送入当前任务");
    await loadRun(state.currentRunId, { quiet: true });
  } catch (error) {
    showToast(`引导失败：${error.message}`, true);
    button.disabled = false;
    button.textContent = "引导";
  }
}

async function submitTask(event) {
  event.preventDefault();
  const task = byId("taskInput").value.trim();
  if (!task) return;
  const followup = Boolean(state.currentRunId);
  const button = byId("submitTaskButton");
  button.disabled = true;
  const busyFollowup = followup && isBusy(state.currentRun);
  button.textContent = busyFollowup ? "入队中" : followup ? "发送中" : "创建中";
  try {
    const sourceMode = document.querySelector('input[name="sourceMode"]:checked').value;
    const path = followup
      ? `/api/runs/${encodeURIComponent(state.currentRunId)}/followups`
      : "/api/runs";
    const body = followup ? { message: task } : {
      source_mode: sourceMode,
      repo: sourceMode === "existing" ? byId("repoInput").value : null,
      task,
      test_commands: lines(byId("testInput").value),
      lint_commands: lines(byId("lintInput").value),
      include_dirty: sourceMode === "existing" && byId("dirtyInput").checked,
      max_repairs: Number(byId("repairsInput").value),
      runtime_mode: byId("runtimeModeInput").value,
      goal_mode: byId("goalModeInput").checked,
    };
    const result = await api(path, {
      method: "POST",
      body: JSON.stringify(body),
    });
    byId("taskInput").value = "";
    autoGrowComposer();
    if (result.status === "followup_queued") {
      showToast("消息已挂起，将在当前任务结束后执行");
      await loadRun(state.currentRunId, { quiet: true });
      return;
    }
    state.currentRunId = result.run_id;
    state.currentRun = null;
    byId("welcomeState").classList.add("is-hidden");
    byId("conversation").classList.remove("is-hidden");
    byId("userPrompt").textContent = task;
    byId("assistantIntro").textContent = "任务已进入本地队列，正在创建隔离工作区…";
    byId("activityList").replaceChildren();
    showToast(followup ? "补充资料已发送" : "任务已创建");
    await loadRun(result.run_id, { quiet: true });
  } catch (error) {
    showToast(`创建任务失败：${error.message}`, true);
  } finally {
    updateComposerMode(state.currentRun);
  }
}

function updateComposerMode(run) {
  const continuing = Boolean(state.currentRunId);
  const busy = continuing && isBusy(run);
  const hasInput = Boolean(byId("taskInput").value.trim());
  byId("composerOptions").classList.toggle("is-hidden", continuing);
  byId("composerContext").classList.toggle("is-hidden", !continuing);
  byId("composerStopButton").classList.toggle("is-hidden", !busy || hasInput);
  byId("submitTaskButton").classList.toggle("is-hidden", busy && !hasInput);
  byId("taskInput").placeholder = continuing
    ? "继续补充资料或追问，按 Enter 发送…"
    : "描述任意任务，按 Enter 提交…";
  byId("submitTaskButton").textContent = continuing ? "发送" : "提交";
  byId("submitTaskButton").disabled = false;
  if (continuing) {
    byId("composerNote").textContent = busy
      ? "当前轮次执行中；发送内容会先挂起排队，也可在队列中点击“引导”立即影响当前规划"
      : "继续追问将沿用本对话上下文和上一轮隔离工作区";
  } else {
    updateSourceMode();
  }
}

function updateSourceMode() {
  const mode = document.querySelector('input[name="sourceMode"]:checked').value;
  const existing = mode === "existing";
  byId("sourceButtonLabel").textContent = existing ? "已有仓库" : "空目录";
  byId("repoRow").classList.toggle("is-hidden", !existing);
  byId("repoInput").required = existing;
  byId("dirtyInput").disabled = !existing;
  if (!existing) byId("dirtyInput").checked = false;
  byId("composerNote").textContent = existing
    ? "原仓库只作为只读来源 · 容器仅挂载隔离快照"
    : "默认从空目录开始 · 原仓库不会被容器挂载";
  byId("sourceMenu").classList.add("is-hidden");
  byId("sourceButton").setAttribute("aria-expanded", "false");
}

async function runAction(action) {
  if (!state.currentRunId) return;
  try {
    await api(`/api/runs/${encodeURIComponent(state.currentRunId)}/${action}`, {
      method: "POST",
      body: "{}",
    });
    const labels = {
      resume: "恢复任务已入队",
      rerun: "重新验证已入队",
      pause: "已请求暂停",
      cancel: "已请求取消",
    };
    showToast(labels[action] || "操作已提交");
    await loadRun(state.currentRunId, { quiet: true });
  } catch (error) {
    showToast(`操作失败：${error.message}`, true);
  }
}

async function decideApproval(decision) {
  if (!state.currentRunId || !state.approvalRequestId) return;
  const approveButton = byId("approveApprovalButton");
  const rejectButton = byId("rejectApprovalButton");
  approveButton.disabled = true;
  rejectButton.disabled = true;
  try {
    await api(`/api/runs/${encodeURIComponent(state.currentRunId)}/approval`, {
      method: "POST",
      body: JSON.stringify({ decision, note: byId("approvalNote").value }),
    });
    byId("approvalDialog").close();
    state.approvalRequestId = null;
    showToast(decision === "approved" ? "已允许，任务继续执行" : "已拒绝该操作");
    await loadRun(state.currentRunId, { quiet: true });
  } catch (error) {
    showToast(`提交确认失败：${error.message}`, true);
  } finally {
    approveButton.disabled = false;
    rejectButton.disabled = false;
  }
}

function schedulePoll(delay) {
  window.clearTimeout(state.pollTimer);
  if (!state.currentRunId) return;
  const active = isBusy(state.currentRun) || hasQueuedMessages(state.currentRun);
  if (!active) return;
  state.pollTimer = window.setTimeout(
    () => loadRuns({ quiet: true }),
    delay || 1800,
  );
}

function autoGrowComposer() {
  const input = byId("taskInput");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
}

function togglePanel(buttonId, panelId) {
  const button = byId(buttonId);
  const panel = byId(panelId);
  const opening = panel.classList.contains("is-hidden");
  panel.classList.toggle("is-hidden", !opening);
  button.setAttribute("aria-expanded", String(opening));
}

function bindEvents() {
  byId("newTaskButton").addEventListener("click", showNewTask);
  byId("refreshButton").addEventListener("click", () => loadRuns());
  byId("taskForm").addEventListener("submit", submitTask);
  byId("taskInput").addEventListener("input", () => {
    autoGrowComposer();
    updateComposerMode(state.currentRun);
  });
  byId("taskInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      byId("taskForm").requestSubmit();
    }
  });
  byId("sourceButton").addEventListener("click", () => togglePanel("sourceButton", "sourceMenu"));
  byId("settingsButton").addEventListener("click", () => togglePanel("settingsButton", "settingsPanel"));
  document.querySelectorAll('input[name="sourceMode"]').forEach((input) => input.addEventListener("change", updateSourceMode));
  byId("resumeButton").addEventListener("click", () => runAction("resume"));
  byId("rerunButton").addEventListener("click", () => runAction("rerun"));
  byId("pauseButton").addEventListener("click", () => runAction("pause"));
  byId("cancelButton").addEventListener("click", () => runAction("cancel"));
  byId("composerStopButton").addEventListener("click", () => runAction("cancel"));
  byId("approveApprovalButton").addEventListener("click", () => decideApproval("approved"));
  byId("rejectApprovalButton").addEventListener("click", () => decideApproval("rejected"));
  byId("sidebarOpenButton").addEventListener("click", () => document.body.classList.add("sidebar-open"));
  byId("sidebarCloseButton").addEventListener("click", () => document.body.classList.remove("sidebar-open"));
  byId("sidebarCollapseButton").addEventListener("click", () => setSidebarCollapsed(true));
  byId("sidebarExpandButton").addEventListener("click", () => setSidebarCollapsed(false));
  byId("sidebarScrim").addEventListener("click", () => document.body.classList.remove("sidebar-open"));
  byId("citationCloseButton").addEventListener("click", closeCitationPopover);
  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => {
      byId("taskInput").value = button.dataset.prompt;
      autoGrowComposer();
      byId("taskInput").focus();
    });
  });
  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      const target = byId(button.dataset.copyTarget);
      const value = target.dataset.rawMarkdown ?? target.textContent;
      try {
        await navigator.clipboard.writeText(value);
        showToast("已复制");
      } catch (_) {
        showToast("浏览器未允许复制", true);
      }
    });
  });
  document.addEventListener("click", (event) => {
    if (!byId("citationPopover").contains(event.target) && !event.target.closest?.(".citation-badge")) {
      closeCitationPopover();
    }
    if (!byId("sourceMenu").contains(event.target) && !byId("sourceButton").contains(event.target)) {
      byId("sourceMenu").classList.add("is-hidden");
      byId("sourceButton").setAttribute("aria-expanded", "false");
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeCitationPopover();
  });
  byId("chatScroll").addEventListener("scroll", closeCitationPopover, { passive: true });
}

bindEvents();
restoreSidebarState();
updateSourceMode();
autoGrowComposer();
loadHealth();
loadRuns();
