// Small DOM helpers. Text is only ever set through textContent, never innerHTML.
export const $ = (selector, root = document) => root.querySelector(selector);

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function applyTheme() {
  let theme = "system";
  try { theme = localStorage.getItem("theme") || "system"; } catch { /* storage may be blocked */ }
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

export function toast(message, ms = 4000) {
  let region = $(".toast-region");
  if (!region) {
    region = el("div", { class: "toast-region", role: "status", "aria-live": "polite" });
    document.body.append(region);
  }
  const node = el("div", { class: "toast", text: message });
  region.append(node);
  setTimeout(() => node.remove(), ms);
}

export function setBusy(button, busy, label) {
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
  if (label !== undefined) button.textContent = label;
}

export function showAlert(container, message, kind = "error") {
  container.replaceChildren();
  if (!message) return;
  container.append(el("div", { class: `alert alert-${kind}`, role: kind === "error" ? "alert" : "status", text: message }));
}
