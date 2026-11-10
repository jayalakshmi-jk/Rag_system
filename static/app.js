import { $, applyTheme, setBusy, showAlert, toast } from "./ui.js";
import { getClient, getSession, signOut } from "./auth.js";
import { api, ApiError } from "./api.js";
import { emit, on, setQuota, state } from "./state.js";
import { initDocuments, loadDocuments } from "./documents.js";
import { initConversations, loadConversations } from "./conversations.js";
import { initChat } from "./chat.js";

applyTheme();
const THEMES = ["system", "light", "dark"];
const currentTheme = () => { try { return localStorage.getItem("theme") || "system"; } catch { return "system"; } };
const renderThemeButton = () => { $("#theme-btn").textContent = `Theme: ${currentTheme()}`; };

$("#theme-btn").addEventListener("click", () => {
  const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
  try { localStorage.setItem("theme", next); } catch { /* ignore */ }
  applyTheme(); renderThemeButton();
});

async function leaveToLogin() {
  try { await signOut(); } catch { /* already signed out */ }
  location.replace("/login");
}

// ---- panels: right tabs and mobile navigation
for (const tab of document.querySelectorAll("#right-tabs [data-tab]")) {
  tab.addEventListener("click", () => {
    for (const other of document.querySelectorAll("#right-tabs [data-tab]")) other.setAttribute("aria-selected", String(other === tab));
    $("#tab-docs").hidden = tab.dataset.tab !== "docs";
    $("#tab-source").hidden = tab.dataset.tab !== "source";
    if (tab.dataset.tab === "source") setView("right");
  });
}
function setView(view) {
  $("#workspace").dataset.view = view;
  for (const button of document.querySelectorAll(".mobile-nav button")) button.setAttribute("aria-current", String(button.dataset.view === view));
}
for (const button of document.querySelectorAll(".mobile-nav button")) button.addEventListener("click", () => setView(button.dataset.view));
on("conversation-opened", () => setView("chat"));
on("open-conversation", () => setView("chat"));
on("new-chat", () => setView("chat"));

// ---- account dialog and deletion
const accountDialog = $("#account-dialog");
$("#account-btn").addEventListener("click", () => {
  const { me } = state;
  $("#account-name").textContent = [me.name, me.email].filter(Boolean).join(" · ");
  $("#account-quota").textContent = `${me.quota.remaining} of ${me.quota.daily_limit} questions left today.`;
  accountDialog.showModal();
});
$("#account-close").addEventListener("click", () => accountDialog.close());
$("#signout-btn").addEventListener("click", leaveToLogin);

const deleteDialog = $("#delete-dialog");
const confirmInput = $("#delete-confirm");
const submit = $("#delete-submit");
$("#delete-btn").addEventListener("click", () => {
  accountDialog.close();
  confirmInput.value = ""; submit.disabled = true; showAlert($("#delete-alert"), "");
  deleteDialog.showModal(); confirmInput.focus();
});
$("#delete-cancel").addEventListener("click", () => deleteDialog.close());
confirmInput.addEventListener("input", () => { submit.disabled = confirmInput.value !== "DELETE"; });
$("#delete-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (confirmInput.value !== "DELETE") return;
  setBusy(submit, true, "Deleting…");
  try {
    await api("/me", { method: "DELETE", body: { confirm: "DELETE" } });
    deleteDialog.close();
    try { await signOut(); } catch { /* account is gone anyway */ }
    location.replace("/login");
  } catch (error) {
    const message = error instanceof ApiError && error.status === 429
      ? "Too many attempts. Try again later." : "Could not delete the account. Nothing was changed, please try again.";
    showAlert($("#delete-alert"), message);
    setBusy(submit, false, "Delete forever");
  }
});

// ---- start
async function start() {
  const session = await getSession();
  if (!session) { location.replace("/login"); return; }
  const client = await getClient();
  client.auth.onAuthStateChange((event) => { if (event === "SIGNED_OUT") location.replace("/login"); });
  try {
    state.me = await api("/me");
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) { await leaveToLogin(); return; }
    $("#loading").textContent = "Could not load your account. Please refresh.";
    return;
  }
  renderThemeButton();
  initDocuments(); initConversations(); initChat();
  $("#loading").hidden = true;
  $("#shell").hidden = false;
  setQuota(state.me.quota);
  await Promise.allSettled([loadDocuments(), loadConversations()]);
  emit("documents");
}

start().catch(() => { $("#loading").textContent = "Could not start the app. Please refresh."; });
