// Minimal, safe Markdown -> DOM. Never uses innerHTML. Supports paragraphs, lists, headings,
// fenced code, **bold**, *italic*, `code`, and [n] citation markers (rendered as buttons).
import { el } from "./ui.js";

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|\[\d{1,2}\])/g;

function inline(text, onCite) {
  const nodes = [];
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("**")) nodes.push(el("strong", { text: token.slice(2, -2) }));
    else if (token.startsWith("`")) nodes.push(el("code", { text: token.slice(1, -1) }));
    else if (token.startsWith("[")) {
      const number = Number(token.slice(1, -1));
      const handled = onCite?.(number);
      nodes.push(handled ?? token);
    } else nodes.push(el("em", { text: token.slice(1, -1) }));
    last = match.index + token.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

export function renderMarkdown(text, { onCite } = {}) {
  const root = document.createDocumentFragment();
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    if (line.trim().startsWith("```")) {
      const code = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith("```")) code.push(lines[i++]);
      i += 1;
      root.append(el("pre", {}, el("code", { text: code.join("\n") })));
      continue;
    }
    const heading = /^#{1,6}\s+(.*)$/.exec(line);
    if (heading) { root.append(el("h4", {}, inline(heading[1], onCite))); i += 1; continue; }
    const bullet = /^\s*[-*•]\s+/.test(line);
    const numbered = /^\s*\d+[.)]\s+/.test(line);
    if (bullet || numbered) {
      const items = [];
      const pattern = bullet ? /^\s*[-*•]\s+(.*)$/ : /^\s*\d+[.)]\s+(.*)$/;
      while (i < lines.length && pattern.test(lines[i])) {
        items.push(el("li", {}, inline(pattern.exec(lines[i])[1], onCite)));
        i += 1;
      }
      root.append(el(bullet ? "ul" : "ol", {}, items));
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(\s*[-*•]\s+|\s*\d+[.)]\s+|#{1,6}\s|```)/.test(lines[i])) {
      para.push(lines[i++]);
    }
    const nodes = [];
    para.forEach((l, index) => { if (index) nodes.push(el("br")); nodes.push(...inline(l, onCite)); });
    root.append(el("p", {}, nodes));
  }
  return root;
}
