const state = {
  summary: null,
  open: new Set(),
  cache: new Map(),
  filter: ""
};

const refsEl = document.querySelector("#refs");
const summaryEl = document.querySelector("#summary");
const filesEl = document.querySelector("#files");
const filterEl = document.querySelector("#filter");
const urlParams = new URLSearchParams(window.location.search);

function escapeAttr(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function statusKind(status) {
  return String(status || "").slice(0, 1);
}

function fileStats(file) {
  if (file.additions === null && file.deletions === null) return "binary";
  return `<span class="plus">+${file.additions ?? 0}</span> <span class="minus">-${file.deletions ?? 0}</span>`;
}

function renderSummary(data) {
  refsEl.textContent = `${data.baseLabel} (${data.baseSha}) -> ${data.headLabel} (${data.headSha})`;
  summaryEl.innerHTML = [
    `<span class="metric metric-files"><strong>${data.fileCount}</strong> files</span>`,
    `<span class="metric metric-additions"><strong>+${data.additions}</strong></span>`,
    `<span class="metric metric-deletions"><strong>-${data.deletions}</strong></span>`,
    `<span class="metric metric-repo" title="${escapeAttr(data.repoRoot)}">${escapeAttr(data.repoRoot)}</span>`
  ].join("");
}

function visibleFiles() {
  const needle = state.filter.trim().toLowerCase();
  if (!needle) return state.summary.files;
  return state.summary.files.filter(file => {
    return file.displayPath.toLowerCase().includes(needle) || file.path.toLowerCase().includes(needle);
  });
}

function renderFiles() {
  const files = visibleFiles();
  if (!files.length) {
    filesEl.innerHTML = `<div class="empty-state">No files match the current filter.</div>`;
    return;
  }

  filesEl.innerHTML = files.map(file => {
    const isOpen = state.open.has(file.id);
    return `
      <section class="file-entry" data-file-id="${file.id}" data-open="${isOpen ? "true" : "false"}">
        <button class="file-row" type="button" title="${escapeAttr(file.absolutePath)}" aria-expanded="${isOpen ? "true" : "false"}">
          <span class="chevron">›</span>
          <span class="status" data-kind="${statusKind(file.status)}">${escapeAttr(file.status)}</span>
          <span class="path">${escapeAttr(file.displayPath)}</span>
          <span class="file-stat">${fileStats(file)}</span>
        </button>
        ${isOpen ? `<div class="diff-panel" id="panel-${file.id}"><div class="panel-state">Loading diff</div></div>` : ""}
      </section>
    `;
  }).join("");

  for (const entry of filesEl.querySelectorAll(".file-entry")) {
    const id = Number(entry.dataset.fileId);
    entry.querySelector(".file-row").addEventListener("click", () => toggleFile(id));
    if (state.open.has(id)) {
      loadFile(id);
    }
  }
}

function toggleFile(id) {
  if (state.open.has(id)) {
    state.open.delete(id);
  } else {
    state.open.add(id);
  }
  renderFiles();
}

function sideRowHtml(row, side) {
  const cls = `diff-row-${row.kind}`;
  const lineNo = side === "old" ? row.oldLine ?? "" : row.newLine ?? "";
  const html = side === "old" ? row.oldHtml : row.newHtml;
  return `
    <div class="${cls} diff-line">
      <div class="line-no ${side}-no">${lineNo}</div>
      <div class="code-cell ${side}-cell"><span>${html}</span></div>
    </div>
  `;
}

function paneHtml(title, rows, side) {
  return `
    <section class="diff-pane diff-pane-${side}">
      <div class="diff-grid-head">${escapeAttr(title)}</div>
      <div class="diff-pane-body">
        ${rows.map(row => sideRowHtml(row, side)).join("")}
      </div>
    </section>
  `;
}

function renderDiff(file) {
  if (file.binary) {
    return `
      <div class="diff-meta">
        <span class="language">${escapeAttr(file.mimeType)}</span>
        <span>${file.oldSize} bytes -> ${file.newSize} bytes</span>
      </div>
      <div class="panel-state">Binary file changed.</div>
    `;
  }

  if (!file.rows.length) {
    return `
      <div class="diff-meta"><span class="language">${escapeAttr(file.language)}</span></div>
      <div class="panel-state">No textual diff for this file.</div>
    `;
  }

  return `
    <div class="diff-meta">
      <span class="language">${escapeAttr(file.language)}</span>
      <span>${file.rows.length} rendered rows</span>
    </div>
    <div class="diff-scroll">
      ${paneHtml(state.summary.baseLabel, file.rows, "old")}
      ${paneHtml(state.summary.headLabel, file.rows, "new")}
    </div>
  `;
}

async function loadFile(id) {
  const panel = document.querySelector(`#panel-${id}`);
  if (!panel) return;
  if (state.cache.has(id)) {
    panel.innerHTML = renderDiff(state.cache.get(id));
    return;
  }

  try {
    const response = await fetch(`/api/file/${id}`);
    if (!response.ok) throw new Error(await response.text());
    const payload = await response.json();
    state.cache.set(id, payload);
    const freshPanel = document.querySelector(`#panel-${id}`);
    if (freshPanel) freshPanel.innerHTML = renderDiff(payload);
  } catch (error) {
    panel.innerHTML = `<div class="panel-state">Failed to load diff: ${escapeAttr(error.message)}</div>`;
  }
}

filterEl.addEventListener("input", () => {
  state.filter = filterEl.value;
  renderFiles();
});

document.querySelector("#expandAll").addEventListener("click", () => {
  for (const file of visibleFiles()) state.open.add(file.id);
  renderFiles();
});

document.querySelector("#collapseAll").addEventListener("click", () => {
  state.open.clear();
  renderFiles();
});

async function boot() {
  try {
    const response = await fetch("/api/summary");
    if (!response.ok) throw new Error(await response.text());
    state.summary = await response.json();
    renderSummary(state.summary);
        if (!state.summary.files.length) {
          filesEl.innerHTML = `<div class="empty-state">No files changed in this comparison.</div>`;
          return;
        }
        if (urlParams.get("expand") === "all") {
          for (const file of state.summary.files) state.open.add(file.id);
        }
        renderFiles();
  } catch (error) {
    refsEl.textContent = "Unable to load comparison";
    filesEl.innerHTML = `<div class="empty-state">${escapeAttr(error.message)}</div>`;
  }
}

boot();
