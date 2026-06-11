const NODE_TYPES = [
  "web.open",
  "web.scroll",
  "web.hover",
  "web.click",
  "web.input",
  "web.wait_for",
  "web.select",
  "web.extract",
  "web.close_modals",
  "ai.ask",
  "flow.wait",
  "flow.if",
  "flow.else",
  "flow.end_if",
  "flow.loop",
  "flow.end_loop"
];

const COMMAND_GROUPS = [
  {
    name: "网页自动化",
    mode: "standard",
    items: [
      { type: "web.open", icon: "🌐", title: "打开网页", params: { url: "https://example.com" } },
      { type: "web.scroll", icon: "↕", title: "滚动容器(web)", params: { target: "滚动容器", selector: ".list", position: "bottom" } },
      { type: "web.hover", icon: "↗", title: "悬停元素(web)", params: { target: "元素", selector: "text=目标元素" } },
      { type: "web.click", icon: "☝", title: "点击元素(web)", params: { target: "按钮", selector: "button:has-text(\"确定\")" } },
      { type: "web.input", icon: "✎", title: "填写输入框(web)", params: { target: "输入框", selector: "input[name=value]", value: "" } },
      { type: "web.wait_for", icon: "⏳", title: "等待元素/页面", params: { text: "完成" } },
      { type: "web.select", icon: "▾", title: "下拉选择(web)", params: { target: "下拉框", selector: "select", value: "" } },
      { type: "web.extract", icon: "⤓", title: "读取文本(web)", params: { target: "文本", selector: "body" } },
      { type: "web.close_modals", icon: "×", title: "关闭弹窗(web)", params: {} }
    ]
  },
  {
    name: "流程控制",
    mode: "standard",
    items: [
      { type: "flow.wait", icon: "⌛", title: "等待", params: { seconds: 1 } },
      { type: "flow.if", icon: "?", title: "IF 判断", params: { selector: "body", negate: false } },
      { type: "flow.else", icon: "↳", title: "Else", params: {} },
      { type: "flow.end_if", icon: "□", title: "End IF", params: {} },
      { type: "flow.loop", icon: "↻", title: "循环", params: { times: 2 } },
      { type: "flow.end_loop", icon: "↺", title: "End Loop", params: {} }
    ]
  },
  {
    name: "人工智能AI",
    mode: "ai",
    items: [
      { type: "ai.ask", icon: "✦", title: "AI 生成/判断", params: { prompt: "请基于当前页面给出下一步动作" } }
    ]
  }
];

const state = {
  workflowName: "",
  workflow: null,
  selectedIndex: 0,
  selectedElementId: "",
  elements: [],
  commandMode: "standard",
  busy: false,
  runAbortController: null,
  activeRunId: 0,
  logSteps: [],
  lastRepairQuestion: "",
  mimoErrorReported: false,
  agentImage: null,
  jsonDirty: false,
  propertyDirty: false
};

const $ = (id) => document.getElementById(id);

function setStatus(text) {
  $("status").textContent = text;
  const floatingStatus = $("floatingLogStatus");
  if (floatingStatus) floatingStatus.textContent = text;
}

function isMimoError(detail) {
  const text = String(detail || "");
  return (
    /MIMO_API_KEY/i.test(text) ||
    /Mimo (health check |request )?failed/i.test(text) ||
    /Mimo returned an empty response/i.test(text) ||
    /Mimo 暂时不可用/i.test(text) ||
    /xiaomimimo/i.test(text) && /(failed|error|unauthorized|timeout|timed out|401|403|失败|错误|超时)/i.test(text)
  );
}

function maybeReportMimoError(detail) {
  if (!isMimoError(detail) || state.mimoErrorReported) return;
  state.mimoErrorReported = true;
  setStatus("Mimo 暂时不可用，已保留当前界面");
}

function setBusy(value) {
  state.busy = value;
  for (const id of [
    "saveBtn", "validateBtn", "reloadBtn", "applyNodeBtn",
    "moveUpBtn", "moveDownBtn", "toggleDisableNodeBtn", "duplicateNodeBtn", "deleteNodeBtn"
  ]) {
    const el = $(id);
    if (el) el.disabled = value;
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "content-type": "application/json", ...(options.headers || {}) }
  });
  const payload = await response.json();
  if (!response.ok) throw payload;
  return payload;
}

function syncJsonFromWorkflow() {
  $("jsonEditor").value = JSON.stringify(state.workflow, null, 2);
  state.jsonDirty = false;
}

function syncWorkflowFromJson() {
  state.workflow = JSON.parse($("jsonEditor").value);
  if (!Array.isArray(state.workflow.nodes)) state.workflow.nodes = [];
  state.jsonDirty = false;
}

function syncSelectedNodeDisabledFromProperty() {
  const checkbox = $("nodeDisabled");
  const node = currentNode();
  if (!checkbox || !node) return;
  node.disabled = checkbox.checked;
  syncJsonFromWorkflow();
  renderCanvas();
  renderTree();
  renderToggleDisableButton();
}

function currentNode() {
  return state.workflow?.nodes?.[state.selectedIndex] || null;
}

function nodeIcon(type) {
  if (type === "web.open") return "🌐";
  if (type === "web.scroll") return "↕";
  if (type === "web.hover") return "↗";
  if (type === "web.click") return "☝";
  if (type === "web.input") return "✎";
  if (type === "web.wait_for") return "⏳";
  if (type === "web.select") return "▾";
  if (type === "web.extract") return "⤓";
  if (type === "web.close_modals") return "×";
  if (type === "ai.ask") return "✦";
  if (type === "flow.wait") return "⌛";
  if (type === "flow.if") return "?";
  if (type === "flow.else") return "↳";
  if (type === "flow.end_if") return "□";
  if (type === "flow.loop") return "↻";
  if (type === "flow.end_loop") return "↺";
  return "•";
}

function summarizeParams(node) {
  if (node.disabled) return `<span class="pill muted">执行时跳过</span>`;
  const params = node.params || {};
  const parts = [];
  if (params.url) parts.push(`打开 <span class="pill">${escapeHtml(params.url)}</span>`);
  if (params.target) parts.push(`目标 <span class="pill green">${escapeHtml(params.target)}</span>`);
  if (params.selector) parts.push(`selector <span class="pill">${escapeHtml(shorten(params.selector, 90))}</span>`);
  if (params.value) parts.push(`输入 <span class="pill">${escapeHtml(shorten(String(params.value), 50))}</span>`);
  if (params.text) parts.push(`等待文本 <span class="pill green">${escapeHtml(params.text)}</span>`);
  if (params.urlIncludes) parts.push(`等待 URL <span class="pill">${escapeHtml(params.urlIncludes)}</span>`);
  if (params.seconds) parts.push(`等待 <span class="pill">${escapeHtml(params.seconds)} 秒</span>`);
  if (params.times) parts.push(`循环 <span class="pill">${escapeHtml(params.times)} 次</span>`);
  if (params.negate) parts.push(`<span class="pill green">条件取反</span>`);
  if (params.prompt) parts.push(`prompt <span class="pill">${escapeHtml(shorten(params.prompt, 80))}</span>`);
  return parts.join("，") || escapeHtml(JSON.stringify(params));
}

function renderWorkflow() {
  if (!state.workflow) return;
  $("activeWorkflow").textContent = `${state.workflowName} / ${state.workflow.nodes.length} 节点`;
  renderCanvas();
  renderTree();
  renderProperty();
  renderElements();
  syncJsonFromWorkflow();
}

function renderCanvas() {
  const canvas = $("flowCanvas");
  canvas.innerHTML = "";
  state.workflow.nodes.forEach((node, index) => {
    const row = document.createElement("div");
    row.className = `node-row${index === state.selectedIndex ? " selected" : ""}${node.disabled ? " disabled" : ""}`;
    row.style.paddingLeft = `${Math.max(0, Number(node.indent || 0)) * 28}px`;
    row.onclick = () => selectNode(index);
    row.innerHTML = `
      <div class="node-number">${index + 1}${node.disabled ? '<span class="node-disabled-badge">禁用</span>' : ""}</div>
      <div class="node-icon">${nodeIcon(node.type)}</div>
      <div class="node-main">
        <div class="node-title">${escapeHtml(node.title || node.id)}${node.disabled ? '<span class="node-title-badge">禁用</span>' : ""}</div>
        <div class="node-summary">${summarizeParams(node)}</div>
      </div>`;
    canvas.appendChild(row);
  });
}

function renderTree() {
  const tree = $("flowTree");
  tree.innerHTML = "";
  const root = document.createElement("div");
  root.className = "tree-node";
  root.textContent = state.workflow.name || state.workflow.id;
  tree.appendChild(root);
  state.workflow.nodes.forEach((node, index) => {
    const item = document.createElement("div");
    item.className = `tree-node${index === state.selectedIndex ? " active" : ""}${node.disabled ? " disabled" : ""}`;
    item.style.paddingLeft = `${12 + Math.max(0, Number(node.indent || 0)) * 14}px`;
    item.textContent = `${index + 1}. ${node.disabled ? "[禁用] " : ""}${node.title || node.id}`;
    item.onclick = () => selectNode(index);
    tree.appendChild(item);
  });
}

function renderProperty() {
  const node = currentNode();
  $("nodeEmpty").classList.toggle("hidden", Boolean(node));
  $("nodeForm").classList.toggle("hidden", !node);
  if (!node) return;
  $("nodeId").value = node.id || "";
  $("nodeTitle").value = node.title || "";
  $("nodeType").value = node.type || NODE_TYPES[0];
  $("nodeIndent").value = Number(node.indent || 0);
  $("nodeDisabled").checked = Boolean(node.disabled);
  $("nodeParams").value = JSON.stringify(node.params || {}, null, 2);
  state.propertyDirty = false;
  renderToggleDisableButton();
}

function selectNode(index) {
  if (!flushPendingEdits({ silent: false })) return;
  state.selectedIndex = index;
  renderWorkflow();
}

function renderToggleDisableButton() {
  const button = $("toggleDisableNodeBtn");
  if (!button) return;
  const node = currentNode();
  button.textContent = node?.disabled ? "启用" : "禁用";
  button.title = node?.disabled ? "启用选中节点" : "禁用选中节点";
}

function renderCommands() {
  const root = $("commandList");
  const keyword = $("commandSearch").value.trim().toLowerCase();
  root.innerHTML = "";
  for (const group of COMMAND_GROUPS) {
    if (group.mode !== state.commandMode) continue;
    const filtered = group.items.filter((item) =>
      `${group.name} ${item.title} ${item.type}`.toLowerCase().includes(keyword)
    );
    if (!filtered.length) continue;
    const groupEl = document.createElement("div");
    groupEl.className = "command-group";
    groupEl.innerHTML = `<div class="group-title">${escapeHtml(group.name)}</div>`;
    for (const item of filtered) {
      const command = document.createElement("div");
      command.className = "command-item";
      command.innerHTML = `<span class="command-icon">${item.icon}</span><span>${escapeHtml(item.title)}</span>`;
      command.onclick = () => insertNode(item);
      groupEl.appendChild(command);
    }
    root.appendChild(groupEl);
  }
}

function insertNode(template) {
  if (!state.workflow) return;
  if (!flushPendingEdits({ silent: false })) return;
  const id = uniqueNodeId(template.type);
  const node = {
    id,
    type: template.type,
    title: template.title,
    indent: Number(currentNode()?.indent || 0),
    params: clone(template.params)
  };
  const insertAt = Math.min(state.workflow.nodes.length, state.selectedIndex + 1);
  state.workflow.nodes.splice(insertAt, 0, node);
  state.selectedIndex = insertAt;
  renderWorkflow();
  setStatus(`已插入节点：${template.title}`);
}

function uniqueNodeId(type) {
  const prefix = type.replace(".", "-");
  const existing = new Set((state.workflow?.nodes || []).map((node) => node.id));
  let index = existing.size + 1;
  let id = `${prefix}-${index}`;
  while (existing.has(id)) {
    index += 1;
    id = `${prefix}-${index}`;
  }
  return id;
}

async function loadWorkflows() {
  const data = await api("/api/workflows");
  const list = $("workflowList");
  list.innerHTML = "";
  data.workflows.forEach((name) => {
    const button = document.createElement("button");
    button.className = `workflow-chip${name === state.workflowName ? " active" : ""}`;
    button.textContent = name;
    button.onclick = () => loadWorkflow(name);
    list.appendChild(button);
  });
  if (!state.workflowName && data.workflows.length) await loadWorkflow(data.workflows[0]);
}

async function loadWorkflow(name) {
  if (!flushPendingEdits({ silent: true })) return;
  const data = await api(`/api/workflow?name=${encodeURIComponent(name)}`);
  state.workflowName = name;
  state.workflow = JSON.parse(data.content);
  state.selectedIndex = 0;
  state.elements = deriveElementsFromWorkflow();
  state.selectedElementId = state.elements[0]?.id || "";
  renderWorkflow();
  await loadWorkflows();
  setStatus(`${name} 已加载`);
}

async function loadSettings() {
  const settings = await api("/api/settings");
  maybeReportMimoError(settings.mimoError || "");
  if (settings.mimoHealthy) {
    setStatus("Mimo 已连接");
  } else if (settings.mimoConfigured) {
    setStatus("Mimo 检查失败，请联系管理员");
  } else {
    setStatus("Mimo 未配置，本地规则可运行");
  }
}

async function applyNode(event) {
  event.preventDefault();
  if (!commitPropertyDraft({ silent: false })) return;
  renderWorkflow();
  await persistWorkflow(`已更新并保存节点：${currentNode()?.title || currentNode()?.id || "节点"}`);
}

function moveSelectedNode(direction) {
  if (!state.workflow) return;
  if (!flushPendingEdits({ silent: false })) return;
  const nextIndex = state.selectedIndex + direction;
  if (nextIndex < 0 || nextIndex >= state.workflow.nodes.length) return;
  const nodes = state.workflow.nodes;
  [nodes[state.selectedIndex], nodes[nextIndex]] = [nodes[nextIndex], nodes[state.selectedIndex]];
  state.selectedIndex = nextIndex;
  renderWorkflow();
  setStatus(direction < 0 ? "节点已上移" : "节点已下移");
}

function duplicateSelectedNode() {
  const node = currentNode();
  if (!node || !state.workflow) return;
  if (!flushPendingEdits({ silent: false })) return;
  const copy = clone(node);
  copy.id = uniqueNodeId(copy.type);
  copy.title = `${copy.title || copy.id} 副本`;
  state.workflow.nodes.splice(state.selectedIndex + 1, 0, copy);
  state.selectedIndex += 1;
  renderWorkflow();
  setStatus(`已复制节点：${copy.title}`);
}

function deleteSelectedNode() {
  if (!state.workflow || !state.workflow.nodes.length) return;
  if (!flushPendingEdits({ silent: false })) return;
  const [removed] = state.workflow.nodes.splice(state.selectedIndex, 1);
  state.selectedIndex = Math.max(0, Math.min(state.selectedIndex, state.workflow.nodes.length - 1));
  renderWorkflow();
  setStatus(`已删除节点：${removed.title || removed.id}`);
}

async function toggleSelectedNodeDisabled() {
  const node = currentNode();
  if (!node) return;
  if (!flushPendingEdits({ silent: false })) return;
  node.disabled = !node.disabled;
  renderWorkflow();
  try {
    await persistWorkflow(`${node.disabled ? "已禁用" : "已启用"}节点：${node.title || node.id}`);
  } catch (error) {
    setStatus(error.error || "保存禁用状态失败");
  }
}

function deriveElementsFromWorkflow() {
  const elements = [];
  const seen = new Set();
  for (const node of state.workflow?.nodes || []) {
    const params = node.params || {};
    if (!params.selector && !params.target) continue;
    const name = String(params.target || node.title || node.id);
    const selector = String(params.selector || "");
    const key = `${name}::${selector}`;
    if (seen.has(key)) continue;
    seen.add(key);
    elements.push({
      id: `element-${elements.length + 1}`,
      name,
      selector,
      protected: isCaptchaLike(name)
    });
  }
  if (!elements.some((item) => item.protected)) {
    elements.push({
      id: "captcha-guarded",
      name: "验证码区域（受保护）",
      selector: "",
      protected: true
    });
  }
  return elements;
}

function renderElements() {
  const list = $("elementList");
  if (!list) return;
  list.innerHTML = "";
  const folder = document.createElement("div");
  folder.className = "element-row folder";
  folder.innerHTML = `<span>▸</span><span>大模型知识引擎</span>`;
  list.appendChild(folder);
  for (const element of state.elements) {
    const row = document.createElement("div");
    row.className = `element-row${element.id === state.selectedElementId ? " active" : ""}${element.protected ? " guarded" : ""}`;
    row.onclick = () => selectElement(element.id);
    row.innerHTML = `
      <span>${element.protected ? "!" : "e"}</span>
      <span>${escapeHtml(element.name)}</span>
      <span class="element-meta">${escapeHtml(shorten(element.selector || "无 selector", 58))}</span>`;
    list.appendChild(row);
  }
}

function selectElement(id) {
  state.selectedElementId = id;
  renderElements();
  const element = state.elements.find((item) => item.id === id);
  if (element) setStatus(`已选中元素：${element.name}`);
}

function captureElementFromNode() {
  const node = currentNode();
  if (!node) return;
  if (!flushPendingEdits({ silent: false })) return;
  const params = node.params || {};
  const name = String(params.target || node.title || node.id);
  const selector = String(params.selector || "");
  const element = {
    id: `manual-element-${Date.now()}`,
    name,
    selector,
    protected: isCaptchaLike(name)
  };
  state.elements.push(element);
  state.selectedElementId = element.id;
  renderElements();
  setStatus(`已从当前节点生成元素：${name}`);
}

function deleteSelectedElement() {
  const element = state.elements.find((item) => item.id === state.selectedElementId);
  if (!element) return;
  const references = (state.workflow?.nodes || []).filter((node) => {
    const params = node.params || {};
    return params.target === element.name || (element.selector && params.selector === element.selector);
  });
  if (references.length) {
    alert(`元素被 ${references.length} 个节点引用，不能删除。请先调整这些节点：${references.map((node) => node.title || node.id).join("、")}`);
    return;
  }
  state.elements = state.elements.filter((item) => item.id !== element.id);
  state.selectedElementId = state.elements[0]?.id || "";
  renderElements();
  setStatus(`已删除元素：${element.name}`);
}

function addAgentMessage(role, text) {
  const root = $("agentMessages");
  const item = document.createElement("div");
  item.className = `agent-message ${role}`;
  item.textContent = text;
  root.appendChild(item);
  root.scrollTop = root.scrollHeight;
}

function handleAgentSubmit(event) {
  event.preventDefault();
  handleAgentText();
}

async function handleAgentText() {
  const input = $("agentInput");
  let text = input.value.trim();
  if (!text && state.agentImage) {
    text = "请根据这张图片识别网页里的目标元素位置，并帮我生成或修改对应 workflow 节点。";
  }
  if (!text) return;
  const image = state.agentImage;
  input.value = "";
  clearAgentImage();
  addAgentMessage("user", image ? `${text}\n[图片附件：${image.name}]` : text);
  setStatus("Mimo 正在判断 workflow 修改");
  state.mimoErrorReported = false;
  try {
    if (!flushPendingEdits({ silent: false })) return;
    const result = await api("/api/agent", {
      method: "POST",
      body: JSON.stringify({
        message: text,
        workflow: state.workflow,
        selectedIndex: state.selectedIndex,
        elements: state.elements,
        imageDataUrl: image?.dataUrl || "",
        imageName: image?.name || ""
      })
    });
    state.workflow = result.workflow;
    state.selectedIndex = Number(result.selectedIndex || 0);
    state.elements = deriveElementsFromWorkflow();
    state.selectedElementId = state.elements[0]?.id || "";
    renderWorkflow();
    const source = result.source === "mimo" ? "Mimo" : "本地规则";
    const errorNote = result.modelError ? `（模型失败：${result.modelError}）` : "";
    addAgentMessage("agent", `${source}：${result.reply}${errorNote}`);
    maybeReportMimoError(result.modelError);
    setStatus(`${source} 已应用：${result.action}`);
  } catch (error) {
    addAgentMessage("agent", `Agent 执行失败：${error.error || error.message || "未知错误"}`);
    maybeReportMimoError(error.error || error.message || "");
    setStatus(error.error || "Agent 执行失败");
  }
}

function handleAgentImageChange(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  if (!/^image\/(png|jpeg|webp)$/.test(file.type)) {
    setStatus("只支持 PNG、JPEG、WebP 图片");
    event.target.value = "";
    return;
  }
  if (file.size > 5 * 1024 * 1024) {
    setStatus("图片不能超过 5MB");
    event.target.value = "";
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    state.agentImage = {
      name: file.name,
      dataUrl: String(reader.result || "")
    };
    renderAgentImagePreview();
    setStatus(`已添加图片：${file.name}`);
  };
  reader.readAsDataURL(file);
}

function renderAgentImagePreview() {
  const root = $("agentImagePreview");
  if (!root) return;
  if (!state.agentImage) {
    root.classList.add("hidden");
    root.innerHTML = "";
    return;
  }
  root.classList.remove("hidden");
  root.innerHTML = `
    <img src="${state.agentImage.dataUrl}" alt="agent attachment" />
    <span>${escapeHtml(state.agentImage.name)}</span>
    <button id="clearAgentImageBtn" class="mini-button" type="button">移除</button>`;
  $("clearAgentImageBtn").onclick = clearAgentImage;
}

function clearAgentImage() {
  state.agentImage = null;
  const input = $("agentImageInput");
  if (input) input.value = "";
  renderAgentImagePreview();
}

async function saveWorkflow() {
  setBusy(true);
  try {
    if (!flushPendingEdits({ silent: false })) return;
    await persistWorkflow();
  } catch (error) {
    setStatus(error.error || "保存失败");
  } finally {
    setBusy(false);
  }
}

async function persistWorkflow(successMessage = "") {
  const result = await api(`/api/workflow?name=${encodeURIComponent(state.workflowName)}`, {
    method: "POST",
    body: JSON.stringify({ content: JSON.stringify(state.workflow, null, 2) })
  });
  renderWorkflow();
  setStatus(successMessage || result.message);
  return result;
}

async function validateWorkflow() {
  setBusy(true);
  try {
    if (!flushPendingEdits({ silent: false })) return;
    const result = await api("/api/validate", {
      method: "POST",
      body: JSON.stringify({ content: JSON.stringify(state.workflow, null, 2) })
    });
    setStatus(`校验通过：${result.message}`);
  } catch (error) {
    setStatus(error.error || "校验失败");
  } finally {
    setBusy(false);
  }
}

async function runWorkflow() {
  if (state.busy && state.runAbortController) {
    state.runAbortController.abort();
    appendLogStep({
      node_id: "runner",
      title: "重新开始运行",
      status: "pass",
      detail: "已停止上一轮前端监听，准备从第 1 个节点重新执行。"
    });
  }
  const runId = Date.now();
  state.activeRunId = runId;
  state.mimoErrorReported = false;
  const controller = new AbortController();
  state.runAbortController = controller;
  setBusy(true);
  try {
    if (!flushPendingEdits({ silent: false })) return;
    clearLogs();
    setStatus(`运行中：${state.workflowName}`);
    await persistWorkflow();
    await runWorkflowStream({
      name: state.workflowName,
      content: JSON.stringify(state.workflow),
      headless: false
    }, controller.signal, runId);
  } catch (error) {
    if (error.name === "AbortError" && state.activeRunId !== runId) return;
    appendLogStep({
      node_id: "runner-error",
      title: "运行失败",
      status: "fail",
      detail: error.error || error.message || "未知错误"
    });
    activateDockTab("logs");
    setStatus(error.error || `运行失败：${error.status || "fail"}`);
  } finally {
    if (state.activeRunId === runId) {
      state.runAbortController = null;
      setBusy(false);
    }
  }
}

async function runWorkflowStream(payload, signal, runId) {
  const response = await fetch("/api/run_stream", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
    signal
  });
  if (!response.ok) {
    const errorPayload = await response.json().catch(() => ({ error: response.statusText }));
    throw errorPayload;
  }
  if (!response.body) {
    const result = await api("/api/run", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    renderLogs(result);
    setStatus(`运行完成：${result.status}`);
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split(/\n\n/);
    buffer = blocks.pop() || "";
    for (const block of blocks) handleSseBlock(block, runId);
  }
  buffer += decoder.decode();
  if (buffer.trim()) handleSseBlock(buffer, runId);
}

function clearLogs() {
  state.logSteps = [];
  $("logsTab").innerHTML = "";
  const floatingBody = $("floatingLogBody");
  if (floatingBody) floatingBody.innerHTML = "";
}

function renderLogs(result) {
  state.logSteps = [];
  const root = $("logsTab");
  root.innerHTML = "";
  const floatingBody = $("floatingLogBody");
  if (floatingBody) floatingBody.innerHTML = "";
  if (result.workflow) {
    state.workflow = result.workflow;
    renderWorkflow();
  }
  for (const step of result.steps || []) {
    appendLogStep(step, false);
  }
  activateDockTab("logs");
}

function appendLogStep(step, keepDock = true) {
  state.logSteps.push(step);
  const bottom = $("logsTab");
  const floatingBody = $("floatingLogBody");
  const bottomItem = createLogItem(step);
  bottom.appendChild(bottomItem);
  bottom.scrollTop = bottom.scrollHeight;
  if (floatingBody) {
    floatingBody.appendChild(createLogItem(step));
    floatingBody.scrollTop = floatingBody.scrollHeight;
  }
  if (keepDock) activateDockTab("logs");
  if (step.status === "fail") maybeReportMimoError(step.detail || "");
  maybeSurfaceRepairQuestion(step);
}

function createLogItem(step) {
  const item = document.createElement("div");
  item.className = `log-item ${step.status || ""}`;
  item.dataset.copyText = formatLogStep(step);
  item.innerHTML = `
    <div class="log-title">${escapeHtml(step.title || step.node_id || "运行日志")}</div>
    <div class="log-detail">${escapeHtml(step.detail || "")}</div>`;
  return item;
}

function formatLogStep(step) {
  const status = step.status ? `[${step.status}] ` : "";
  const title = step.title || step.node_id || "运行日志";
  const id = step.node_id && step.node_id !== title ? ` (${step.node_id})` : "";
  const detail = step.detail ? `\n${step.detail}` : "";
  return `${status}${title}${id}${detail}`;
}

function currentLogText() {
  if (state.logSteps.length) return state.logSteps.map(formatLogStep).join("\n\n");
  return Array.from(document.querySelectorAll("#logsTab .log-item"))
    .map((item) => item.dataset.copyText || item.textContent.trim())
    .filter(Boolean)
    .join("\n\n");
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

async function copyLogs() {
  const text = currentLogText();
  if (!text) {
    setStatus("暂无日志可复制");
    return;
  }
  try {
    await copyText(text);
    setStatus("日志已复制");
  } catch (error) {
    setStatus(`日志复制失败：${error.message || "请手动选择复制"}`);
  }
}

function handleSseBlock(block, runId) {
  if (state.activeRunId !== runId) return;
  const eventLine = block.split("\n").find((line) => line.startsWith("event:"));
  const event = eventLine ? eventLine.slice(6).trim() : "message";
  const data = block
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data) return;
  const payload = JSON.parse(data);
  if (event === "start") {
    const disabledPreview = Array.isArray(payload.disabledNodes) && payload.disabledNodes.length
      ? `，跳过 ${payload.disabledNodeCount} 个禁用节点：${payload.disabledNodes.slice(0, 5).join("、")}${payload.disabledNodes.length > 5 ? "..." : ""}`
      : "，无禁用节点";
    appendLogStep({
      node_id: "runner-start",
      title: "开始执行 workflow",
      status: "pass",
      detail: `${payload.name || state.workflowName} / ${payload.nodeCount || 0} 个节点${disabledPreview}`
    });
    setStatus(`正在执行：${payload.name || state.workflowName}`);
  } else if (event === "step") {
    appendLogStep(payload);
    setStatus(`正在执行：${payload.title || payload.node_id || "下一步"}`);
  } else if (event === "result") {
    if (payload.workflow) {
      state.workflow = payload.workflow;
      renderWorkflow();
    }
    setStatus(`运行完成：${payload.status}`);
    if (payload.status !== "pass") {
      appendLogStep({
        node_id: "runner-result",
        title: "运行中断",
        status: "fail",
        detail: "workflow 返回失败，请查看上一条失败节点。"
      });
    }
  } else if (event === "error") {
    appendLogStep({
      node_id: "runner-error",
      title: "运行异常",
      status: "fail",
      detail: payload.error || "未知错误"
    });
    setStatus(payload.error || "运行异常");
  }
}

function maybeSurfaceRepairQuestion(step) {
  const detail = String(step.detail || "");
  if (!String(step.node_id || "").endsWith("-ai-repair")) return;
  if (!/AI 不确定|等待用户确认|question=/.test(detail)) return;
  const question = extractRepairQuestion(detail);
  if (!question || question === state.lastRepairQuestion) return;
  state.lastRepairQuestion = question;
  const message = `AI 修复需要你确认：${question}`;
  addAgentMessage("agent", message);
  const input = $("agentInput");
  if (input && !input.value.trim()) {
    input.value = `请根据这个修复问题修改 workflow：${question}`;
    input.focus();
  }
  activateDockTab("agent");
  setStatus("AI 修复需要用户确认，请在 Agent 聊天中回复");
}

function extractRepairQuestion(detail) {
  const marker = "question=";
  const index = detail.indexOf(marker);
  if (index >= 0) return detail.slice(index + marker.length).trim();
  return detail.trim();
}

function activateDockTab(name) {
  for (const button of document.querySelectorAll(".dock-tab")) {
    button.classList.toggle("active", button.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll(".dock-content")) {
    panel.classList.remove("active");
  }
  $(`${name}Tab`).classList.add("active");
}

function formatJson() {
  try {
    if (!commitJsonDraft({ silent: false })) return;
    state.elements = deriveElementsFromWorkflow();
    renderWorkflow();
    setStatus("JSON 已格式化");
  } catch (error) {
    setStatus(`JSON 格式错误：${error.message}`);
  }
}

function shorten(value, max) {
  const text = String(value);
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function clone(value) {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

function commitJsonDraft({ silent = false } = {}) {
  if (!state.jsonDirty) return true;
  try {
    syncWorkflowFromJson();
    state.selectedIndex = Math.max(0, Math.min(state.selectedIndex, (state.workflow?.nodes?.length || 1) - 1));
    state.elements = deriveElementsFromWorkflow();
    state.selectedElementId = state.elements[0]?.id || "";
    return true;
  } catch (error) {
    if (!silent) setStatus(`JSON 格式错误：${error.message}`);
    return false;
  }
}

function commitPropertyDraft({ silent = false } = {}) {
  if (!state.workflow || !state.propertyDirty) return true;
  const node = currentNode();
  if (!node) return true;
  try {
    node.id = $("nodeId").value.trim();
    node.title = $("nodeTitle").value.trim() || node.id;
    node.type = $("nodeType").value;
    node.indent = Math.max(0, Number($("nodeIndent").value || 0));
    node.disabled = $("nodeDisabled").checked;
    node.params = JSON.parse($("nodeParams").value || "{}");
    state.propertyDirty = false;
    state.elements = deriveElementsFromWorkflow();
    if (!state.elements.some((item) => item.id === state.selectedElementId)) {
      state.selectedElementId = state.elements[0]?.id || "";
    }
    return true;
  } catch (error) {
    if (!silent) setStatus(`节点参数不是合法 JSON：${error.message}`);
    return false;
  }
}

function flushPendingEdits({ silent = false } = {}) {
  if (!commitJsonDraft({ silent })) return false;
  return commitPropertyDraft({ silent });
}

function isCaptchaLike(value) {
  return /验证码|校验码|图形码|captcha|verify|vcode/i.test(String(value || ""));
}

function commandMatches(text, item) {
  const normalized = text.toLowerCase();
  const title = item.title.replace(/\(.+\)/g, "");
  if (text.includes(item.title) || text.includes(title) || normalized.includes(item.type)) return true;
  if (item.type === "web.click" && /点击|按钮|单击/.test(text)) return true;
  if (item.type === "web.hover" && /hover|悬停|鼠标移入|鼠标移动/.test(normalized)) return true;
  if (item.type === "web.input" && /输入|填写|填入/.test(text)) return true;
  if (item.type === "web.open" && /打开|访问|网址/.test(text)) return true;
  if (item.type === "web.scroll" && /滚动|scroll|到底|顶部|列表/.test(normalized)) return true;
  if (item.type === "web.select" && /下拉|选择|option/.test(normalized)) return true;
  if (item.type === "web.close_modals" && /弹窗|modal|关闭/.test(normalized)) return true;
  if (item.type === "flow.wait" && /等待|暂停/.test(text)) return true;
  if (item.type === "flow.if" && /if|判断|如果|条件/.test(normalized)) return true;
  if (item.type === "flow.loop" && /loop|循环|重复/.test(normalized)) return true;
  if (item.type === "ai.ask" && /ai|大模型|判断|生成/.test(normalized)) return true;
  return false;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[char]));
}

function setupFloatingLogPanel() {
  const panel = $("floatingLog");
  const head = $("floatingLogHead");
  const toggle = $("logMinimizeBtn");
  const copyButton = $("copyFloatingLogsBtn");
  if (!panel || !head || !toggle) return;

  if (copyButton) {
    copyButton.onclick = (event) => {
      event.stopPropagation();
      copyLogs();
    };
  }

  toggle.onclick = (event) => {
    event.stopPropagation();
    const collapsed = panel.classList.toggle("collapsed");
    toggle.textContent = collapsed ? "展开" : "收起";
  };

  let dragging = false;
  let offsetX = 0;
  let offsetY = 0;
  head.addEventListener("pointerdown", (event) => {
    if (event.target.closest("button")) return;
    dragging = true;
    const rect = panel.getBoundingClientRect();
    offsetX = event.clientX - rect.left;
    offsetY = event.clientY - rect.top;
    panel.style.left = `${rect.left}px`;
    panel.style.top = `${rect.top}px`;
    panel.style.right = "auto";
    panel.style.bottom = "auto";
    head.setPointerCapture(event.pointerId);
  });
  head.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const width = panel.offsetWidth;
    const height = panel.offsetHeight;
    const nextLeft = Math.min(Math.max(8, event.clientX - offsetX), window.innerWidth - width - 8);
    const nextTop = Math.min(Math.max(8, event.clientY - offsetY), window.innerHeight - height - 8);
    panel.style.left = `${nextLeft}px`;
    panel.style.top = `${nextTop}px`;
  });
  head.addEventListener("pointerup", (event) => {
    dragging = false;
    try {
      head.releasePointerCapture(event.pointerId);
    } catch {
      // Pointer capture can already be released when the window loses focus.
    }
  });
}

function init() {
  const typeSelect = $("nodeType");
  typeSelect.innerHTML = NODE_TYPES.map((type) => `<option value="${type}">${type}</option>`).join("");
  setupFloatingLogPanel();
  $("commandSearch").addEventListener("input", renderCommands);
  for (const button of document.querySelectorAll("[data-command-mode]")) {
    button.onclick = () => {
      state.commandMode = button.dataset.commandMode;
      for (const peer of document.querySelectorAll("[data-command-mode]")) {
        peer.classList.toggle("active", peer === button);
      }
      renderCommands();
    };
  }
  $("nodeForm").addEventListener("submit", applyNode);
  for (const id of ["nodeId", "nodeTitle", "nodeType", "nodeIndent", "nodeParams"]) {
    $(id).addEventListener("input", () => {
      state.propertyDirty = true;
    });
    $(id).addEventListener("change", () => {
      state.propertyDirty = true;
    });
  }
  $("nodeDisabled").addEventListener("change", () => {
    state.propertyDirty = true;
    syncSelectedNodeDisabledFromProperty();
    persistWorkflow(`已${$("nodeDisabled").checked ? "禁用" : "启用"}节点：${currentNode()?.title || currentNode()?.id || ""}`).catch((error) => {
      setStatus(error.error || "保存禁用状态失败");
    });
  });
  $("jsonEditor").addEventListener("input", () => {
    state.jsonDirty = true;
  });
  $("agentForm").addEventListener("submit", handleAgentSubmit);
  $("agentSendBtn").onclick = handleAgentSubmit;
  $("agentImageBtn").onclick = () => $("agentImageInput").click();
  $("agentImageInput").addEventListener("change", handleAgentImageChange);
  $("agentInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      handleAgentText();
    }
  });
  $("addNodeBtn").onclick = () => insertNode(COMMAND_GROUPS[0].items[1]);
  $("saveBtn").onclick = saveWorkflow;
  $("validateBtn").onclick = validateWorkflow;
  $("runBtn").onclick = runWorkflow;
  $("reloadBtn").onclick = async () => {
    if (!state.workflowName) return;
    if (!flushPendingEdits({ silent: false })) return;
    await loadWorkflow(state.workflowName);
  };
  $("formatBtn").onclick = formatJson;
  $("moveUpBtn").onclick = () => moveSelectedNode(-1);
  $("moveDownBtn").onclick = () => moveSelectedNode(1);
  $("toggleDisableNodeBtn").onclick = toggleSelectedNodeDisabled;
  $("duplicateNodeBtn").onclick = duplicateSelectedNode;
  $("deleteNodeBtn").onclick = deleteSelectedNode;
  $("captureElementBtn").onclick = captureElementFromNode;
  $("deleteElementBtn").onclick = deleteSelectedElement;
  $("undoBtn").onclick = () => setStatus("撤销栈将在下一步接入；当前可通过重载恢复已保存版本");
  $("redoBtn").onclick = () => setStatus("重做栈将在下一步接入；当前可通过 JSON 面板手动恢复");
  $("recordBtn").onclick = () => setStatus("流程录制入口已保留，后续会接 Playwright recorder");
  $("dataBtn").onclick = () => setStatus("数据抓取入口已保留，后续会生成抽取节点");
  $("browserBtn").onclick = () => runWorkflow();
  $("debugBtn").onclick = validateWorkflow;
  $("copyLogsBtn").onclick = copyLogs;
  for (const button of document.querySelectorAll(".dock-tab")) {
    button.onclick = () => activateDockTab(button.dataset.tab);
  }
  addAgentMessage("agent", "我会把你的指令交给 Mimo 判断，并把结果应用为具体 workflow 节点。");
  renderCommands();
  state.mimoErrorReported = false;
  loadSettings().catch((error) => {
    maybeReportMimoError(error.error || error.message || "");
    setStatus(error.error || "设置加载失败");
  });
  loadWorkflows().catch((error) => setStatus(error.error || "workflow 加载失败"));
}

init();
