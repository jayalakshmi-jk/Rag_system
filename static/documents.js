import { $, el, toast } from "./ui.js";
import { api, ApiError, upload } from "./api.js";
import { emit, on, readyDocuments, state } from "./state.js";

const STAGES = { uploading: "Uploading", parsing: "Reading file", chunking: "Splitting text",
  embedding: "Understanding content", indexing: "Indexing", ready: "Ready", failed: "Failed" };
let pollTimer = null;
const list = () => $("#doc-list");

export async function loadDocuments() {
  const { documents } = await api("/documents");
  state.documents = documents;
  const ids = new Set(documents.filter((d) => d.status === "ready").map((d) => d.id));
  for (const id of [...state.selected]) if (!ids.has(id)) state.selected.delete(id);
  render();
  emit("documents");
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (state.documents.some((d) => d.status === "processing")) {
    pollTimer = setTimeout(() => loadDocuments().catch(() => schedulePoll()), 1500);
  }
}

const size = (bytes) => (bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`);

function statusLine(doc) {
  if (doc.status === "processing") return `${STAGES[doc.processing_stage] ?? "Processing"}… ${doc.processing_progress}%`;
  if (doc.status === "failed") return doc.error || "Processing failed.";
  const pages = doc.page_count ? `${doc.page_count} page${doc.page_count === 1 ? "" : "s"}` : "";
  return [pages, doc.file_size ? size(doc.file_size) : ""].filter(Boolean).join(" · ");
}

function item(doc) {
  const ready = doc.status === "ready";
  const checkbox = el("input", {
    type: "checkbox", id: `sel-${doc.id}`, checked: state.selected.has(doc.id), disabled: !ready,
    "aria-label": `Only ask about ${doc.title}`,
    onchange: (event) => {
      if (event.target.checked) state.selected.add(doc.id); else state.selected.delete(doc.id);
      emit("documents");
    },
  });
  const progress = doc.status === "processing"
    ? el("progress", { max: 100, value: doc.processing_progress, "aria-label": `Processing ${doc.title}` }) : null;
  return el("li", { class: `doc doc-${doc.status}`, "data-id": doc.id },
    checkbox,
    el("div", { class: "doc-main" },
      el("div", { class: "doc-title", title: doc.title, text: doc.title }),
      el("div", { class: `doc-meta${doc.status === "failed" ? " doc-error" : ""}`, role: doc.status === "failed" ? "alert" : null, text: statusLine(doc) }),
      progress),
    el("div", { class: "doc-actions" },
      el("button", { class: "icon-btn", type: "button", "aria-label": `Rename ${doc.title}`, title: "Rename", text: "✎", onclick: () => rename(doc) }),
      el("button", { class: "icon-btn", type: "button", "aria-label": `Delete ${doc.title}`, title: "Delete", text: "🗑", onclick: () => remove(doc) })));
}

export function render() {
  const docs = state.documents;
  list().replaceChildren(...docs.map(item));
  $("#doc-empty").hidden = docs.length > 0;
  const ready = readyDocuments().length;
  $("#doc-count").textContent = docs.length ? `${ready} of ${docs.length} ready` : "";
}

// ------------------------------------------------------------------ upload
function validate(file) {
  const ext = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  if (![".pdf", ".txt", ".md"].includes(ext)) return "Only PDF, TXT and MD files are supported.";
  const max = state.me.limits.max_upload_mb * 1048576;
  if (file.size > max) return `This file is larger than ${state.me.limits.max_upload_mb} MB.`;
  if (file.size === 0) return "This file is empty.";
  return null;
}

async function send(file, allowDuplicate = false) {
  const form = new FormData();
  form.append("file", file);
  if (allowDuplicate) form.append("allow_duplicate", "true");
  try {
    await upload("/documents", form);
    await loadDocuments();
  } catch (error) {
    if (error instanceof ApiError && error.code === "DUPLICATE_DOCUMENT") {
      if (await confirmDialog("This document already exists.", `“${file.name}” has the same content as a document you already uploaded. Upload it anyway?`, "Upload anyway")) {
        return send(file, true);
      }
      return undefined;
    }
    toast(error instanceof ApiError ? error.message : "Upload failed. Check your connection and try again.", 6000);
    return undefined;
  }
  return undefined;
}

export async function uploadFiles(files) {
  for (const file of files) {
    const problem = validate(file);
    if (problem) { toast(`${file.name}: ${problem}`, 6000); continue; }
    toast(`Uploading ${file.name}…`, 1500);
    await send(file);
  }
}

// ------------------------------------------------------------- rename/delete
export function confirmDialog(title, message, confirmLabel, danger = false) {
  return new Promise((resolve) => {
    const dialog = $("#confirm-dialog");
    $("#confirm-title").textContent = title;
    $("#confirm-message").textContent = message;
    const ok = $("#confirm-ok");
    ok.textContent = confirmLabel;
    ok.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
    const finish = (value) => { dialog.close(); ok.onclick = null; $("#confirm-cancel").onclick = null; resolve(value); };
    ok.onclick = () => finish(true);
    $("#confirm-cancel").onclick = () => finish(false);
    dialog.addEventListener("cancel", () => resolve(false), { once: true });
    dialog.showModal();
    $("#confirm-cancel").focus();
  });
}

export function promptDialog(title, value, maxLength) {
  return new Promise((resolve) => {
    const dialog = $("#prompt-dialog");
    $("#prompt-title").textContent = title;
    const input = $("#prompt-input");
    input.value = value; input.maxLength = maxLength;
    const form = $("#prompt-form");
    const finish = (result) => { dialog.close(); form.onsubmit = null; $("#prompt-cancel").onclick = null; resolve(result); };
    form.onsubmit = (event) => { event.preventDefault(); finish(input.value.trim() || null); };
    $("#prompt-cancel").onclick = () => finish(null);
    dialog.addEventListener("cancel", () => resolve(null), { once: true });
    dialog.showModal();
    input.focus(); input.select();
  });
}

async function rename(doc) {
  const title = await promptDialog("Rename document", doc.title, 200);
  if (!title || title === doc.title) return;
  try { await api(`/documents/${doc.id}`, { method: "PATCH", body: { title } }); await loadDocuments(); }
  catch { toast("Could not rename the document."); }
}

async function remove(doc) {
  if (!(await confirmDialog("Delete document?", `“${doc.title}” and its indexed content will be permanently deleted. Past answers that cited it will lose their source preview.`, "Delete", true))) return;
  try {
    await api(`/documents/${doc.id}`, { method: "DELETE" });
    state.selected.delete(doc.id);
    await loadDocuments();
    toast("Document deleted.");
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) await loadDocuments();
    else toast("Could not delete the document.");
  }
}

export function initDocuments() {
  const input = $("#file-input");
  $("#upload-btn").addEventListener("click", () => input.click());
  $("#doc-empty-upload").addEventListener("click", () => input.click());
  input.addEventListener("change", async () => { const files = [...input.files]; input.value = ""; await uploadFiles(files); });
  const zone = $("#dropzone");
  for (const name of ["dragenter", "dragover"]) zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.add("drag"); });
  for (const name of ["dragleave", "drop"]) zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.remove("drag"); });
  zone.addEventListener("drop", (event) => uploadFiles([...event.dataTransfer.files]));
  on("documents", () => { /* selection or list changed elsewhere */ });
}
