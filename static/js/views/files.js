/**
 * views/files.js — Generated files panel + file viewer modal.
 */

import { store, metaFor } from "../state/store.js";
import { escHtml } from "../util/format.js";

const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]);

function _formatBytes(bytes) {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

// ── Registry sync ─────────────────────────────────────────────────────────────

export function syncFilesFromServer(files) {
  store.fileRegistry.length = 0;
  const sorted = [...files].sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
  sorted.forEach(f => store.fileRegistry.push(f));
  store.fileCount = store.fileRegistry.length;
  renderFilesList();
}

export function addFileToRegistry(file, animate = true, openCardDrawerFn) {
  const existing = store.fileRegistry.find(f => f.path === file.path);
  if (existing) {
    Object.assign(existing, file);
    store.fileRegistry.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
    renderFilesList();
    return;
  }

  store.fileRegistry.unshift(file);
  store.fileRegistry.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
  store.fileCount = store.fileRegistry.length;

  const countEl = document.getElementById("files-count");
  if (countEl) countEl.textContent = String(store.fileCount);

  const empty = document.getElementById("files-empty");
  if (empty) empty.style.display = "none";

  const list  = document.getElementById("files-list");
  const entry = _buildFileEntry(file, animate, openCardDrawerFn);
  if (list) list.insertBefore(entry, list.firstChild);
}

export function renderFilesList(openCardDrawerFn) {
  const list    = document.getElementById("files-list");
  const countEl = document.getElementById("files-count");
  const empty   = document.getElementById("files-empty");
  if (!list) return;

  list.querySelectorAll(".file-entry").forEach(r => r.remove());
  if (countEl) countEl.textContent = String(store.fileCount);

  if (store.fileCount === 0) {
    if (empty) empty.style.display = "";
    return;
  }
  if (empty) empty.style.display = "none";
  store.fileRegistry.forEach(f => list.appendChild(_buildFileEntry(f, false, openCardDrawerFn)));
}

function _buildFileEntry(file, animate, openCardDrawerFn) {
  const ext        = (file.ext || "").toLowerCase();
  const isImage    = IMAGE_EXTS.has(ext);
  const isMarkdown = ext === ".md";
  const isHtml     = ext === ".html" || ext === ".htm";
  const icon = isImage ? "🖼️" : (isMarkdown ? "📝" : (isHtml ? "🌐" : "📄"));

  const sizeStr = _formatBytes(file.size || 0);
  const timeStr = (file.ts || "").split(" ")[1] || file.ts || "";

  const entry = document.createElement("div");
  entry.className = "file-entry";
  if (animate) entry.style.animation = "fadeIn 0.2s ease";
  entry.onclick = () => openFileViewer(file.path, file.agent);
  entry.title   = file.path;

  const agentLabel = metaFor(file.agent).label;
  const cardChip   = file.card_id && openCardDrawerFn
    ? `<span class="file-card-chip" title="open producing card" data-card-id="${escHtml(file.card_id)}">↳ ${escHtml(file.card_id)}</span>`
    : "";

  entry.innerHTML = `
    <span class="file-icon">${icon}</span>
    <div class="file-info">
      <div class="file-name">${escHtml(file.filename || "")}</div>
      <div class="file-meta">
        <span class="agent-badge badge-${file.agent}" style="font-size:9px;padding:1px 4px">${agentLabel}</span>
        ${escHtml(sizeStr)} · ${escHtml(timeStr)}
        ${cardChip}
      </div>
    </div>
  `;

  if (file.card_id && openCardDrawerFn) {
    entry.querySelector(".file-card-chip")?.addEventListener("click", (e) => {
      e.stopPropagation();
      openCardDrawerFn(file.card_id);
    });
  }
  return entry;
}

// ── File viewer modal ─────────────────────────────────────────────────────────

export function openFileViewer(path, agent) {
  const modal   = document.getElementById("file-modal");
  const body    = document.getElementById("modal-body");
  const fname   = document.getElementById("modal-filename");
  const badge   = document.getElementById("modal-badge");
  const meta    = document.getElementById("modal-meta");
  const dl      = document.getElementById("modal-download");
  if (!modal) return;

  const filename = path.split("/").pop();
  const ext      = ("." + filename.split(".").pop()).toLowerCase();
  const isImage    = IMAGE_EXTS.has(ext);
  const isMarkdown = ext === ".md";
  const isHtml     = ext === ".html" || ext === ".htm";
  const modalBox   = modal.querySelector(".modal-box");

  if (fname)  fname.textContent  = filename;
  if (badge)  { badge.className = `agent-badge badge-${agent}`; badge.textContent = metaFor(agent).label; }
  if (meta)   meta.textContent   = "";
  if (dl)     { dl.href = `/api/file?path=${encodeURIComponent(path)}`; dl.download = filename; }
  if (modalBox) modalBox.classList.toggle("modal-box-html", isHtml);

  if (body) body.innerHTML = '<div class="modal-loading">Loading…</div>';
  modal.classList.add("open");

  const url = `/api/file?path=${encodeURIComponent(path)}`;

  if (isImage) {
    const img = document.createElement("img");
    img.className = "modal-image";
    img.onload  = () => { if (meta) meta.textContent = `${img.naturalWidth}×${img.naturalHeight}`; if (body) { body.innerHTML = ""; body.appendChild(img); } };
    img.onerror = () => { if (body) body.innerHTML = '<div class="modal-error">Failed to load image</div>'; };
    img.src = url;
  } else if (isHtml) {
    const iframe = document.createElement("iframe");
    iframe.className  = "modal-html-frame";
    iframe.title      = filename;
    iframe.sandbox    = "allow-same-origin allow-popups allow-popups-to-escape-sandbox";
    iframe.onload  = () => { if (meta) meta.textContent = "rendered HTML"; };
    iframe.onerror = () => { if (body) body.innerHTML = '<div class="modal-error">Failed to load HTML</div>'; };
    if (body) { body.innerHTML = ""; body.appendChild(iframe); }
    iframe.src = url;
  } else {
    fetch(url)
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.text(); })
      .then(text => {
        const byteLen = new TextEncoder().encode(text).length;
        const lines   = text.split("\n").length;
        if (isMarkdown && typeof marked !== "undefined") {
          if (meta) meta.textContent = `${lines} lines · ${_formatBytes(byteLen)} · rendered`;
          const div = document.createElement("div");
          div.className = "modal-markdown";
          div.innerHTML = marked.parse(text, { breaks: false, gfm: true });
          div.querySelectorAll("a").forEach(a => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
          if (body) { body.innerHTML = ""; body.appendChild(div); }
        } else {
          if (meta) meta.textContent = `${lines} lines · ${_formatBytes(byteLen)}`;
          const pre = document.createElement("pre");
          pre.className = "modal-text";
          pre.textContent = text;
          if (body) { body.innerHTML = ""; body.appendChild(pre); }
        }
      })
      .catch(err => { if (body) body.innerHTML = `<div class="modal-error">Error: ${escHtml(err.message)}</div>`; });
  }
}

export function closeFileViewer() {
  const modal = document.getElementById("file-modal");
  if (!modal) return;
  modal.classList.remove("open");
  const body = document.getElementById("modal-body");
  if (body) body.innerHTML = "";
  const modalBox = modal.querySelector(".modal-box");
  if (modalBox) modalBox.classList.remove("modal-box-html");
}

/** Wire backdrop click to close. */
export function setupFileModal() {
  document.getElementById("file-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "file-modal") closeFileViewer();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const modal = document.getElementById("file-modal");
      if (modal?.classList.contains("open")) closeFileViewer();
    }
  });
}
