import { $, applyTheme, setBusy, showAlert, toast } from "./ui.js";
import { getClient, getSession, signOut } from "./auth.js";
import { api, ApiError } from "./api.js";

applyTheme();
const THEMES = ["system", "light", "dark"];

function currentTheme() {
  try { return localStorage.getItem("theme") || "system"; } catch { return "system"; }
}
function renderThemeButton() { $("#theme-btn").textContent = `Theme: ${currentTheme()}`; }
$("#theme-btn").addEventListener("click", () => {
  const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
  try { localStorage.setItem("theme", next); } catch { /* ignore */ }
  applyTheme(); renderThemeButton();
});

async function leaveToLogin() {
  try { await signOut(); } catch { /* already signed out */ }
  location.replace("/login");
}

$("#signout-btn").addEventListener("click", leaveToLogin);

function renderMe(me) {
  $("#user-label").textContent = me.name || me.email || "";
  $("#quota").textContent = `${me.quota.remaining} of ${me.quota.daily_limit} questions left today.`;
}

async function start() {
  const session = await getSession();
  if (!session) { location.replace("/login"); return; }
  const client = await getClient();
  client.auth.onAuthStateChange((event) => { if (event === "SIGNED_OUT") location.replace("/login"); });
  try {
    renderMe(await api("/me"));
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) { await leaveToLogin(); return; }
    $("#loading").textContent = "Could not load your account. Please refresh.";
    return;
  }
  renderThemeButton();
  $("#loading").hidden = true;
  $("#shell").hidden = false;
}

// Delete-account flow ------------------------------------------------------
const dialog = $("#delete-dialog");
const confirmInput = $("#delete-confirm");
const submit = $("#delete-submit");

$("#delete-btn").addEventListener("click", () => {
  confirmInput.value = ""; submit.disabled = true; showAlert($("#delete-alert"), "");
  dialog.showModal(); confirmInput.focus();
});
$("#delete-cancel").addEventListener("click", () => dialog.close());
confirmInput.addEventListener("input", () => { submit.disabled = confirmInput.value !== "DELETE"; });
$("#delete-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (confirmInput.value !== "DELETE") return;
  setBusy(submit, true, "Deleting…");
  try {
    await api("/me", { method: "DELETE", body: { confirm: "DELETE" } });
    dialog.close();
    try { await signOut(); } catch { /* account is gone anyway */ }
    location.replace("/login");
  } catch (error) {
    const message = error instanceof ApiError && error.status === 429
      ? "Too many attempts. Try again later." : "Could not delete the account. Nothing was changed, please try again.";
    showAlert($("#delete-alert"), message);
    setBusy(submit, false, "Delete forever");
    toast("Account was not deleted.");
  }
});

start().catch(() => { $("#loading").textContent = "Could not start the app. Please refresh."; });
