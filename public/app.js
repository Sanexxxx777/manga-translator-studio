/* Manga Translation Studio — UI controller */

const $ = (id) => document.getElementById(id);

const els = {
  form: $('job-form'),
  input: $('url-input'),
  runBtn: $('run-btn'),
  statusPill: $('status-pill'),
  progressCard: $('progress-card'),
  progressMeta: $('progress-meta'),
  chapterIdLabel: $('chapter-id-label'),
  pagesGrid: $('pages-grid'),
  pagesEmpty: $('pages-empty'),
  pagesCounter: $('pages-counter'),
  logStream: $('log-stream'),
  copyLogBtn: $('copy-log-btn'),
  resultBar: $('result-bar'),
  resultSummary: $('result-summary'),
  cbzDownload: $('cbz-download'),
  openFolder: $('open-folder'),
  historyList: $('history-list'),
  historyEmpty: $('history-empty'),
};

let currentEventSource = null;
let currentJobStart = 0;

// ───────────── helpers ─────────────

function setStatus(s, text) {
  els.statusPill.className = `pill pill-${s}`;
  els.statusPill.textContent = text || s;
}

function appendLog(line) {
  const span = document.createElement('span');
  let cls = 'log-info';
  const lower = line.toLowerCase();
  if (lower.includes('error') || lower.includes('traceback')) cls = 'log-error';
  else if (lower.includes('warn')) cls = 'log-warn';
  else if (line.startsWith('[') && line.includes(']')) cls = 'log-meta';
  span.className = cls;
  span.textContent = line + '\n';
  els.logStream.appendChild(span);
  els.logStream.scrollTop = els.logStream.scrollHeight;
}

function addPage(file, url) {
  // Идемпотентно: если такой thumb уже есть — не дублируем
  if (els.pagesGrid.querySelector(`[data-file="${file}"]`)) return;

  els.pagesEmpty.style.display = 'none';
  const num = (file.match(/(\d+)/) || ['', '?'])[1];
  const a = document.createElement('div');
  a.className = 'page-thumb';
  a.dataset.url = url;
  a.dataset.file = file;
  a.innerHTML = `
    <img loading="lazy" alt="страница ${num}" />
    <div class="page-num">${num}</div>
  `;
  a.onclick = () => openLightbox(url);
  els.pagesGrid.appendChild(a);
  els.pagesCounter.textContent = els.pagesGrid.children.length;

  // Retry-логика на случай race-condition: SSE-event 'page' может прилететь
  // раньше чем сервер успеет отдать файл через статику.
  const img = a.querySelector('img');
  let attempts = 0;
  const tryLoad = () => {
    attempts += 1;
    img.src = url + (attempts > 1 ? `?r=${attempts}` : '');
  };
  img.onerror = () => {
    if (attempts < 5) setTimeout(tryLoad, 400 * attempts);
  };
  tryLoad();
}

function openLightbox(url) {
  let lb = document.querySelector('.lightbox');
  if (!lb) {
    lb = document.createElement('div');
    lb.className = 'lightbox';
    lb.innerHTML = '<img />';
    lb.onclick = () => lb.classList.remove('open');
    document.body.appendChild(lb);
  }
  lb.querySelector('img').src = url;
  lb.classList.add('open');
}

function fmtMs(ms) {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} с`;
  return `${Math.floor(s/60)} мин ${s % 60} с`;
}

function fmtTime(ts) {
  const d = new Date(ts);
  const today = new Date(); today.setHours(0,0,0,0);
  const dt = new Date(ts); dt.setHours(0,0,0,0);
  const sameDay = dt.getTime() === today.getTime();
  const hh = d.getHours().toString().padStart(2,'0');
  const mm = d.getMinutes().toString().padStart(2,'0');
  if (sameDay) return `сегодня ${hh}:${mm}`;
  return `${d.getDate().toString().padStart(2,'0')}.${(d.getMonth()+1).toString().padStart(2,'0')} ${hh}:${mm}`;
}

// ───────────── job lifecycle ─────────────

async function runJob(url) {
  // reset UI
  els.runBtn.disabled = true;
  els.runBtn.querySelector('.btn-label').textContent = 'Запуск…';
  els.progressCard.hidden = false;
  els.resultBar.hidden = true;
  els.cbzDownload.hidden = true;
  els.openFolder.hidden = true;
  els.pagesGrid.innerHTML = '';
  els.pagesEmpty.style.display = '';
  els.pagesCounter.textContent = '0';
  els.logStream.innerHTML = '';
  setStatus('running', 'running');

  let job;
  try {
    const r = await fetch('/api/jobs', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || `HTTP ${r.status}`);
    }
    job = await r.json();
  } catch (e) {
    setStatus('failed', 'failed');
    appendLog(`[ui] ошибка запуска: ${e.message}`);
    els.runBtn.disabled = false;
    els.runBtn.querySelector('.btn-label').textContent = 'Перевести';
    return;
  }

  els.chapterIdLabel.textContent = job.chapterId.slice(0, 8) + '…';
  els.openFolder.href = `/chapters/${job.chapterId}/`;
  currentJobStart = Date.now();
  els.progressMeta.textContent = 'Запущено только что';

  // Подписка на SSE
  const es = new EventSource(`/api/jobs/${job.id}/stream`);
  currentEventSource = es;

  es.addEventListener('hello', () => {});
  es.addEventListener('log',   (ev) => { try { appendLog(JSON.parse(ev.data).line); } catch {} });
  es.addEventListener('page',  (ev) => { try { const p = JSON.parse(ev.data); addPage(p.file, p.url); els.progressMeta.textContent = `Готово страниц: ${els.pagesGrid.children.length} · идёт ${fmtMs(Date.now()-currentJobStart)}`; } catch {} });
  es.addEventListener('done',  (ev) => {
    try {
      const d = JSON.parse(ev.data);
      setStatus(d.status, d.status);
      els.runBtn.disabled = false;
      els.runBtn.querySelector('.btn-label').textContent = 'Перевести';
      els.resultBar.hidden = false;
      els.openFolder.hidden = false;
      if (d.cbz) {
        els.cbzDownload.href = d.cbz;
        els.cbzDownload.download = '';
        els.cbzDownload.hidden = false;
      }
      els.resultSummary.innerHTML = d.status === 'done'
        ? `Готово · <strong>${d.pages}</strong> страниц за <strong>${fmtMs(d.durationMs)}</strong>`
        : `Прерывание (exit ${d.exitCode}) · ${d.pages} стр., см. лог.`;
      els.progressMeta.textContent = `Финиш: ${fmtMs(d.durationMs)}`;
    } catch {}
    es.close();
    refreshHistory();
  });
  es.onerror = () => {
    appendLog('[ui] SSE-соединение прервалось — обновить страницу для переподключения');
    els.runBtn.disabled = false;
    els.runBtn.querySelector('.btn-label').textContent = 'Перевести';
  };
}

els.form.addEventListener('submit', (e) => {
  e.preventDefault();
  const url = els.input.value.trim();
  if (url) runJob(url);
});

els.copyLogBtn?.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(els.logStream.innerText);
    els.copyLogBtn.textContent = 'copied';
    setTimeout(() => (els.copyLogBtn.textContent = 'copy'), 1500);
  } catch {}
});

// ───────────── history ─────────────

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

const expandedHistory = new Set();   // chapterId-ы для которых раскрыта мини-галерея

async function refreshHistory() {
  try {
    const r = await fetch('/api/jobs');
    const { history = [] } = await r.json();
    els.historyList.innerHTML = '';
    if (history.length === 0) {
      els.historyEmpty.style.display = '';
      return;
    }
    els.historyEmpty.style.display = 'none';
    for (const h of history) {
      const wrap = document.createElement('div');
      wrap.className = 'history-item';

      const row = document.createElement('div');
      row.className = 'history-row' + (h.status === 'done' && h.pages ? ' clickable' : '');
      const stat = h.status === 'done' ? `<span class="pill pill-done">done</span>`
                 : h.status === 'failed' ? `<span class="pill pill-failed">failed</span>`
                 : `<span class="pill pill-running">running</span>`;
      const dur = h.durationMs ? fmtMs(h.durationMs) : '—';

      // meta заголовок: "Vol.7 Ch.52 · Otaku ni Yasashii Gal wa Inai!?" или fallback id
      const titleText = h.metaLabel || `${h.chapterId.slice(0,8)}…`;

      row.innerHTML = `
        <div class="hr-time">${fmtTime(h.started)}</div>
        <div class="hr-title" title="${escapeHtml(h.url)}">
          ${escapeHtml(titleText)}
          ${h.metaLabel ? `<span class="hr-id-mono">${h.chapterId.slice(0,8)}…</span>` : ''}
        </div>
        <div class="hr-stat">${stat}</div>
        <div class="hr-pages">${h.pages || 0} стр · ${dur}</div>
        <div class="hr-actions">
          <a class="ghost-btn" href="/chapters/${h.chapterId}/" target="_blank" onclick="event.stopPropagation()">Папка</a>
          ${h.cbz ? `<a class="ghost-btn" href="${h.cbz}" onclick="event.stopPropagation()">CBZ</a>` : ''}
        </div>`;

      // Клик по строке → раскрыть мини-галерею под ней
      const expander = document.createElement('div');
      expander.className = 'history-expander';
      if (h.status === 'done' && h.pages) {
        row.onclick = () => toggleHistoryExpand(h.chapterId, expander);
        if (expandedHistory.has(h.chapterId)) {
          expander.classList.add('open');                 // ← восстанавливаем при refresh
          loadHistoryThumbs(h.chapterId, expander);
        }
      }

      wrap.appendChild(row);
      wrap.appendChild(expander);
      els.historyList.appendChild(wrap);
    }
  } catch {}
}

async function toggleHistoryExpand(chapterId, container) {
  if (expandedHistory.has(chapterId)) {
    expandedHistory.delete(chapterId);
    container.innerHTML = '';
    container.classList.remove('open');
    delete container.dataset.loaded;     // ← без сброса второй клик не перезагружал thumbs
    return;
  }
  expandedHistory.add(chapterId);
  container.classList.add('open');
  await loadHistoryThumbs(chapterId, container);
}

async function loadHistoryThumbs(chapterId, container) {
  if (container.dataset.loaded) return;
  container.innerHTML = '<div class="muted small">загружаю…</div>';
  try {
    const r = await fetch(`/api/chapters/${chapterId}`);
    const { pages = [] } = await r.json();
    if (!pages.length) { container.innerHTML = '<div class="muted small">страниц нет</div>'; return; }
    container.innerHTML = '';
    const grid = document.createElement('div');
    grid.className = 'history-thumbs';
    for (const p of pages) {
      const num = (p.match(/\d+/) || ['?'])[0];
      const url = `/chapters/${chapterId}/${p}`;
      const a = document.createElement('div');
      a.className = 'page-thumb';
      a.innerHTML = `<img src="${url}" loading="lazy" alt="page ${num}"><div class="page-num">${num}</div>`;
      a.onclick = (e) => { e.stopPropagation(); openLightbox(url); };
      grid.appendChild(a);
    }
    container.appendChild(grid);
    container.dataset.loaded = '1';
  } catch (e) {
    container.innerHTML = `<div class="muted small">ошибка: ${escapeHtml(e.message || e)}</div>`;
  }
}

refreshHistory();
setInterval(refreshHistory, 10000);
