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
};
const STATUS_LABELS = {
  created: "已创建",
  preparing: "准备中",
  running: "执行中",
  succeeded: "已完成",
  needs_attention: "需要处理",
  failed: "失败",
  interrupted: "已中断",
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
};

const byId = (id) => document.getElementById(id);

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
  } catch (error) {
    byId("healthLabel").textContent = "本地服务不可用";
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
    state.currentRun = run;
    await renderRun(run, { forceScroll: changed });
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

  const runStatus = byId("runStatus");
  runStatus.className = `run-status status-${run.status}`;
  runStatus.textContent = statusLabel(run.status);
  runStatus.classList.remove("is-hidden");

  renderActions(run);
  renderActivity(run.activity || [], run);
  renderVerification(isBusy(run) ? [] : (run.verification || []));
  renderError(run);
  updateAssistantIntro(run);
  updateApproval(run.approval_request);
  updateComposerMode(run);

  byId("summaryBlock").classList.add("is-hidden");
  byId("diffBlock").classList.add("is-hidden");
  byId("summaryContent").textContent = "";
  byId("diffContent").textContent = "";

  const jobs = [];
  if (!isBusy(run) && run.artifacts?.summary) jobs.push(loadSummary(run.run_id, renderToken));
  if (!isBusy(run) && run.has_changes) jobs.push(loadDiff(run.run_id, renderToken));
  await Promise.allSettled(jobs);
  scrollConversation(forceScroll);
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
      content.textContent = turn.summary.trim();
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
    assistant.append(assistantAvatar, assistantBody);
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
    message = run.current_step
      ? `正在执行：${stepLabel(run.current_step.replace(/^approval:/, ""))}`
      : "正在执行任务，页面会自动更新真实运行输出。";
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

function renderActions(run) {
  const busy = isBusy(run);
  const hasApproval = Boolean(run.approval_request);
  const outOfScope = isOutOfScope(run);
  byId("resumeButton").classList.toggle(
    "is-hidden",
    busy || hasApproval || outOfScope || run.general_only || run.analysis_only || !["failed", "interrupted"].includes(run.status),
  );
  byId("rerunButton").classList.toggle(
    "is-hidden",
    busy || hasApproval || outOfScope || run.answer_only || run.general_only || run.analysis_only || ACTIVE_STATUSES.has(run.status),
  );
}

function renderActivity(activity, run) {
  const list = byId("activityList");
  list.replaceChildren();
  const terminal = !isBusy(run) && !run.approval_request;
  const visible = activity.filter((step) => {
    if (terminal && step.status === "pending") return false;
    return !(step.status === "skipped" && String(step.error || "").includes("cancelled before execution"));
  });

  if (!visible.length && isBusy(run)) {
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
    const visualStatus = displayStatus === "verification_failed" ? "failed" :
      displayStatus === "verification_passed" ? "success" : displayStatus;
    details.className = `activity-step status-${visualStatus}`;
    details.open = ["running", "failed", "waiting_approval", "verification_failed"].includes(displayStatus);

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

    if (index === visible.length - 1 && step.status === "success" && isBusy(run)) details.open = true;
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
    const name = document.createElement("span");
    name.textContent = tool.name;
    const meta = document.createElement("span");
    meta.textContent = tool.output === null ? "等待输出" : duration(tool.latency_ms) || "完成";
    summary.append(name, meta);
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
    byId("summaryContent").textContent = content.trim();
    byId("summaryBlock").classList.remove("is-hidden");
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
  renderRunList();
  byId("welcomeState").classList.remove("is-hidden");
  byId("conversation").classList.add("is-hidden");
  byId("chatTitle").textContent = "新任务";
  byId("chatMeta").textContent = "本机隔离执行";
  byId("runStatus").classList.add("is-hidden");
  byId("resumeButton").classList.add("is-hidden");
  byId("rerunButton").classList.add("is-hidden");
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

async function submitTask(event) {
  event.preventDefault();
  const task = byId("taskInput").value.trim();
  if (!task) return;
  const followup = Boolean(state.currentRunId);
  if (followup && isBusy(state.currentRun)) {
    showToast("请等待当前轮次完成后再继续追问", true);
    return;
  }
  const button = byId("submitTaskButton");
  button.disabled = true;
  button.textContent = followup ? "发送中" : "创建中";
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
    };
    const result = await api(path, {
      method: "POST",
      body: JSON.stringify(body),
    });
    state.currentRunId = result.run_id;
    state.currentRun = null;
    byId("taskInput").value = "";
    autoGrowComposer();
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
  byId("composerOptions").classList.toggle("is-hidden", continuing);
  byId("taskInput").placeholder = continuing
    ? "继续补充资料或追问，按 Enter 发送…"
    : "描述任意任务，按 Enter 提交…";
  byId("submitTaskButton").textContent = continuing ? "发送" : "提交";
  byId("submitTaskButton").disabled = busy;
  if (continuing) {
    byId("composerNote").textContent = busy
      ? "当前轮次执行中，完成后可继续补充资料"
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
    showToast(action === "resume" ? "恢复任务已入队" : "重新验证已入队");
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
  const active = isBusy(state.currentRun);
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
  byId("taskInput").addEventListener("input", autoGrowComposer);
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
  byId("approveApprovalButton").addEventListener("click", () => decideApproval("approved"));
  byId("rejectApprovalButton").addEventListener("click", () => decideApproval("rejected"));
  byId("sidebarOpenButton").addEventListener("click", () => document.body.classList.add("sidebar-open"));
  byId("sidebarCloseButton").addEventListener("click", () => document.body.classList.remove("sidebar-open"));
  byId("sidebarScrim").addEventListener("click", () => document.body.classList.remove("sidebar-open"));
  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => {
      byId("taskInput").value = button.dataset.prompt;
      autoGrowComposer();
      byId("taskInput").focus();
    });
  });
  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      const value = byId(button.dataset.copyTarget).textContent;
      try {
        await navigator.clipboard.writeText(value);
        showToast("已复制");
      } catch (_) {
        showToast("浏览器未允许复制", true);
      }
    });
  });
  document.addEventListener("click", (event) => {
    if (!byId("sourceMenu").contains(event.target) && !byId("sourceButton").contains(event.target)) {
      byId("sourceMenu").classList.add("is-hidden");
      byId("sourceButton").setAttribute("aria-expanded", "false");
    }
  });
}

bindEvents();
updateSourceMode();
autoGrowComposer();
loadHealth();
loadRuns();
