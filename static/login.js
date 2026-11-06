import { $, applyTheme, setBusy, showAlert } from "./ui.js";
import { friendlyAuthError, getSession, sendPasswordReset, signIn, signInWithGoogle, signUp } from "./auth.js";

applyTheme();
const alertBox = $("#alert");
const tabs = { signin: $("#tab-signin"), signup: $("#tab-signup") };
const forms = { signin: $("#form-signin"), signup: $("#form-signup") };

function selectTab(name) {
  for (const key of Object.keys(tabs)) {
    tabs[key].setAttribute("aria-selected", String(key === name));
    tabs[key].tabIndex = key === name ? 0 : -1;
    forms[key].hidden = key !== name;
  }
  showAlert(alertBox, "");
}
tabs.signin.addEventListener("click", () => selectTab("signin"));
tabs.signup.addEventListener("click", () => selectTab("signup"));
for (const key of Object.keys(tabs)) {
  tabs[key].addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      const next = key === "signin" ? "signup" : "signin";
      selectTab(next); tabs[next].focus();
    }
  });
}

function fieldError(input, message) {
  const holder = input.closest(".field").querySelector(".error");
  holder.textContent = message ?? "";
  input.setAttribute("aria-invalid", String(Boolean(message)));
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

async function guarded(button, label, work) {
  setBusy(button, true, "Please wait…");
  try { await work(); } catch (error) { showAlert(alertBox, friendlyAuthError(error)); }
  finally { setBusy(button, false, label); }
}

forms.signin.addEventListener("submit", async (event) => {
  event.preventDefault();
  const email = $("#signin-email"), password = $("#signin-password");
  fieldError(email, EMAIL_RE.test(email.value.trim()) ? "" : "Enter a valid email address.");
  fieldError(password, password.value ? "" : "Enter your password.");
  if (!EMAIL_RE.test(email.value.trim()) || !password.value) return;
  await guarded($("#signin-submit"), "Sign in", async () => {
    await signIn(email.value.trim(), password.value);
    location.assign("/app");
  });
});

forms.signup.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = $("#signup-name"), email = $("#signup-email"), password = $("#signup-password");
  const problems = {
    name: name.value.trim() ? "" : "Enter your name.",
    email: EMAIL_RE.test(email.value.trim()) ? "" : "Enter a valid email address.",
    password: password.value.length >= 8 ? "" : "Use at least 8 characters.",
  };
  fieldError(name, problems.name); fieldError(email, problems.email); fieldError(password, problems.password);
  if (Object.values(problems).some(Boolean)) return;
  await guarded($("#signup-submit"), "Create account", async () => {
    const { needsConfirmation } = await signUp(name.value.trim(), email.value.trim(), password.value);
    if (needsConfirmation) {
      selectTab("signin");
      showAlert(alertBox, "Account created. Check your email and confirm your address, then sign in.", "ok");
    } else location.assign("/app");
  });
});

for (const button of document.querySelectorAll("[data-google]")) {
  button.addEventListener("click", () => guarded(button, button.dataset.label, signInWithGoogle));
}

$("#forgot").addEventListener("click", async () => {
  const email = $("#signin-email");
  if (!EMAIL_RE.test(email.value.trim())) {
    fieldError(email, "Enter your email above first.");
    email.focus();
    return;
  }
  fieldError(email, "");
  try {
    await sendPasswordReset(email.value.trim());
    showAlert(alertBox, "If an account exists for that email, a reset link is on its way.", "ok");
  } catch (error) { showAlert(alertBox, friendlyAuthError(error)); }
});

// Already signed in? Skip the form.
getSession().then((session) => { if (session) location.replace("/app"); }).catch(() => {});
if (location.hash === "#signup") selectTab("signup");
