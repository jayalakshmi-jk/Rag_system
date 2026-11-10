import { $, el, toast } from "./ui.js";
import { api, ApiError, stream } from "./api.js";
import { renderMarkdown } from "./markdown.js";
import { emit, on, readyDocuments, setQuota, state } from "./state.js";
import { loadConversations } from "./conversations.js";
import { showSource } from "./sources.js";

let messages = [];
let controller = null;
let pendingFrame = 0;
const FEEDBACK_REASONS = [["incorrect_answer", "Incorrect answer"], ["wrong_citation", "Wrong citation"], ["missing_information", "Missing information"], ["too_long", "Too long"], ["didnt_understand", "Didn't understand me"], ["other", "Other"]];

const log = () => $("#messages");
const nearBottom = () => { const e = log(); return e.scrollHeight - e.scrollTop - e.clientHeight < 120; };
const scrollDown = () => { log().scrollTop = log().scrollHeight; };

// --------------------------------------------------------------- rendering
function citeButton(message) {
  return (n) => {
    const source = message.sources?.find((s) => s.n === n);
    if (!source) return null;
    return el("button", { class: "cite", type: "button", text: String(n), "aria-label": `Source ${n}: ${source.title}${source.page_no ? `, page ${source.page_no}` : ""}`, onclick: () => showSource(source) });
  };
}

function sourceChips(message) {
  const seen = new Set();
  const chips = [];
  for (const s of message.sources ?? []) {
    const key = `${s.document_id}:${s.page_no}`;
    if (seen.has(key)) continue;
    seen.add(key);
    chips.push(el("button", { class: "chip", type: "button", text: `${s.title}${s.page_no ? ` · p.${s.page_no}` : ""}`, onclick: () => showSource(s) }));
  }
  return chips.length ? el("div", { class: "source-chips", "aria-label": "Sources" }, el("span", { class: "muted", text: "Sources:" }), chips) : null;
}

function actions(message, isLast) {
  if (message.streaming || !message.id) return null;
  const rate = (rating) => () => setFeedback(message, rating);
  return el("div", { class: "answer-actions" },
    el("button", { class: "icon-btn", type: "button", title: "Copy answer", "aria-label": "Copy answer", text: "⧉", onclick: async () => { try { await navigator.clipboard.writeText(message.content); toast("Copied."); } catch { toast("Could not copy."); } } }),
    el("button", { class: `icon-btn${message.feedback === "up" ? " on" : ""}`, type: "button", title: "Good answer", "aria-label": "Good answer", "aria-pressed": String(message.feedback === "up"), text: "👍", onclick: rate("up") }),
    el("button", { class: `icon-btn${message.feedback === "down" ? " on" : ""}`, type: "button", title: "Bad answer", "aria-label": "Bad answer", "aria-pressed": String(message.feedback === "down"), text: "👎", onclick: rate("down") }),
    isLast ? el("button", { class: "icon-btn", type: "button", title: "Regenerate", "aria-label": "Regenerate answer", text: "↻", onclick: regenerate }) : null);
}

function bubble(message, index) {
  if (message.role === "user") return el("div", { class: "msg msg-user" }, el("div", { class: "bubble", text: message.content }));
  const body = el("div", { class: "bubble prose" });
  if (message.error) {
    body.append(el("p", { class: "msg-error", role: "alert", text: message.error }),
      el("button", { class: "btn", type: "button", text: "Try again", onclick: regenerate }));
  } else if (message.streaming && !message.content) {
    body.append(el("span", { class: "typing", role: "status", "aria-label": "Thinking" }, el("i"), el("i"), el("i")));
  } else {
    body.append(renderMarkdown(message.content, { onCite: citeButton(message) }));
  }
  const last = index === messages.length - 1;
  return el("div", { class: "msg msg-assistant", "data-id": message.id ?? "" }, body,
    message.error ? null : sourceChips(message), actions(message, last && !state.streaming),
    last && message.suggestions?.length && !state.streaming
      ? el("div", { class: "suggestions" }, message.suggestions.map((q) => el("button", { class: "chip", type: "button", text: q, onclick: () => ask(q) }))) : null);
}

export function renderMessages() {
  const stick = nearBottom();
  const hasReady = readyDocuments().length > 0;
  $("#empty-state").hidden = messages.length > 0;
  $("#onboarding").hidden = hasReady;
  $("#starter").hidden = !hasReady;
  log().replaceChildren(...messages.map(bubble));
  if (stick) scrollDown();
  updateComposer();
}

function scheduleRender() {
  if (pendingFrame) return;
  pendingFrame = requestAnimationFrame(() => { pendingFrame = 0; renderMessages(); });
}

function updateComposer() {
  const input = $("#question");
  const ready = readyDocuments().length > 0;
  $("#send-btn").hidden = state.streaming;
  $("#stop-btn").hidden = !state.streaming;
  $("#send-btn").disabled = !ready || !input.value.trim();
  input.disabled = false;
  input.placeholder = ready ? "Ask about your documents…" : "Upload a document to start asking questions";
  const selected = state.selected.size;
  const total = readyDocuments().length;
  $("#scope").textContent = !total ? "No documents ready" : selected ? `Asking ${selected} selected document${selected === 1 ? "" : "s"}` : `Asking all ${total} document${total === 1 ? "" : "s"}`;
  const remaining = state.me?.quota?.remaining;
  $("#quota-pill").textContent = remaining === undefined ? "" : `${remaining} question${remaining === 1 ? "" : "s"} left today`;
  $("#quota-pill").classList.toggle("low", remaining !== undefined && remaining <= 5);
}

// ----------------------------------------------------------------- actions
function errorText(error) {
  if (error instanceof ApiError) {
    if (error.code === "QUOTA_EXCEEDED") return error.message;
    return error.message;
  }
  return "Network problem. Check your connection and try again.";
}

export async function ask(question) {
  if (state.streaming) return;
  if (!readyDocuments().length) { toast("Upload a document and wait until it is ready."); return; }
  const user = { role: "user", content: question };
  const assistant = { role: "assistant", content: "", streaming: true, sources: [], suggestions: [] };
  messages.push(user, assistant);
  state.streaming = true;
  controller = new AbortController();
  renderMessages(); scrollDown();
  const body = {
    message: question,
    conversation_id: state.activeId,
    document_ids: state.selected.size ? [...state.selected] : null,
    language: $("#language").value, style: $("#style").value,
  };
  const wasNew = state.activeId === null;
  let retrieved = [];
  try {
    for await (const { event, data } of stream("/chat", body, { signal: controller.signal })) {
      if (event === "meta") { state.activeId = data.conversation_id; }
      else if (event === "sources") { retrieved = data.sources; assistant.sources = retrieved; }
      else if (event === "token") { assistant.content += data.text; scheduleRender(); }
      else if (event === "suggestions") { assistant.suggestions = data.suggestions; }
      else if (event === "error") {
        assistant.error = data.message;
        // The server removes a brand-new conversation when nothing was answered.
        if (!assistant.content && wasNew) state.activeId = null;
        assistant.content = "";
      }
      else if (event === "done") {
        assistant.id = data.message_id;
        assistant.sources = retrieved.filter((s) => data.cited.includes(s.n));
        setQuota(data.quota);
      }
    }
  } catch (error) {
    if (error.name === "AbortError") assistant.stopped = true;
    else {
      assistant.error = errorText(error);
      // A rejected request (quota, validation) never created messages: drop the question.
      if (error instanceof ApiError) { messages.pop(); messages.pop(); messages.push({ role: "assistant", content: "", error: assistant.error, sources: [] }); }
    }
  } finally {
    assistant.streaming = false;
    state.streaming = false;
    controller = null;
    renderMessages();
    loadConversations().catch(() => {});
    $("#question").focus();
  }
}

function regenerate() {
  const lastUser = [...messages].reverse().find((m) => m.role === "user");
  if (!lastUser || state.streaming) return;
  // Remove the failed/previous answer from view; the server keeps history consistent.
  while (messages.length && messages[messages.length - 1].role === "assistant") messages.pop();
  if (messages.length && messages[messages.length - 1] === lastUser) messages.pop();
  ask(lastUser.content);
}

async function setFeedback(message, rating) {
  try {
    if (message.feedback === rating) {
      await api(`/messages/${message.id}/feedback`, { method: "DELETE" });
      message.feedback = null;
    } else {
      let reason = null;
      if (rating === "down") reason = await pickReason();
      await api(`/messages/${message.id}/feedback`, { method: "PUT", body: { rating, reason } });
      message.feedback = rating;
      toast("Thanks for the feedback.");
    }
    renderMessages();
  } catch { toast("Could not save feedback."); }
}

function pickReason() {
  return new Promise((resolve) => {
    const dialog = $("#reason-dialog");
    const select = $("#reason-select");
    select.replaceChildren(el("option", { value: "", text: "Choose a reason (optional)" }), ...FEEDBACK_REASONS.map(([v, t]) => el("option", { value: v, text: t })));
    const finish = (value) => { dialog.close(); resolve(value || null); };
    $("#reason-form").onsubmit = (event) => { event.preventDefault(); finish(select.value); };
    $("#reason-skip").onclick = () => finish(null);
    dialog.addEventListener("cancel", () => resolve(null), { once: true });
    dialog.showModal();
    select.focus();
  });
}

export function newChat() {
  if (state.streaming) controller?.abort();
  state.activeId = null;
  messages = [];
  renderMessages();
  $("#question").focus();
}

export async function openConversation(id) {
  if (state.streaming) controller?.abort();
  try {
    const detail = await api(`/conversations/${id}`);
    state.activeId = id;
    messages = detail.messages.map((m) => ({ id: m.role === "assistant" ? m.id : undefined, role: m.role, content: m.content, sources: m.sources ?? [], feedback: m.feedback }));
    renderMessages(); scrollDown();
    emit("conversation-opened");
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) { toast("That conversation no longer exists."); newChat(); await loadConversations(); }
    else toast("Could not open the conversation.");
  }
}

export function initChat() {
  const input = $("#question");
  const fit = () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 180)}px`; updateComposer(); };
  input.addEventListener("input", fit);
  input.maxLength = state.me.limits.max_question_length;
  const submit = () => {
    const text = input.value.trim();
    if (!text || state.streaming) return;
    input.value = ""; fit();
    ask(text);
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); submit(); }
  });
  $("#composer").addEventListener("submit", (event) => { event.preventDefault(); submit(); });
  $("#stop-btn").addEventListener("click", () => controller?.abort());
  for (const key of ["language", "style"]) {
    const select = $(`#${key}`);
    try { const saved = localStorage.getItem(`pref-${key}`); if (saved && [...select.options].some((o) => o.value === saved)) select.value = saved; } catch { /* ignore */ }
    select.addEventListener("change", () => { try { localStorage.setItem(`pref-${key}`, select.value); } catch { /* ignore */ } });
  }
  for (const button of document.querySelectorAll("[data-starter]")) button.addEventListener("click", () => ask(button.dataset.starter));
  on("documents", renderMessages);
  on("quota", updateComposer);
  on("new-chat", newChat);
  on("open-conversation", openConversation);
  renderMessages();
}
