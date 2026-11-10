import { $, el, toast } from "./ui.js";
import { api, ApiError } from "./api.js";
import { emit, state } from "./state.js";
import { confirmDialog, promptDialog } from "./documents.js";

let searchTimer = null;

function groupOf(iso) {
  const day = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diff = Math.round((day(new Date()) - day(new Date(iso))) / 86400000);
  if (diff <= 0) return "Today";
  if (diff === 1) return "Yesterday";
  if (diff <= 7) return "Previous 7 days";
  return "Older";
}

export async function loadConversations() {
  const q = $("#conv-search").value.trim();
  const { conversations } = await api(`/conversations${q ? `?q=${encodeURIComponent(q)}` : ""}`);
  state.conversations = conversations;
  render();
}

function render() {
  const holder = $("#conv-list");
  holder.replaceChildren();
  const searching = $("#conv-search").value.trim() !== "";
  $("#conv-empty").hidden = state.conversations.length > 0;
  $("#conv-empty").textContent = searching ? "No conversations match your search." : "Your conversations will appear here.";
  let currentGroup = null;
  let ul = null;
  for (const conv of state.conversations) {
    const group = groupOf(conv.updated_at);
    if (group !== currentGroup) {
      currentGroup = group;
      holder.append(el("h3", { class: "group-title", text: group }));
      ul = el("ul", { class: "conv-group" });
      holder.append(ul);
    }
    const active = conv.id === state.activeId;
    ul.append(el("li", { class: `conv${active ? " active" : ""}` },
      el("button", { class: "conv-open", type: "button", "aria-current": active ? "true" : null, title: conv.title ?? "", text: conv.title || "New chat", onclick: () => emit("open-conversation", conv.id) }),
      el("button", { class: "icon-btn", type: "button", "aria-label": `Rename ${conv.title}`, title: "Rename", text: "✎", onclick: () => rename(conv) }),
      el("button", { class: "icon-btn", type: "button", "aria-label": `Delete ${conv.title}`, title: "Delete", text: "🗑", onclick: () => remove(conv) })));
  }
}

async function rename(conv) {
  const title = await promptDialog("Rename conversation", conv.title ?? "", 120);
  if (!title || title === conv.title) return;
  try { await api(`/conversations/${conv.id}`, { method: "PATCH", body: { title } }); await loadConversations(); }
  catch { toast("Could not rename the conversation."); }
}

async function remove(conv) {
  if (!(await confirmDialog("Delete conversation?", "This conversation and its messages will be permanently deleted.", "Delete", true))) return;
  try { await api(`/conversations/${conv.id}`, { method: "DELETE" }); }
  catch (error) { if (!(error instanceof ApiError && error.status === 404)) { toast("Could not delete the conversation."); return; } }
  if (state.activeId === conv.id) emit("new-chat");
  await loadConversations();
}

export function initConversations() {
  $("#conv-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadConversations().catch(() => toast("Search failed.")), 300);
  });
  $("#new-chat").addEventListener("click", () => emit("new-chat"));
}

export { render as renderConversations };
