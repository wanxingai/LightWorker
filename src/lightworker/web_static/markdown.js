(() => {
  "use strict";

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function safeHref(value) {
    const href = String(value || "").trim();
    const normalized = href.replace(/[\u0000-\u001f\u007f\s]/g, "").toLowerCase();
    if (
      normalized.startsWith("https://") ||
      normalized.startsWith("http://") ||
      normalized.startsWith("mailto:") ||
      normalized.startsWith("#") ||
      normalized.startsWith("/") && !normalized.startsWith("//") ||
      normalized.startsWith("./") ||
      normalized.startsWith("../")
    ) {
      return href;
    }
    return "#";
  }

  function renderInline(source) {
    const tokens = [];
    const token = (html) => {
      const marker = `\u0000LWMD${tokens.length}\u0000`;
      tokens.push(html);
      return marker;
    };
    let text = String(source || "").replaceAll("\u0000", "");

    text = text.replace(/`([^`\n]+)`/g, (_, code) => token(`<code>${escapeHtml(code)}</code>`));
    text = text.replace(/<((?:https?:\/\/|mailto:)[^ >]+)>/gi, (_, url) => {
      const href = safeHref(url);
      const external = /^https?:\/\//i.test(href);
      return token(
        `<a href="${escapeHtml(href)}"${external ? ' target="_blank" rel="noopener noreferrer"' : ""}>${escapeHtml(url)}</a>`,
      );
    });
    text = text.replace(/\[([^\]\n]+)\]\(([^)\s]+)(?:\s+["']([^"']*)["'])?\)/g, (_, label, url, title) => {
      const href = safeHref(url);
      const external = /^https?:\/\//i.test(href);
      const titleAttribute = title ? ` title="${escapeHtml(title)}"` : "";
      return token(
        `<a href="${escapeHtml(href)}"${titleAttribute}${external ? ' target="_blank" rel="noopener noreferrer"' : ""}>${renderInline(label)}</a>`,
      );
    });
    text = text.replace(/https?:\/\/[^\s<>\u0000]+/gi, (rawUrl) => {
      const url = rawUrl.replace(/[),.;:!?，。；：！？）】]+$/g, "");
      const suffix = rawUrl.slice(url.length);
      const href = safeHref(url);
      return `${token(`<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a>`)}${suffix}`;
    });

    text = escapeHtml(text);
    text = text
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_]+)__/g, "<strong>$1</strong>")
      .replace(/~~([^~]+)~~/g, "<del>$1</del>")
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=$|[\s).,!?:;，。！？：；])/g, "$1<em>$2</em>")
      .replace(/(^|[\s(])_([^_\n]+)_(?=$|[\s).,!?:;，。！？：；])/g, "$1<em>$2</em>");
    return text.replace(/\u0000LWMD(\d+)\u0000/g, (_, index) => tokens[Number(index)] || "");
  }

  function splitTableRow(line) {
    let value = String(line).trim();
    if (value.startsWith("|")) value = value.slice(1);
    if (value.endsWith("|")) value = value.slice(0, -1);
    return value.split(/(?<!\\)\|/).map((cell) => cell.replaceAll("\\|", "|").trim());
  }

  function isTableDelimiter(line) {
    const cells = splitTableRow(line);
    return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
  }

  function beginsBlock(lines, index) {
    const line = lines[index] || "";
    const next = lines[index + 1] || "";
    return (
      /^\s*(```|~~~)/.test(line) ||
      /^\s{0,3}#{1,6}\s+/.test(line) ||
      /^\s{0,3}>/.test(line) ||
      /^\s{0,3}(?:[-+*]|\d+[.)])\s+/.test(line) ||
      /^\s{0,3}(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$/.test(line) ||
      line.includes("|") && isTableDelimiter(next)
    );
  }

  function renderMarkdown(source) {
    const lines = String(source || "").replaceAll("\r\n", "\n").replaceAll("\r", "\n").split("\n");
    const blocks = [];
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }

      const fence = line.match(/^\s*(```|~~~)\s*([\w+-]*)\s*$/);
      if (fence) {
        const marker = fence[1];
        const language = fence[2].replace(/[^\w+-]/g, "");
        const code = [];
        index += 1;
        while (index < lines.length && !new RegExp(`^\\s*${marker}`).test(lines[index])) {
          code.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        const className = language ? ` class="language-${escapeHtml(language)}"` : "";
        blocks.push(`<pre><code${className}>${escapeHtml(code.join("\n"))}</code></pre>`);
        continue;
      }

      const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (heading) {
        const level = heading[1].length;
        blocks.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
        index += 1;
        continue;
      }

      if (/^\s{0,3}(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$/.test(line)) {
        blocks.push("<hr>");
        index += 1;
        continue;
      }

      if (/^\s{0,3}>/.test(line)) {
        const quote = [];
        while (index < lines.length && /^\s{0,3}>/.test(lines[index])) {
          quote.push(lines[index].replace(/^\s{0,3}>\s?/, ""));
          index += 1;
        }
        blocks.push(`<blockquote>${renderMarkdown(quote.join("\n"))}</blockquote>`);
        continue;
      }

      if (line.includes("|") && index + 1 < lines.length && isTableDelimiter(lines[index + 1])) {
        const headers = splitTableRow(line);
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
          rows.push(splitTableRow(lines[index]));
          index += 1;
        }
        const head = headers.map((cell) => `<th>${renderInline(cell)}</th>`).join("");
        const body = rows
          .map((row) => `<tr>${headers.map((_, cellIndex) => `<td>${renderInline(row[cellIndex] || "")}</td>`).join("")}</tr>`)
          .join("");
        blocks.push(`<div class="markdown-table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
        continue;
      }

      const listItem = line.match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/);
      if (listItem) {
        const ordered = /^\d/.test(listItem[1]);
        const tag = ordered ? "ol" : "ul";
        const items = [];
        while (index < lines.length) {
          const match = lines[index].match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/);
          if (!match || /^\d/.test(match[1]) !== ordered) break;
          const checkbox = match[2].match(/^\[([ xX])\]\s+(.+)$/);
          if (checkbox) {
            const checked = checkbox[1].toLowerCase() === "x";
            items.push(`<li class="task-list-item"><input type="checkbox" disabled${checked ? " checked" : ""}> ${renderInline(checkbox[2])}</li>`);
          } else {
            items.push(`<li>${renderInline(match[2])}</li>`);
          }
          index += 1;
        }
        blocks.push(`<${tag}>${items.join("")}</${tag}>`);
        continue;
      }

      const paragraph = [line];
      index += 1;
      while (index < lines.length && lines[index].trim() && !beginsBlock(lines, index)) {
        paragraph.push(lines[index]);
        index += 1;
      }
      blocks.push(`<p>${paragraph.map((item) => renderInline(item)).join("<br>")}</p>`);
    }

    return blocks.join("\n");
  }

  function setContent(element, source) {
    if (!element) return;
    const markdown = String(source || "");
    element.dataset.rawMarkdown = markdown;
    element.innerHTML = renderMarkdown(markdown);
  }

  window.LightWorkerMarkdown = Object.freeze({ render: renderMarkdown, setContent });
})();
