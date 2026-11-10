import { $, el } from "./ui.js";
import { api, ApiError } from "./api.js";

const words = (text) => text.split(/\s+/).filter(Boolean);
const escapeRe = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// Find the cited chunk inside the (whitespace-differing) page text so it can be highlighted.
export function locate(page, snippet) {
  const w = words(snippet);
  if (w.length < 3) return null;
  const head = new RegExp(w.slice(0, Math.min(8, w.length)).map(escapeRe).join("\\s+")).exec(page);
  if (!head) return null;
  const tailWords = w.slice(-Math.min(8, w.length));
  const tail = new RegExp(tailWords.map(escapeRe).join("\\s+")).exec(page.slice(head.index));
  const end = tail ? head.index + tail.index + tail[0].length : head.index + head[0].length;
  return [head.index, end];
}

export async function showSource(source) {
  $("#right-tabs [data-tab=source]").click();
  const body = $("#source-body");
  const title = $("#source-title");
  title.textContent = source.title + (source.page_no ? ` · page ${source.page_no}` : "");
  body.replaceChildren(el("p", { class: "muted", text: "Loading…" }));
  try {
    const page = source.page_no
      ? await api(`/documents/${source.document_id}/pages/${source.page_no}`)
      : null;
    if (!page) throw new ApiError(404, "NOT_FOUND", "");
    const span = locate(page.content, source.snippet ?? "");
    const content = el("div", { class: "page-text" });
    if (span) {
      const mark = el("mark", { text: page.content.slice(span[0], span[1]) });
      content.append(page.content.slice(0, span[0]), mark, page.content.slice(span[1]));
      body.replaceChildren(content);
      mark.scrollIntoView({ block: "center" });
    } else {
      content.textContent = page.content;
      body.replaceChildren(content);
    }
    if (page.page_count) $("#source-meta").textContent = `Page ${page.page_no} of ${page.page_count}`;
  } catch (error) {
    const gone = error instanceof ApiError && error.status === 404;
    body.replaceChildren(
      el("p", { class: "muted", text: gone ? "This document is no longer available. Showing the saved excerpt:" : "Could not load the page." }),
      source.snippet ? el("blockquote", { text: source.snippet }) : null);
  }
}
