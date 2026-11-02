"use strict";
const token = document.querySelector('meta[name="csrf-token"]').content;
const messages = document.getElementById("messages");
const status = document.getElementById("status");
const input = document.getElementById("message");
const sessionInput = document.getElementById("session");
const modelInput = document.getElementById("model");
const backendInput = document.getElementById("backend");
const backendNote = document.getElementById("backend-note");
const agentStatusPanel = document.getElementById("agent-status-panel");
const agentStatusFeedback = document.getElementById("agent-status-feedback");
const agentStatusRefresh = document.getElementById("agent-status-refresh");
let agentStatusTimer = null;
let agentStatusRequest = null;
let agentStatusRevision = 0;
let agentStatusQueued = false;
let agentStatusValue = null;
let agentStatusAuthFailed = false;
const viewNames = ["chat", "news", "repositories", "papers"];
let currentView = "chat";
const newsStatus = document.getElementById("news-status");
const papersStatus = document.getElementById("papers-status");
const newsSourceIds = new Set(["aibot", "aiagentstore", "aihot"]);
const allowedModels = new Set(Array.from(modelInput.options, option => option.value));
const shared = document.body.dataset.shared === "true";
const storageKey = shared ? "joyagent.visitor.session" : "joyagent.session";
const modelStorageKey = shared ? "joyagent.visitor.model" : "joyagent.model";
const autoMigrationKey = modelStorageKey + ".auto-routing-v1";
const backendStoragePrefix = shared ? "joyagent.visitor.backend." : "joyagent.backend.";
const knownBackendIds = new Set(["joyagent", "codex", "pi", "deepseek"]);
const configuredBackends = new Set(Array.from(backendInput.options, option => option.value));
const configuredBackendLabels = new Map(Array.from(backendInput.options, option => [option.value, option.textContent]));
let defaultBackend = backendInput.value;
const backendSelections = new Map();
let backends = [];
let backendsReady = false;
let backendError = "";
let activeBackend = "";
let busyMode = "";
let session = "default";
let busy = false;
let navigationBusy = false;
let navigationMode = "";
let historyTarget = null;
let conversation = null;
let historyReady = false;
let conversations = [];
let conversationsLoading = false;
let conversationsQueued = false;
let conversationRevision = 0;
let historyRevision = 0;
const deletedConversations = new Set();
const pendingDeletions = new Map();
const pendingRenames = new Set();
const renamedTitles = new Map();
let titleGeneration = 0;
const conversationButtons = [];
const drafts = new Map();
const draftRevisions = new Map();
const sessionRuns = new Map();
const recoveredDrafts = new Map();
let displayedMessages = [];
const conversationState = document.getElementById("conversation-status");
try {
  if (shared) {
    // Old account-scoped preferences included usernames in their key names.
    const obsolete = [];
    for (let index = 0; index < localStorage.length; index++) {
      const key = localStorage.key(index);
      if (key?.startsWith("joyagent.session.")) obsolete.push(key);
    }
    obsolete.forEach(key => localStorage.removeItem(key));
  }
  const saved = localStorage.getItem(storageKey);
  if (saved !== null) {
    session = saved || "default";
  } else if (!shared) {
    const legacy = localStorage.getItem("jyagent.session");
    if (legacy) {
      session = legacy;
      localStorage.setItem("joyagent.session", legacy);
      // Remove the legacy key only after the replacement was saved successfully.
      localStorage.removeItem("jyagent.session");
    }
  }
} catch (_) {}
try {
  if (allowedModels.has("auto") && localStorage.getItem(autoMigrationKey) !== "1") {
    // Earlier clients persisted the default Flash as if it were a manual
    // choice. Migrate once; every subsequent explicit choice remains intact.
    modelInput.value = "auto";
    localStorage.setItem(modelStorageKey, "auto");
    localStorage.setItem(autoMigrationKey, "1");
  } else {
    const savedModel = localStorage.getItem(modelStorageKey);
    if (allowedModels.has(savedModel)) modelInput.value = savedModel;
    else if (savedModel !== null) localStorage.removeItem(modelStorageKey);
  }
} catch (_) {}
sessionInput.value = session;
let preferredModel = modelInput.value;
function setStatus(text, error = false) { status.textContent = text; status.className = error ? "status error" : "status"; }
function running(selected = session) { return sessionRuns.get(selected)?.pending === true; }
function updateControls() {
  busy = navigationBusy || running();
  busyMode = navigationBusy ? navigationMode : running() ? "chat" : "";
  activeBackend = running() ? sessionRuns.get(session).backend : "";
  updateConversationControls(); updateBackendControls();
}
function setBusy(value, mode = "conversation") {
  const previousMode = navigationMode;
  historyTarget = null;
  navigationBusy = value; navigationMode = value ? mode : ""; updateControls();
  trimCompletedViews();
  if (!value && conversationsQueued) { conversationsQueued = false; loadConversations(); }
  if (!value && previousMode === "conversation") refreshAgentStatus();
}
function trimCompletedViews() {
  const completed = [...sessionRuns].filter(([id, run]) => !run.pending && run.messages && id !== session && id !== historyTarget);
  for (const [, run] of completed.slice(0, -8)) run.messages = null;
}
function restoreRunStatus() {
  const run = sessionRuns.get(session);
  setStatus(conversation?.archived ? "已归档 · 恢复后可继续对话" : run?.status || `会话 ${conversation?.title || session} · 就绪`, !conversation?.archived && run?.error === true);
}
function renderMessages(records) {
  displayedMessages = records;
  welcome();
  records.forEach(message => append(message.role, message.content, message.tool_events || [], message.backend_label || backendLabel(message.backend || "joyagent"), message.routing));
}
function updateConversationControls() {
  const readonly = conversation?.archived === true;
  const run = sessionRuns.get(session);
  const send = document.getElementById("send");
  send.disabled = navigationBusy || readonly || !historyReady || run?.steerSending === true || running() && run.steeringClosed === true;
  send.textContent = running() ? "补充指令" : "发送 ↗";
  input.disabled = navigationBusy || readonly || !historyReady;
  input.placeholder = running() ? "补充要求或调整方向…" : "说说你想完成什么…";
  document.getElementById("switch").disabled = navigationBusy;
  sessionInput.disabled = navigationBusy;
  modelInput.disabled = busy || readonly;
  document.getElementById("conversation-new").disabled = navigationBusy;
  document.getElementById("conversation-retry").disabled = navigationBusy || conversationsLoading;
  document.getElementById("conversation-restore").disabled = busy;
  document.getElementById("archive-banner").hidden = !readonly;
  document.getElementById("conversation-title").textContent = conversation?.title || (session ? "新会话" : "未选择会话");
  document.getElementById("conversation-identity").hidden = !historyReady || !session;
  document.getElementById("conversation-id").textContent = historyReady ? session : "";
  conversationButtons.forEach(button => { button.disabled = navigationBusy || button.deleted === true
    || button.mutatesSession && (running(button.session) || pendingRenames.has(button.session))
    || button.renamesSession === true && pendingRenames.has(button.session); });
  renderSteering();
}
function renderSteering() {
  const target = document.getElementById("steering-feedback");
  const run = sessionRuns.get(session);
  target.replaceChildren();
  if (run?.pending) {
    const help = document.createElement("p");
    help.textContent = run.delivery === "next_step" ? "执行中可补充要求，在下一安全步骤采用；已执行的操作不会撤销。"
      : "执行中可补充要求，当前 Agent 轮次结束后在本会话自动续接；已执行的操作不会撤销。";
    target.append(help);
  }
  if (run?.steerNotice) {
    const notice = document.createElement("p"); notice.className = run.steerError ? "error" : "";
    notice.textContent = run.steerNotice; target.append(notice);
  }
  for (const item of run?.steering?.values() || []) {
    const row = document.createElement("p");
    const state = { sending: "正在提交", queued: "待应用", applied: "已应用", unapplied: "未应用", unknown: run.outcomeUnknown ? "执行结果待确认" : "提交结果待确认", rejected: "未接收" }[item.status];
    row.className = ["unapplied", "unknown", "rejected"].includes(item.status) ? "error" : "";
    row.textContent = `${state || "待确认"} · ${item.message}`; target.append(row);
  }
  for (const [id, text] of recoveredDrafts) {
    const row = document.createElement("p"); row.textContent = `已删除会话的未发送草稿：${text}`;
    const restore = document.createElement("button"); restore.type = "button"; restore.textContent = "放入输入框";
    restore.disabled = navigationBusy || !historyReady || conversation?.archived === true;
    restore.addEventListener("click", () => {
      if (restore.disabled) return;
      input.value = input.value ? `${input.value}\n${text}` : text;
      drafts.set(session, input.value); draftRevisions.set(session, (draftRevisions.get(session) || 0) + 1);
      recoveredDrafts.delete(id); renderSteering(); input.focus();
    });
    row.append(restore); target.append(row);
  }
  target.hidden = target.children.length === 0;
}
function currentDraft(selected) { return session === selected ? input.value : drafts.get(selected) || ""; }
function clearSubmittedDraft(selected, text, revision) {
  if (currentDraft(selected) !== text || (draftRevisions.get(selected) || 0) !== revision) return;
  drafts.set(selected, "");
  if (session === selected) input.value = "";
}
function rememberLostDraft(selected) {
  const text = currentDraft(selected);
  if (text.trim()) recoveredDrafts.set(selected, text);
}
function conversationRow(value) {
  if (!value || typeof value.session_id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,123}$/.test(value.session_id)
      || typeof value.title !== "string" || typeof value.archived !== "boolean") throw new Error("会话数据无效，请重试");
  return value;
}
function conversationUpdatedAfter(row, previous) {
  // The server emits UTC ISO timestamps with fixed microsecond precision.
  // Compare the full values: Date.parse would discard sub-millisecond order.
  return typeof row.updated_at === "string" && typeof previous.updated_at === "string" && row.updated_at > previous.updated_at;
}
function currentConversationTitle(row, requestedGeneration) {
  const renamed = renamedTitles.get(row.session_id);
  // A read begun before a successful rename may still contain the old name.
  return renamed && renamed.generation > requestedGeneration && !conversationUpdatedAfter(row, renamed)
    ? { ...row, title: renamed.title, updated_at: renamed.updated_at } : row;
}
function renderConversations() {
  const target = document.getElementById("conversation-list");
  target.replaceChildren(); conversationButtons.length = 0;
  for (const archived of [false, true]) {
    const section = document.createElement("section"); section.className = "conversation-section";
    const heading = document.createElement("h2"); heading.textContent = archived ? "已归档" : "正在使用"; section.append(heading);
    const rows = conversations.filter(row => row.archived === archived);
    if (!rows.length) { const empty = document.createElement("p"); empty.className = "conversation-empty"; empty.textContent = archived ? "暂无归档会话" : "暂无已保存会话"; section.append(empty); }
    for (const row of rows) {
      const item = document.createElement("div"); item.className = `conversation-row${row.session_id === session ? " current" : ""}`;
      const open = document.createElement("button"); open.type = "button"; open.className = "conversation-open";
      open.title = `${row.title || "新会话"} · ${row.session_id}`;
      const title = document.createElement("span"); title.className = "conversation-row-title"; title.textContent = row.title || "新会话";
      const identity = document.createElement("span"); identity.className = "conversation-row-id"; identity.textContent = `ID: ${row.session_id}`;
      open.append(title, identity);
      const run = sessionRuns.get(row.session_id);
      if (run) {
        const state = document.createElement("span"); state.className = `conversation-run-state${run.error ? " error" : ""}`;
        state.textContent = run.pending ? "处理中" : run.error ? "未完成" : "已完成"; open.append(state);
      }
      open.setAttribute("aria-current", row.session_id === session ? "true" : "false");
      open.addEventListener("click", () => loadSession(row.session_id));
      open.deleted = deletedConversations.has(row.session_id);
      const action = document.createElement("button"); action.type = "button"; action.className = "conversation-action";
      action.textContent = archived ? "恢复" : "归档";
      action.setAttribute("aria-label", `${action.textContent}：${row.title || "新会话"}`);
      action.addEventListener("click", () => archiveConversation(row.session_id, !archived));
      action.deleted = deletedConversations.has(row.session_id);
      const rename = document.createElement("button"); rename.type = "button"; rename.className = "conversation-action conversation-rename";
      rename.textContent = pendingRenames.has(row.session_id) ? "保存中…" : "重命名";
      rename.setAttribute("aria-label", `重命名：${row.title || "新会话"}`);
      rename.addEventListener("click", () => renameConversation(row.session_id));
      rename.deleted = deletedConversations.has(row.session_id) || pendingDeletions.has(row.session_id);
      rename.renamesSession = true;
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "conversation-action conversation-delete";
      remove.textContent = pendingDeletions.has(row.session_id) ? "重试删除" : "删除";
      remove.setAttribute("aria-label", `${remove.textContent}：${row.title || "新会话"}`);
      remove.addEventListener("click", () => deleteConversation(row.session_id));
      for (const button of [open, action, remove, rename]) { button.session = row.session_id; button.mutatesSession = button !== open && button !== rename; }
      conversationButtons.push(open, rename, action, remove); item.append(open, rename, action, remove); section.append(item);
    }
    target.append(section);
  }
  updateConversationControls();
}
function rememberConversation(row) {
  if (deletedConversations.has(row.session_id)) return;
  const index = conversations.findIndex(item => item.session_id === row.session_id);
  if (index < 0) conversations.unshift(row); else conversations[index] = row;
  if (row.session_id === session) conversation = row;
  renderConversations();
}
async function loadConversations() {
  const revision = ++conversationRevision;
  const requestedGeneration = titleGeneration;
  conversationsLoading = true; updateConversationControls();
  conversationState.textContent = "正在读取会话列表…"; conversationState.className = "";
  try {
    const data = await Promise.all([api("/api/conversations?archived=false"), api("/api/conversations?archived=true")]);
    const rows = data.flatMap((page, index) => {
      if (!Array.isArray(page.conversations)) throw new Error("读取会话列表失败");
      return page.conversations.map(value => {
        const row = conversationRow(value);
        if (row.archived !== Boolean(index)) throw new Error("会话列表状态无效");
        return currentConversationTitle(row, requestedGeneration);
      });
    });
    if (revision !== conversationRevision) return false;
    conversations = rows.filter(row => !deletedConversations.has(row.session_id));
    for (const [id, run] of sessionRuns) {
      if (run.pending && !deletedConversations.has(id) && !conversations.some(row => row.session_id === id)) conversations.push(run.row);
    }
    for (const [id, pending] of pendingDeletions) {
      if (!conversations.some(row => row.session_id === id)) conversations.push(pending);
    }
    const current = conversations.find(row => row.session_id === session);
    if (current) conversation = current;
    renderConversations(); updateBackendControls();
    conversationState.textContent = "归档只收起会话，消息仍保留。"; return true;
  } catch (error) {
    if (revision === conversationRevision) { conversationState.textContent = `${error.message}，请重试。`; conversationState.className = "error"; }
    return false;
  } finally {
    if (revision === conversationRevision) { conversationsLoading = false; updateConversationControls(); }
  }
}
function displaySession(selected, data, requestedRun = data.requestedRun || null) {
  if (deletedConversations.has(selected)) throw Object.assign(new Error("此会话已永久删除"), { status: 410 });
  if (!Array.isArray(data.messages)) throw new Error("读取会话记录失败，请重试");
  const row = data.conversation ? currentConversationTitle(conversationRow(data.conversation), data.titleGeneration ?? titleGeneration) : null;
  if (row && row.session_id !== selected) throw new Error("会话编号不匹配，请重试");
  ++conversationRevision; conversationsLoading = false; // Discard a list requested before this session change.
  if (session && !deletedConversations.has(session)) drafts.set(session, input.value);
  session = selected; sessionInput.value = session; conversation = row; historyReady = true;
  input.value = drafts.get(session) || "";
  try { localStorage.setItem(storageKey, session); } catch (_) {}
  const run = sessionRuns.get(session);
  modelInput.value = run?.pending ? run.model : preferredModel; showModel();
  restoreBackend();
  // A history request made during a turn can race its persistence/HTTP reply.
  // Keep that turn's coherent local view until a later fresh history fetch.
  renderMessages(run && (run.pending || requestedRun === run) && run.messages ? run.messages : data.messages);
  if (run && !run.pending && requestedRun !== run) run.messages = null;
  if (row) rememberConversation(row); else renderConversations();
  updateControls(); restoreRunStatus();
}
async function createConversation() {
  if (navigationBusy) return;
  setBusy(true, "conversation"); setStatus("正在新建会话…");
  try { await createAndDisplayConversation(); await loadConversations(); }
  catch (error) { setStatus(error.message, true); }
  finally { setBusy(false); }
}
async function createAndDisplayConversation() {
  const data = await api("/api/conversations", { method: "POST", body: "{}" });
  const row = conversationRow(data.conversation);
  if (row.archived) throw new Error("新会话状态无效，请刷新列表");
  displaySession(row.session_id, { conversation: row, messages: [] });
}
function forgetConversation(selected, preserveNavigation = false) {
  if (running(selected)) rememberLostDraft(selected);
  deletedConversations.add(selected);
  if (!preserveNavigation) { ++conversationRevision; ++historyRevision; conversationsLoading = false; }
  drafts.delete(selected); backendSelections.delete(selected); sessionRuns.delete(selected); renamedTitles.delete(selected);
  try {
    localStorage.removeItem(backendStoragePrefix + selected);
    if (localStorage.getItem(storageKey) === selected) localStorage.removeItem(storageKey);
    if (!shared && localStorage.getItem("jyagent.session") === selected) localStorage.removeItem("jyagent.session");
  } catch (_) {}
  conversations = conversations.filter(row => row.session_id !== selected);
  if (pendingDeletions.has(selected)) {
    const pending = { ...pendingDeletions.get(selected), title: "删除清理未完成" };
    pendingDeletions.set(selected, pending); conversations.push(pending);
  }
  if (session === selected) {
    session = ""; sessionInput.value = ""; conversation = null; historyReady = false; input.value = "";
    displayedMessages = []; restoreBackend(); messages.replaceChildren();
    const empty = document.createElement("div"); empty.className = "welcome";
    const title = document.createElement("h2"); title.textContent = "会话已删除";
    const description = document.createElement("p"); description.textContent = "选择其他会话，或点击“＋ 新建会话”继续。";
    empty.append(title, description); messages.append(empty);
  }
  renderConversations(); updateControls();
}
async function continueAfterDeletion(createOnly = false, notice = "原会话已永久删除") {
  try {
    const loaded = await loadConversations();
    if (!loaded && !createOnly) throw new Error("会话列表暂时不可用");
    const next = createOnly ? null : conversations.find(row => !row.archived && !deletedConversations.has(row.session_id) && !pendingDeletions.has(row.session_id));
    if (next) displaySession(next.session_id, await readHistory(next.session_id));
    else { await createAndDisplayConversation(); await loadConversations(); }
    setStatus(`${notice}，已打开另一会话。`);
  } catch (error) { setStatus(`${notice}；${error.message}。请新建会话或重新选择。`, true); }
}
async function handleGoneConversation(selected, createOnly = false, allowNavigation = true, background = false) {
  const wasCurrent = session === selected;
  rememberLostDraft(selected);
  forgetConversation(selected, !allowNavigation);
  const notice = pendingDeletions.has(selected) ? "原会话已停止访问，删除清理尚未完成，请重试删除" : "原会话已永久删除";
  if (wasCurrent && allowNavigation) await continueAfterDeletion(createOnly, notice);
  else {
    if (navigationBusy && background) conversationsQueued = true;
    else await loadConversations();
    if (!background) setStatus(`${notice}，当前会话保持不变。`, true);
  }
}
async function deleteConversation(selected) {
  if (navigationBusy || running(selected) || pendingRenames.has(selected)) return;
  const row = conversations.find(item => item.session_id === selected);
  if (!row) return;
  const accepted = confirm(`永久删除“${row.title || "新会话"}”？此操作不可恢复。\n\n将删除此会话的消息、工具记录和对应审计记录。共享记忆和工作区文件保留。`);
  if (!accepted) return;
  setBusy(true, "conversation"); setStatus("正在永久删除会话…");
  try {
    ++conversationRevision; conversationsLoading = false;
    const data = await api("/api/conversations/delete", { method: "POST", body: JSON.stringify({ session_id: selected, confirm: true }) });
    if (data.session_id !== selected || data.deleted !== true) throw new Error("删除结果未确认，请重试");
    const wasCurrent = session === selected;
    pendingDeletions.delete(selected); forgetConversation(selected);
    if (wasCurrent) await continueAfterDeletion();
    else { await loadConversations(); setStatus("会话已永久删除，当前会话保持不变。"); }
  } catch (error) {
    if (error.responseInvalid || !Number.isInteger(error.status) || error.status >= 500) { pendingDeletions.set(selected, row); renderConversations(); }
    setStatus(`删除未完成：${error.message}。可以重试删除。`, true);
  } finally { sessionInput.value = session; setBusy(false); }
}
async function archiveConversation(selected, archived) {
  if (navigationBusy || running(selected) || pendingRenames.has(selected)) return;
  let archiveSucceeded = false;
  setBusy(true, "conversation"); setStatus(archived ? "正在归档会话…" : "正在恢复会话…");
  try {
    ++conversationRevision; conversationsLoading = false;
    const data = await api("/api/conversations/archive", { method: "POST", body: JSON.stringify({ session_id: selected, archived }) });
    const row = conversationRow(data.conversation);
    if (row.session_id !== selected || row.archived !== archived) throw new Error("会话状态不匹配，请刷新列表");
    archiveSucceeded = true;
    rememberConversation(row);
    const refreshed = await loadConversations();
    if (archived && selected === session) {
      if (!refreshed) throw new Error("会话已归档，但列表刷新失败；请重试列表或新建会话");
      const next = conversations.find(item => !item.archived);
      if (next) displaySession(next.session_id, await readHistory(next.session_id));
      else { await createAndDisplayConversation(); await loadConversations(); }
    } else setStatus(archived ? "会话已归档，记录仍保留。" : "会话已恢复，可以继续对话。");
  } catch (error) {
    if (error.status === 410 && !error.responseInvalid && !archiveSucceeded) await handleGoneConversation(selected);
    else setStatus(error.message, true);
  }
  finally { sessionInput.value = session; setBusy(false); }
}
async function renameConversation(selected) {
  if (navigationBusy || pendingRenames.has(selected) || deletedConversations.has(selected) || pendingDeletions.has(selected)) return;
  const current = conversations.find(row => row.session_id === selected);
  if (!current) return;
  const entered = prompt("重命名会话（1–80 个字符，单行）", current.title || "新会话");
  if (entered === null) return;
  const title = entered.trim();
  if (!title || [...title].length > 80 || /[\u0000-\u001f\u007f-\u009f\u2028\u2029\ud800-\udfff]/u.test(entered)) {
    conversationState.textContent = "名称须为 1–80 个字符，不能包含换行或控制字符。"; conversationState.className = "error"; return;
  }
  pendingRenames.add(selected); renderConversations();
  conversationState.textContent = "正在保存会话名称…"; conversationState.className = "";
  try {
    const data = await api("/api/conversations/rename", { method: "POST", body: JSON.stringify({ session_id: selected, title }) });
    const row = conversationRow(data.conversation);
    if (row.session_id !== selected || row.title !== title) throw new Error("重命名结果不匹配，请刷新列表确认");
    if (deletedConversations.has(selected)) return;
    const latest = conversations.find(item => item.session_id === selected);
    // A newer authoritative list may have observed deletion in another tab.
    // A delayed rename receipt must never recreate that missing conversation.
    if (!latest) {
      conversationState.textContent = "会话已不在列表中，请刷新确认。"; conversationState.className = ""; return;
    }
    const superseded = conversationUpdatedAfter(latest, row);
    const saved = superseded ? latest : { ...latest, title: row.title, updated_at: row.updated_at };
    renamedTitles.set(selected, { title: saved.title, updated_at: saved.updated_at, generation: ++titleGeneration });
    // Only rename metadata: concurrent chat completion may have newer counters.
    rememberConversation(saved);
    const run = sessionRuns.get(selected);
    if (run) run.row = { ...run.row, title: saved.title, updated_at: saved.updated_at };
    conversationState.textContent = superseded && saved.title !== title ? "会话已有更新，已保留最新名称。" : "会话名称已保存。";
    conversationState.className = "";
    if (session === selected && !navigationBusy) restoreRunStatus();
  } catch (error) {
    conversationState.textContent = `重命名未完成：${error.message}。请重试或刷新列表确认。`;
    conversationState.className = "error";
  } finally { pendingRenames.delete(selected); renderConversations(); }
}
document.getElementById("conversation-new").addEventListener("click", createConversation);
document.getElementById("conversation-retry").addEventListener("click", () => { if (!navigationBusy) return loadConversations(); });
document.getElementById("conversation-restore").addEventListener("click", () => archiveConversation(session, false));
const agentStates = Object.freeze({ ready: "服务就绪", busy: "执行中", starting: "启动中", idle: "运行中 · 空闲", stopped: "未启动", unavailable: "不可用", error: "进程异常" });
function agentStatusVisible() { return currentView === "chat" && agentStatusPanel.open && !document.hidden; }
function stopAgentStatusTimer() { if (agentStatusTimer !== null) clearTimeout(agentStatusTimer); agentStatusTimer = null; }
function scheduleAgentStatus() {
  stopAgentStatusTimer();
  if (agentStatusVisible() && !agentStatusAuthFailed) agentStatusTimer = setTimeout(() => { agentStatusTimer = null; refreshAgentStatus(); }, 5000);
}
function validateAgentStatus(data) {
  const count = value => Number.isSafeInteger(value) && value >= 0;
  const pid = value => value === null || Number.isSafeInteger(value) && value > 0;
  if (!data || data.scope !== (shared ? "visitor" : "local") || !Array.isArray(data.agents) || data.agents.length > 32
      || ![data.conversation_count, data.archived_conversation_count, data.resident_session_count, data.running_agent_count].every(count)
      || data.archived_conversation_count > data.conversation_count) throw new Error("Agent 状态数据无效，请重试");
  const seen = new Set();
  for (const row of data.agents) {
    if (!row || !configuredBackends.has(row.id) || seen.has(row.id) || typeof row.label !== "string"
        || !Object.hasOwn(agentStates, row.status) || !pid(row.pid) || !pid(row.manager_pid)
        || !count(row.session_count) || !(row.resident_session_count === null && row.id === "joyagent" || count(row.resident_session_count))
        || typeof row.reason !== "string") throw new Error("Agent 状态数据无效，请重试");
    seen.add(row.id);
  }
  return data;
}
function clearAgentStatus() {
  agentStatusValue = null;
  document.getElementById("agent-status-rows").replaceChildren();
  document.getElementById("agent-status-metrics").replaceChildren();
  document.getElementById("agent-status-summary").textContent = "状态不可用";
}
function renderAgentStatus(data) {
  const metrics = document.getElementById("agent-status-metrics"); metrics.replaceChildren();
  for (const [label, value, note] of [["会话总数", data.conversation_count, `含 ${data.archived_conversation_count} 个归档会话`],
    ["运行中 Agent", data.running_agent_count, "含 JoyAgent 服务"], ["常驻会话总数", data.resident_session_count, "外部 Agent 缓存合计"]]) {
    const item = document.createElement("div"); item.className = "agent-status-metric";
    const title = document.createElement("span"); title.textContent = label;
    const total = document.createElement("strong"); total.textContent = String(value);
    const detail = document.createElement("small"); detail.textContent = note;
    item.append(title, total, detail); metrics.append(item);
  }
  const target = document.getElementById("agent-status-rows"); target.replaceChildren();
  for (const agent of data.agents) {
    const row = document.createElement("tr");
    const name = document.createElement("td"); name.textContent = agent.label;
    const state = document.createElement("td"); const badge = document.createElement("span");
    badge.className = `agent-status-state ${agent.status}`; badge.textContent = agentStates[agent.status]; state.append(badge);
    if (agent.reason) { const reason = document.createElement("small"); reason.textContent = agent.reason; state.append(reason); }
    const pid = document.createElement("td"); pid.textContent = agent.pid === null ? "—" : String(agent.pid);
    if (agent.manager_pid !== null && agent.manager_pid !== agent.pid) {
      const manager = document.createElement("small"); manager.textContent = `管理 PID ${agent.manager_pid}`; pid.append(manager);
    }
    if (agent.pid === null && agent.manager_pid !== null) {
      const pending = document.createElement("small"); pending.textContent = "Agent PID 待确认"; pid.append(pending);
    }
    if (agent.id === "joyagent") { const builtin = document.createElement("small"); builtin.textContent = "Web 服务进程"; pid.append(builtin); }
    const sessions = document.createElement("td"); sessions.textContent = String(agent.session_count);
    const resident = document.createElement("td"); resident.textContent = agent.resident_session_count === null ? "不适用" : String(agent.resident_session_count);
    row.append(name, state, pid, sessions, resident); target.append(row);
  }
  document.getElementById("agent-status-scope").textContent = shared ? "当前访客 · 当前工作区" : "本地服务 · 当前工作区";
  document.getElementById("agent-status-summary").textContent = `${data.running_agent_count} 个运行中 · ${data.conversation_count} 个会话`;
}
function pauseAgentStatus() {
  stopAgentStatusTimer(); agentStatusQueued = false; ++agentStatusRevision;
  agentStatusRequest?.controller.abort();
}
async function refreshAgentStatus({ manual = false } = {}) {
  if (!agentStatusVisible() || agentStatusAuthFailed && !manual) return;
  stopAgentStatusTimer();
  if (agentStatusRequest) { agentStatusQueued = true; ++agentStatusRevision; return; }
  if (manual) agentStatusAuthFailed = false;
  const revision = ++agentStatusRevision;
  const controller = new AbortController();
  const request = { controller }; agentStatusRequest = request;
  agentStatusRefresh.disabled = true; agentStatusPanel.setAttribute("aria-busy", "true");
  agentStatusFeedback.className = "agent-status-feedback"; agentStatusFeedback.textContent = "正在刷新状态…";
  let timeout;
  try {
    const deadline = new Promise((_, reject) => {
      controller.signal.addEventListener("abort", () => reject(Object.assign(new Error("状态读取已取消"), { name: "AbortError" })), { once: true });
      timeout = setTimeout(() => { reject(new Error("读取 Agent 状态超时，请重试")); controller.abort(); }, 8000);
    });
    const data = validateAgentStatus(await Promise.race([api("/api/agents/status", { signal: controller.signal }), deadline]));
    if (revision !== agentStatusRevision || !agentStatusVisible()) return;
    agentStatusValue = data; renderAgentStatus(data);
    agentStatusFeedback.textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })} · 展开时每 5 秒刷新`;
  } catch (error) {
    if (revision !== agentStatusRevision || !agentStatusVisible()) return;
    if ([401, 403].includes(error.status)) { agentStatusAuthFailed = true; clearAgentStatus(); }
    if (error.name !== "AbortError") {
      agentStatusFeedback.className = "agent-status-feedback error";
      agentStatusFeedback.textContent = `${error.message}${agentStatusValue ? "；下方为上次读取结果，可能已变化。" : ""}`;
      document.getElementById("agent-status-summary").textContent = agentStatusValue ? "状态待刷新" : "状态不可用";
    }
  } finally {
    clearTimeout(timeout);
    if (agentStatusRequest === request) {
      agentStatusRequest = null; agentStatusRefresh.disabled = false; agentStatusPanel.setAttribute("aria-busy", "false");
      const queued = agentStatusQueued; agentStatusQueued = false;
      if (queued && agentStatusVisible() && !agentStatusAuthFailed) refreshAgentStatus();
      else scheduleAgentStatus();
    }
  }
}
agentStatusRefresh.addEventListener("click", () => refreshAgentStatus({ manual: true }));
agentStatusPanel.addEventListener("toggle", () => { if (agentStatusVisible()) refreshAgentStatus(); else pauseAgentStatus(); });
document.addEventListener("visibilitychange", () => {
  if (agentStatusVisible()) refreshAgentStatus(); else pauseAgentStatus();
  for (const feed of publicFeeds) feed.syncVisibility();
});
function backendLabel(id) { return backends.find(backend => backend.id === id)?.label || configuredBackendLabels.get(id) || (id === "joyagent" ? "JoyAgent" : id === "opencode" ? "OpenCode" : id); }
function restoreBackend() {
  let saved = backendSelections.get(session);
  if (saved === undefined) { try { saved = localStorage.getItem(backendStoragePrefix + session); } catch (_) {} }
  // OpenCode was retired by the owner. Drop its obsolete preference while
  // keeping the conversation and historical Agent labels intact.
  if (saved === "opencode") {
    backendSelections.delete(session);
    try { localStorage.removeItem(backendStoragePrefix + session); } catch (_) {}
    saved = null;
  }
  // Other removed or unavailable Agents still require an explicit replacement.
  const selected = saved || defaultBackend;
  backendInput.value = selected;
  if (!Array.from(backendInput.options).some(option => option.value === selected)) {
    const option = document.createElement("option"); option.value = selected; option.textContent = `${selected}（已移除）`; option.disabled = true;
    backendInput.append(option); backendInput.value = selected;
  }
  updateBackendControls();
}
function updateBackendControls() {
  backendInput.disabled = !backendsReady || (busy && busyMode !== "chat") || conversation?.archived === true;
  const selected = backends.find(backend => backend.id === backendInput.value);
  if (conversation?.archived) { backendNote.textContent = "此会话已归档。恢复后可继续选择 Agent 和执行任务。"; backendNote.className = "backend-note"; return; }
  if (!backendsReady) { backendNote.textContent = backendError || "正在读取 Agent 列表…"; backendNote.className = backendError ? "backend-note error" : "backend-note"; return; }
  if (!selected || !selected.available) {
    backendNote.textContent = `${backendLabel(backendInput.value)} 暂不可用：${selected?.reason || "此 Agent 已不在服务配置中，请手动选择其他 Agent。"}`;
    backendNote.className = "backend-note error"; return;
  }
  backendNote.className = "backend-note";
  backendNote.textContent = activeBackend
    ? `当前由 ${backendLabel(activeBackend)} 执行；下一条消息使用 ${selected.label}。切换不会中断当前任务。`
    : `下一条消息使用 ${selected.label}。可随时切换 Agent，当前会话记录和工作区会保留。`;
}
backendInput.addEventListener("change", () => {
  const selected = backends.find(backend => backend.id === backendInput.value);
  if (!backendsReady || (busy && busyMode !== "chat") || conversation?.archived || !selected?.available) { updateBackendControls(); return; }
  backendSelections.set(session, selected.id);
  try { localStorage.setItem(backendStoragePrefix + session, selected.id); } catch (_) {}
  updateBackendControls();
});
async function loadBackends() {
  try {
    const data = await api("/api/backends");
    const invalid = () => new Error("Agent 列表数据无效，请刷新页面后重试");
    if (!Array.isArray(data.backends) || !data.backends.length || data.backends.length > knownBackendIds.size
        || !knownBackendIds.has(data.default)) throw invalid();
    const seen = new Set();
    const catalog = data.backends.map(backend => {
      if (!backend || !knownBackendIds.has(backend.id) || seen.has(backend.id)
          || typeof backend.label !== "string" || !backend.label.trim() || backend.label.length > 80
          || typeof backend.available !== "boolean" || typeof backend.reason !== "string" || backend.reason.length > 500
          || /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/u.test(backend.label + backend.reason)) throw invalid();
      seen.add(backend.id);
      return { id: backend.id, label: backend.label, available: backend.available, reason: backend.reason };
    });
    if (!seen.has(data.default)) throw invalid();
    const options = catalog.map(backend => {
      const option = document.createElement("option"); option.value = backend.id; option.disabled = !backend.available;
      option.textContent = `${backend.label}${backend.available ? "" : `（${backend.reason || "不可用"}）`}`;
      option.title = backend.reason; return option;
    });
    // Validate the entire response before replacing stale server-rendered choices.
    backends = catalog; defaultBackend = data.default;
    configuredBackends.clear();
    catalog.forEach(backend => { configuredBackends.add(backend.id); configuredBackendLabels.set(backend.id, backend.label); });
    backendInput.replaceChildren(...options);
    backendsReady = true; backendError = ""; restoreBackend();
  } catch (error) { backendsReady = false; backendError = error.message; updateBackendControls(); }
}
function showModel() {
  const provider = document.body.dataset.provider;
  const label = document.getElementById("model-label");
  label.textContent = modelInput.value === "auto" ? "自动选择（Decision）" : provider === "demo" ? "离线 Demo" : `${provider} · ${modelInput.value}`;
  label.title = label.textContent;
  if (modelInput.value === "auto") document.getElementById("model-note").textContent = "每轮先判断任务复杂度，再选择模型；回复中显示实际模型和选择原因。手动选择模型可跳过分类。";
  else if (provider !== "demo") document.getElementById("model-note").textContent = `模型配置：${provider} / ${modelInput.value}`;
}
modelInput.addEventListener("change", () => {
  if (busy || conversation?.archived || !allowedModels.has(modelInput.value)) return;
  preferredModel = modelInput.value;
  try { localStorage.setItem(modelStorageKey, modelInput.value); } catch (_) {}
  showModel();
  setStatus(modelInput.value === "auto" ? "已选择自动路由 · 下一条消息先判断复杂度，再选择模型" : `已选择 ${modelInput.value} · 下一条消息使用此模型`);
});
showModel();
function routingSummary(routing) {
  if (!routing || routing.mode !== "auto" || !["simple", "complex"].includes(routing.category)
      || routing.model === "auto" || !allowedModels.has(routing.model)) return "";
  const reason = routing.reason === "classified" ? (routing.category === "simple" ? "简单任务" : "复杂任务")
    : routing.reason === "low_confidence" ? "分类置信度不足，使用回退模型"
    : routing.reason === "decision_failed" ? "决策服务未完成，使用回退模型" : "";
  return reason ? `自动选择 · ${reason} → ${routing.model}` : "";
}
function welcome() {
  messages.replaceChildren();
  const box = document.createElement("div"); box.className = "welcome";
  const symbol = document.createElement("div"); symbol.className = "symbol"; symbol.textContent = "✳";
  const title = document.createElement("h2"); title.textContent = "从一个小问题开始。";
  const desc = document.createElement("p"); desc.textContent = document.body.dataset.provider === "demo" ? "当前是离线工具演示，可以查看时间、操作工作区文件和保存偏好。先试试下面的指令，配置真实模型后即可自由对话。" : "我可以和你一起思考、记住偏好，并在你配置的权限内使用工具。你掌握模型、数据和行动的边界。";
  const suggestions = document.createElement("div"); suggestions.className = "suggestions";
  ["现在几点", "列出文件", "记住：我喜欢简洁的中文回答"].forEach(text => { const button = document.createElement("button"); button.type = "button"; button.textContent = text; button.addEventListener("click", () => { if (!busy && !conversation?.archived) { input.value = text; input.focus(); } }); suggestions.append(button); });
  box.append(symbol, title, desc, suggestions); messages.append(box);
}
function append(role, text, events = [], agentLabel = "JoyAgent", routing = null) {
  messages.querySelector(".welcome")?.remove();
  const row = document.createElement("article"); row.className = `message ${role}`;
  const avatar = document.createElement("div"); avatar.className = "avatar"; avatar.textContent = role === "user" ? "你" : "joy";
  const body = document.createElement("div"); body.className = "message-body";
  const label = document.createElement("div"); label.className = "message-role"; label.textContent = role === "user" ? "你" : agentLabel;
  const content = document.createElement("div"); content.className = "message-text"; content.textContent = text;
  body.append(label);
  const route = role === "assistant" ? routingSummary(routing) : "";
  if (route) { const note = document.createElement("div"); note.className = "routing-note"; note.textContent = route; body.append(note); }
  body.append(content);
  if (events.length) {
    const failed = events.filter(event => event.ok !== true).length;
    const details = document.createElement("details"); details.className = "tools"; details.open = failed > 0;
    const summary = document.createElement("summary");
    summary.textContent = failed ? "工具详情 · 有执行失败" : "工具详情";
    details.append(summary);
    for (const event of events) {
      const result = document.createElement("section"); result.className = `tool-result${event.ok === true ? "" : " tool-failed"}`;
      const heading = document.createElement("div"); heading.className = "tool-result-heading";
      heading.textContent = `${event.ok === true ? "成功" : "失败"} · ${event.name || "工具"}${Number.isFinite(event.duration_ms) ? ` · ${event.duration_ms} ms` : ""}`;
      const pre = document.createElement("pre"); pre.textContent = JSON.stringify(event.result, null, 2);
      result.append(heading, pre); details.append(result);
    }
    body.append(details);
  }
  row.append(avatar, body); messages.append(row); messages.scrollTop = messages.scrollHeight;
}
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "X-CSRF-Token": token, "Content-Type": "application/json", ...(options.headers || {}) } });
  if (shared && response.status === 401) throw Object.assign(new Error("浏览器会话已失效，请刷新页面后重试"), { status: 401 });
  const invalidResponse = () => {
    const reason = response.status === 504 ? "服务响应超时" : [502, 503].includes(response.status) ? "服务暂时不可用" : "服务返回了无法解析的响应";
    const next = path === "/api/chat" || path.startsWith("/api/chat/") ? "本轮结果尚未确认，请重新打开会话历史核对，避免重复执行。" : "请稍后核对状态或联系服务管理员。";
    // Keep transport status for diagnosis without treating an HTML error page as an application acknowledgement.
    return Object.assign(new Error(`${reason}（HTTP ${response.status}）。${next}`), { status: response.status, responseInvalid: true });
  };
  let data;
  try { data = await response.json(); }
  catch (error) { if (error.name === "AbortError") throw error; throw invalidResponse(); }
  if (!data || typeof data !== "object" || Array.isArray(data)) throw invalidResponse();
  if (!response.ok) throw Object.assign(new Error(typeof data.error === "string" && data.error ? data.error : `请求失败 (${response.status})`), { status: response.status });
  return data;
}
async function readHistory(selected) {
  historyTarget = selected;
  const requestedGeneration = titleGeneration;
  const requestedRun = running(selected) ? sessionRuns.get(selected) : null;
  const data = await api(`/api/history?session_id=${encodeURIComponent(selected)}`);
  return { ...data, requestedRun, titleGeneration: requestedGeneration };
}
async function loadSession(value) {
  if (navigationBusy) return;
  const selected = typeof value === "string" ? value : sessionInput.value.trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,123}$/.test(selected)) { setStatus("会话名须为 1–124 位字母、数字、点、冒号、下划线或连字符", true); return; }
  const initial = !historyReady && session === selected;
  const revision = ++historyRevision;
  setBusy(true, "history"); setStatus("正在载入会话…");
  try {
    if (deletedConversations.has(selected)) throw Object.assign(new Error("此会话已永久删除"), { status: 410 });
    const data = await readHistory(selected);
    if (revision !== historyRevision) return;
    displaySession(selected, data); await loadConversations();
  } catch (error) {
    if (revision !== historyRevision) return;
    if (error.status === 410 && !error.responseInvalid) await handleGoneConversation(selected, initial);
    else setStatus(error.message, true);
  }
  finally { sessionInput.value = session; setBusy(false); }
}
document.getElementById("switch").addEventListener("click", loadSession);
sessionInput.addEventListener("keydown", event => { if (event.key === "Enter") loadSession(); });
function requestIdentifier() { return crypto.randomUUID().replaceAll("-", ""); }
function acceptSteering(run, value, final = false) {
  if (!value || value.turn_id !== run.turnId || !/^[A-Za-z0-9_-]{16,64}$/.test(value.request_id)
      || typeof value.message !== "string" || !["next_step", "next_turn"].includes(value.delivery)
      || !(final ? ["applied", "unapplied"] : ["queued"]).includes(value.status)) throw new Error("补充指令回执无效，请保留原文后重试");
  const previous = run.steering.get(value.request_id);
  if (previous && previous.message !== value.message) throw new Error("补充指令回执不匹配，请保留原文后重试");
  // A queued acknowledgement may arrive after the final, authoritative result.
  if (!final && previous && ["applied", "unapplied"].includes(previous.status)) return previous;
  const item = { ...previous, ...value };
  if (!final && run.outcomeUnknown) item.status = "unknown";
  run.steering.set(item.request_id, item);
  if (!run.messages?.some(message => message.steering_id === item.request_id)) {
    if (run.messages) run.messages = [...run.messages, { role: "user", content: item.message, steering_id: item.request_id }].slice(-80);
  }
  return item;
}
async function sendSteering(text) {
  const selected = session, run = sessionRuns.get(selected);
  if (navigationBusy || !run?.pending || run.steerSending || run.steeringClosed || !text) return;
  const draft = input.value, revision = draftRevisions.get(selected) || 0;
  const retry = [...run.steering.values()].find(item => ["unknown", "rejected"].includes(item.status) && item.message === text);
  const requestId = retry?.request_id || requestIdentifier();
  const item = { turn_id: run.turnId, request_id: requestId, message: text, delivery: run.delivery, status: "sending", draft, revision };
  run.steering.set(requestId, item); run.steerSending = true; run.steerError = false; run.steerNotice = "正在提交补充指令…";
  updateControls();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const receipt = await api("/api/steer", { method: "POST", signal: controller.signal,
      body: JSON.stringify({ session_id: selected, turn_id: run.turnId, request_id: requestId, message: text }) });
    if (deletedConversations.has(selected) || sessionRuns.get(selected) !== run) return;
    if (receipt.request_id !== requestId || receipt.message !== text) throw new Error("补充指令回执不匹配，请保留原文后重试");
    const accepted = acceptSteering(run, receipt);
    if (["queued", "applied"].includes(accepted.status)) clearSubmittedDraft(selected, draft, revision);
    run.steerError = ["unapplied", "unknown"].includes(accepted.status);
    run.steerNotice = accepted.status === "applied" ? "补充指令已应用。" : accepted.status === "unapplied" ? "补充指令未应用，请核对结果后重新发送。"
      : run.outcomeUnknown ? "补充指令已接收，但本轮结果未确认；请重新打开会话历史核对，不要重复执行已完成操作。"
      : run.pending ? "补充指令已接收，等待应用。" : "补充指令已接收，请核对会话历史中的执行结果。";
    if (session === selected && run.pending && !navigationBusy && run.messages) renderMessages(run.messages);
  } catch (error) {
    if (deletedConversations.has(selected) || sessionRuns.get(selected) !== run) return;
    const accepted = run.steering.get(requestId);
    if (["applied", "unapplied"].includes(accepted?.status)) {
      if (accepted.status === "applied") clearSubmittedDraft(selected, draft, revision);
      run.steerError = accepted.status === "unapplied";
      run.steerNotice = accepted.status === "applied" ? "补充指令已应用。" : "补充指令未应用，请核对结果后重新发送。";
    } else {
      item.status = !error.responseInvalid && [400, 401, 403, 409, 410, 429].includes(error.status) ? "rejected" : "unknown";
      run.steering.set(requestId, item); run.steerError = true;
      if (error.status === 409 && !error.responseInvalid) {
        run.steeringClosed = true;
        run.steerNotice = "本轮已结束或不再接受补充，原文已保留；收到本轮结果后可作为新消息发送。";
      } else if (error.status === 429 && !error.responseInvalid) run.steerNotice = "补充指令队列已满，原文已保留，请等待本轮完成。";
      else if (item.status === "unknown") run.steerNotice = "提交结果尚未确认，原文已保留；本轮仍运行时重发相同文字会核对同一条指令。";
      else run.steerNotice = error.message;
    }
  } finally {
    clearTimeout(timeout); run.steerSending = false; updateControls();
  }
}
async function refreshCompletedHistory(selected, run) {
  try {
    const data = await api(`/api/history?session_id=${encodeURIComponent(selected)}`);
    if (sessionRuns.get(selected) !== run || run.pending || deletedConversations.has(selected)) return;
    if (!Array.isArray(data.messages)) throw new Error("会话历史格式无效");
    run.messages = data.messages;
    if (session === selected && !navigationBusy) renderMessages(run.messages);
  } catch (_) {
    if (sessionRuns.get(selected) === run) {
      run.steerNotice = "会话历史暂未刷新，请重新打开会话核对补充指令和执行结果。";
      run.steerError = true; renderSteering();
    }
  }
}
async function sendMessage(text) {
  if (busy || sessionRuns.get(session)?.steerSending || !text) return;
  if (conversation?.archived || !historyReady) { setStatus(conversation?.archived ? "此会话已归档，请先恢复后再发送消息或执行工具。" : "会话尚未载入，请重新打开会话。", true); return; }
  const selectedSession = session;
  const selectedModel = modelInput.value;
  const selectedBackend = backendInput.value;
  const routesToJoyAgent = ["/tool", "/tools"].includes(text.trim().split(/\s+/, 1)[0]);
  const backend = backends.find(item => item.id === selectedBackend);
  if (!routesToJoyAgent && (!backendsReady || !backend?.available)) { setStatus("所选 Agent 不可用，请选择可用的 Agent 后重试", true); return; }
  if (!configuredBackends.has(selectedBackend)) { setStatus("所选 Agent 已移除，请手动选择其他 Agent", true); return; }
  if (!allowedModels.has(selectedModel)) { setStatus("所选模型不可用，请刷新页面后选择", true); return; }
  const run = {
    turnId: requestIdentifier(), steering: new Map(), steerSending: false,
    delivery: !routesToJoyAgent && selectedBackend === "joyagent" ? "next_step" : "next_turn",
    draftRevision: draftRevisions.get(selectedSession) || 0,
    pending: true, error: false, model: selectedModel, backend: routesToJoyAgent ? "joyagent" : selectedBackend,
    messages: [...displayedMessages, { role: "user", content: text }].slice(-80),
    row: conversation || { session_id: selectedSession, title: text.slice(0, 48), archived: false },
    status: routesToJoyAgent ? "正在执行工具…" : selectedModel === "auto" ? "正在判断任务复杂度并选择模型…" : "正在处理，可切换或新建其他会话。同一 Agent 的任务可能需要等待。",
  };
  sessionRuns.set(selectedSession, run);
  input.value = ""; drafts.set(selectedSession, "");
  renderMessages(run.messages); updateControls(); rememberConversation(run.row); restoreRunStatus(); refreshAgentStatus();
  try {
    const data = await api("/api/chat", { method: "POST", body: JSON.stringify({ message: text, session_id: selectedSession, model: selectedModel, backend: selectedBackend, turn_id: run.turnId }) });
    if (deletedConversations.has(selectedSession) || sessionRuns.get(selectedSession) !== run) return;
    const events = data.tool_events || [];
    const continued = Number(data.steps) > 0 && Array.isArray(data.steering) && data.steering.length > 0;
    const directOnly = routesToJoyAgent && !continued;
    for (const value of data.steering || []) {
      const item = acceptSteering(run, value, true);
      if (item.status === "applied" && typeof item.draft === "string") clearSubmittedDraft(selectedSession, item.draft, item.revision);
    }
    run.messages = [...run.messages, { role: "assistant", content: data.text || "（本轮没有文本回复）", tool_events: events,
      backend_label: routesToJoyAgent ? "JoyAgent" : data.backend_label || backendLabel(selectedBackend), routing: directOnly ? null : data.routing }].slice(-80);
    const model = directOnly ? "直接工具 · 未调用聊天模型" : routingSummary(data.routing) || data.model || (selectedModel === "auto" ? "自动选择未完成" : selectedModel);
    run.error = data.stopped === true;
    run.status = `${data.backend_label || backendLabel(selectedBackend)} · ${model} · ${data.steps} 次聊天模型请求 · ${data.stopped ? "本轮未完成，原因见回复" : "完成"}`;
    run.pending = false;
    if (run.steering.size) {
      const unapplied = [...run.steering.values()].filter(item => item.status === "unapplied").length;
      const uncertain = [...run.steering.values()].some(item => ["sending", "unknown", "queued"].includes(item.status));
      const rejected = [...run.steering.values()].some(item => item.status === "rejected");
      run.steerError = unapplied > 0 || uncertain || rejected;
      run.steerNotice = unapplied ? `${unapplied} 条补充指令未应用，请核对结果后重新发送。`
        : uncertain ? "部分补充指令的执行状态待确认，请核对会话历史，不要重复执行已完成操作。"
        : rejected ? "部分补充指令未被接收，原文仍保留，可核对后重新发送。" : "补充指令已应用。";
    }
    if (session === selectedSession) {
      renderMessages(run.messages);
      if (!navigationBusy) restoreRunStatus();
    }
    updateControls(); renderConversations();
    if (run.steering.size) await refreshCompletedHistory(selectedSession, run);
    if (navigationBusy) conversationsQueued = true;
    else await loadConversations();
  }
  catch (error) {
    run.pending = false; run.error = true; run.status = error.message;
    run.outcomeUnknown = true;
    let uncertain = false;
    for (const item of run.steering.values()) {
      if (["sending", "queued", "unknown"].includes(item.status)) { item.status = "unknown"; uncertain = true; }
    }
    if (uncertain) {
      run.steerError = true;
      run.steerNotice = "本轮结果未确认，补充指令原文已保留在此处；请重新打开会话历史核对，不要重复执行已完成操作。";
    }
    if (error.status === 410 && !error.responseInvalid) {
      const mayNavigate = !navigationBusy;
      const ownsNavigation = mayNavigate && session === selectedSession;
      if (ownsNavigation) setBusy(true, "conversation");
      try { await handleGoneConversation(selectedSession, false, mayNavigate, true); }
      finally { if (ownsNavigation) setBusy(false); }
    } else if (!deletedConversations.has(selectedSession)) {
      const restoreDraft = !currentDraft(selectedSession) && !run.steering.size
        && (draftRevisions.get(selectedSession) || 0) === run.draftRevision;
      if (restoreDraft) drafts.set(selectedSession, text);
      if (session === selectedSession) {
        if (restoreDraft) input.value = text;
        if (!navigationBusy) restoreRunStatus();
      }
    }
  }
  finally {
    run.pending = false;
    updateControls(); renderConversations(); trimCompletedViews(); refreshAgentStatus();
    if (currentView === "chat" && session === selectedSession && !navigationBusy) input.focus();
  }
}
document.getElementById("composer").addEventListener("submit", async event => {
  event.preventDefault();
  if (running()) await sendSteering(input.value.trim());
  else await sendMessage(input.value.trim());
});
input.addEventListener("input", () => {
  drafts.set(session, input.value); draftRevisions.set(session, (draftRevisions.get(session) || 0) + 1);
});
input.addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); document.getElementById("composer").requestSubmit(); } });

function newsLink(value) {
  if (typeof value !== "string" || value.length > 4096 || /[\u0000-\u0020\u007f\\]/.test(value)) return null;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && url.hostname && !url.username && !url.password ? url.href : null;
  } catch (_) { return null; }
}
function newsTime(value) {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) return "尚无记录";
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}
function newsAnchor(label, url) {
  const link = document.createElement("a"); link.textContent = label; link.href = url;
  link.target = "_blank"; link.rel = "noopener noreferrer"; return link;
}
function validateNews(data, sourceIds = newsSourceIds, label = "资讯") {
  const timestamp = value => value === null || typeof value === "string" && Number.isFinite(Date.parse(value));
  if (!data || typeof data.enabled !== "boolean" || typeof data.refreshing !== "boolean"
      || !Number.isSafeInteger(data.interval_seconds) || data.interval_seconds < 1 || data.interval_seconds > 604800
      || !timestamp(data.last_attempt_at) || !timestamp(data.next_refresh_at)
      || !Array.isArray(data.sources) || data.sources.length > sourceIds.size || !Array.isArray(data.items) || data.items.length > 100) throw new Error(`${label}数据无效，请稍后刷新列表`);
  const seen = new Set();
  for (const source of data.sources) {
    if (!source || !sourceIds.has(source.id) || seen.has(source.id) || typeof source.name !== "string"
        || !newsLink(source.url) || !timestamp(source.last_success_at) || !timestamp(source.last_attempt_at)
        || !(source.error === null || typeof source.error === "string")
        || !Number.isSafeInteger(source.item_count) || source.item_count < 0) throw new Error(`${label}来源数据无效，请稍后刷新列表`);
    seen.add(source.id);
  }
  // A malformed article cannot introduce an executable URL or hide valid news.
  return { ...data, items: data.items.filter(item => item && typeof item.id === "string" && seen.has(item.source_id)
    && typeof item.title === "string" && item.title.trim() && item.title.length <= 1000 && newsLink(item.url)
    && (item.published_at === null || typeof item.published_at === "string") && timestamp(item.fetched_at)) };
}
function renderNews(data) {
  const interval = data.interval_seconds;
  const cadence = interval % 3600 === 0 ? `${interval / 3600} 小时` : interval % 60 === 0 ? `${interval / 60} 分钟` : `${interval} 秒`;
  document.getElementById("news-schedule").textContent = data.enabled
    ? `每 ${cadence}自动抓取，前 10 条展示热榜资讯，其余按发布时间倒序补足，最多保留 100 条。`
    : "资讯定时抓取尚未启用，请联系服务管理员。";
  const list = document.getElementById("news-list"); list.replaceChildren();
  document.getElementById("news-count").textContent = `${data.items.length} 条`;
  for (const item of data.items) {
    const article = document.createElement("article"); article.className = "news-item";
    const heading = document.createElement("h3"); heading.append(newsAnchor(item.title, newsLink(item.url)));
    const meta = document.createElement("div"); meta.className = "news-item-meta";
    const destination = newsAnchor(`${new URL(item.url).hostname} ↗`, newsLink(item.url));
    meta.append(destination);
    if (item.source_id === "aihot" && Number.isInteger(item.hot_rank) && item.hot_rank >= 1 && item.hot_rank <= 10) {
      const rank = document.createElement("span"); rank.textContent = `热榜第 ${item.hot_rank} 名`; meta.append(rank);
    }
    if (item.published_at && /^\d{4}-\d{2}-\d{2}/.test(item.published_at)) {
      const date = document.createElement("time"); date.dateTime = item.published_at; date.textContent = item.published_at.slice(0, 10); meta.append(date);
    } else if (typeof item.published_label === "string" && item.published_label.length <= 80) {
      const date = document.createElement("span"); date.textContent = item.published_label; meta.append(date);
    }
    article.append(heading, meta); list.append(article);
  }
  if (!data.items.length) {
    const empty = document.createElement("p"); empty.className = "news-empty";
    empty.textContent = data.refreshing ? "正在抓取首批资讯，请稍后刷新列表。"
      : data.sources.some(source => source.error) ? "暂未获取到资讯，服务会按计划重试。"
      : data.enabled ? "暂时没有资讯，服务会按计划自动抓取。" : "启用后，这里会展示资讯标题和原文链接。";
    list.append(empty);
  }
  const sourceFailed = data.sources.some(source => source.error);
  newsStatus.className = sourceFailed ? "news-status error" : "news-status";
  const sourceNotice = sourceFailed ? data.items.length ? " · 部分来源暂未更新，显示已缓存资讯。" : " · 来源抓取失败，将按计划重试。" : "";
  newsStatus.textContent = `${data.refreshing ? "正在抓取资讯 · " : ""}最近检查：${newsTime(data.last_attempt_at)}${data.enabled ? ` · 下次抓取：${newsTime(data.next_refresh_at)}` : ""}${sourceNotice}`;
}
function validatePapers(data) {
  const value = validateNews(data, new Set(["agents"]), "论文");
  if (value.items.length > 20) throw new Error("智能体论文数据无效，请稍后刷新列表");
  const raw = value.translation;
  const translation = raw && typeof raw.enabled === "boolean" && typeof raw.running === "boolean"
    && Number.isInteger(raw.completed) && raw.completed >= 0 && Number.isInteger(raw.total)
    && raw.total >= raw.completed && raw.total <= 20 && (raw.error === null || typeof raw.error === "string" && raw.error.length <= 200)
    ? raw : { enabled: false, running: false, completed: 0, total: 0, error: null };
  return { ...value, translation, items: value.items.filter(item => typeof item.arxiv_id === "string"
    && /^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?\/\d{7})(?:v\d+)?$/.test(item.arxiv_id)
    && item.url === `https://arxiv.org/abs/${item.arxiv_id}`
    && item.pdf_url === `https://arxiv.org/pdf/${item.arxiv_id}`
    && item.discussion_url === `https://huggingface.co/papers/${item.arxiv_id}`
    && typeof item.summary === "string" && item.summary.length <= 20000
    && ["collection", "daily"].includes(item.selection_source)
    && typeof item.published_at === "string" && Number.isFinite(Date.parse(item.published_at))
    && (item.paper_published_at === null || typeof item.paper_published_at === "string" && Number.isFinite(Date.parse(item.paper_published_at))))
    .map(item => ({ ...item, summary_zh: translation.enabled && typeof item.summary_zh === "string"
      && item.summary_zh.trim() && item.summary_zh.length <= 40000 ? item.summary_zh : "" })) };
}
function renderPapers(data) {
  const interval = data.interval_seconds;
  const cadence = interval % 3600 === 0 ? `${interval / 3600} 小时` : interval % 60 === 0 ? `${interval / 60} 分钟` : `${interval} 秒`;
  document.getElementById("papers-schedule").textContent = data.enabled
    ? `14 个研究方向，Agents 智能体精选 20 篇，每 ${cadence}自动更新。`
    : "论文定时抓取尚未启用，请联系服务管理员。";
  const list = document.getElementById("papers-list"); list.replaceChildren();
  document.getElementById("papers-count").textContent = `${data.items.length} 篇`;
  for (const item of data.items) {
    const article = document.createElement("article"); article.className = "news-item paper-item";
    const heading = document.createElement("h3"); heading.append(newsAnchor(item.title, item.url));
    const meta = document.createElement("div"); meta.className = "news-item-meta";
    if (item.paper_published_at) {
      const published = document.createElement("time"); published.dateTime = item.paper_published_at;
      published.textContent = `发表 ${item.paper_published_at.slice(0, 10)}`; meta.append(published);
    }
    const selection = document.createElement("span"); selection.className = "paper-selection-source";
    selection.textContent = item.selection_source === "collection" ? "Tempo14 合集" : "Hugging Face 补选"; meta.append(selection);
    meta.append(newsAnchor("论文原文 ↗", item.url), newsAnchor("PDF ↗", item.pdf_url), newsAnchor("HF 讨论 ↗", item.discussion_url));
    article.append(heading, meta);
    if (item.summary) {
      const text = item.summary_zh || item.summary;
      if (data.translation.enabled) {
        const language = document.createElement("span");
        language.textContent = item.summary_zh ? "中文摘要 · 机器翻译" : data.translation.error ? "英文摘要 · 翻译暂不可用" : "英文摘要 · 待翻译";
        meta.append(language);
      }
      const preview = document.createElement("p"); preview.className = "paper-summary";
      preview.textContent = text.length > 300 ? `${text.slice(0, 300).trimEnd()}…` : text;
      article.append(preview);
      if (text.length > 300) {
        const details = document.createElement("details"); details.className = "paper-abstract";
        const summary = document.createElement("summary"); summary.textContent = item.summary_zh ? "查看完整中文摘要" : "查看完整摘要";
        const abstract = document.createElement("p"); abstract.textContent = text;
        details.append(summary, abstract); article.append(details);
      }
      if (item.summary_zh) {
        const details = document.createElement("details"); details.className = "paper-abstract";
        const summary = document.createElement("summary"); summary.textContent = "查看英文原文";
        const abstract = document.createElement("p"); abstract.textContent = item.summary;
        details.append(summary, abstract); article.append(details);
      }
    }
    list.append(article);
  }
  const failed = data.sources.some(source => source.error);
  if (!data.items.length) {
    const empty = document.createElement("p"); empty.className = "news-empty";
    empty.textContent = data.refreshing ? "正在抓取首批论文，请稍后刷新列表。" : failed ? "暂未获取到论文，服务会按计划重试。"
      : data.enabled ? "暂时没有论文，服务会按计划自动抓取。" : "启用后，这里会展示论文标题、摘要和原文链接。";
    list.append(empty);
  }
  papersStatus.className = failed ? "news-status error" : "news-status";
  papersStatus.textContent = `${data.refreshing ? "正在抓取论文 · " : ""}最近检查：${newsTime(data.last_attempt_at)}${data.enabled ? ` · 下次抓取：${newsTime(data.next_refresh_at)}` : ""}${failed ? data.items.length ? " · 来源暂未更新，显示已缓存论文。" : " · 来源抓取失败，将按计划重试。" : ""}`;
  if (data.translation.enabled) {
    papersStatus.textContent += ` · 中文摘要 ${data.translation.completed}/${data.translation.total}${data.translation.running ? "（生成中）" : ""}${data.translation.error ? ` · ${data.translation.error}` : ""}`;
  }
  if (data.items.length) {
    const fromCollection = data.items.filter(item => item.selection_source === "collection").length;
    papersStatus.textContent += ` · 合集 ${fromCollection} 篇，补选 ${data.items.length - fromCollection} 篇`;
  }
}
function publicFeed(view, label, validate, render) {
  let value = null, timer = null, request = null, revision = 0, authFailed = false;
  const status = document.getElementById(`${view}-status`), button = document.getElementById(`${view}-refresh`);
  const visible = () => currentView === view && !document.hidden;
  const stopTimer = () => { if (timer !== null) clearTimeout(timer); timer = null; };
  function schedule() {
    stopTimer();
    if (visible() && !authFailed && value?.enabled !== false) timer = setTimeout(() => { timer = null; refresh(); }, 60000);
  }
  function pause() { stopTimer(); ++revision; request?.controller.abort(); }
  async function refresh({ manual = false } = {}) {
    if (!visible() || authFailed && !manual) return;
    stopTimer();
    if (request) { request.queued = true; return; }
    if (manual) authFailed = false;
    const selectedRevision = ++revision, controller = new AbortController();
    const pending = { controller, queued: false }; request = pending;
    button.disabled = true; status.className = "news-status"; status.textContent = `正在读取${label}…`;
    let timeout;
    try {
      const deadline = new Promise((_, reject) => {
        controller.signal.addEventListener("abort", () => reject(Object.assign(new Error(`${label}读取已取消`), { name: "AbortError" })), { once: true });
        timeout = setTimeout(() => { reject(new Error(`读取${label}超时，请重试`)); controller.abort(); }, 10000);
      });
      const data = validate(await Promise.race([api(`/api/${view}`, { signal: controller.signal }), deadline]));
      if (selectedRevision !== revision || !visible()) return;
      value = data; render(data);
    } catch (error) {
      if (selectedRevision !== revision || !visible()) return;
      if ([401, 403].includes(error.status)) authFailed = true;
      if (error.name !== "AbortError") {
        status.className = "news-status error";
        status.textContent = `${error.message}${value ? `；下方为上次读取的${label}。` : ""}`;
      }
    } finally {
      clearTimeout(timeout);
      if (request === pending) {
        request = null; button.disabled = false;
        if (pending.queued && visible() && !authFailed) refresh(); else schedule();
      }
    }
  }
  button.addEventListener("click", () => refresh({ manual: true }));
  return { syncVisibility() { if (visible()) refresh(); else pause(); } };
}
const publicFeeds = [publicFeed("news", "资讯", validateNews, renderNews), publicFeed("papers", "论文", validatePapers, renderPapers)];
function selectView(value) {
  if (!viewNames.includes(value) || currentView === value) return;
  currentView = value; document.body.dataset.view = value;
  for (const view of viewNames) {
    const selected = view === value;
    const tab = document.getElementById(`view-${view}`);
    tab.setAttribute("aria-selected", String(selected)); tab.tabIndex = selected ? 0 : -1;
    document.getElementById(`${view}-view`).hidden = !selected;
  }
  document.getElementById("chat-sidebar").hidden = value !== "chat";
  if (value === "chat") refreshAgentStatus(); else pauseAgentStatus();
  for (const feed of publicFeeds) feed.syncVisibility();
}
for (const view of viewNames) {
  const tab = document.getElementById(`view-${view}`);
  tab.addEventListener("click", () => selectView(view));
  tab.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const direction = event.key === "ArrowLeft" ? -1 : 1;
    const selected = event.key === "Home" ? viewNames[0] : event.key === "End" ? viewNames.at(-1)
      : viewNames[(viewNames.indexOf(view) + direction + viewNames.length) % viewNames.length];
    selectView(selected); document.getElementById(`view-${selected}`).focus();
  });
}

restoreBackend(); welcome(); loadBackends(); loadSession(); refreshAgentStatus();
