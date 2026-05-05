/**
 * Manga Translation Studio — Express server поверх Python-pipeline.
 *
 * UX:
 *   POST /api/jobs   — старт перевода (передаём URL/UUID главы)
 *   GET  /api/jobs/:id/stream  — SSE-стрим логов и прогресса
 *   GET  /api/jobs              — история последних N
 *   GET  /chapters/:id/page/:n  — отдать переведённую страницу
 *   GET  /chapters/:id.cbz      — отдать собранный CBZ
 */

const express = require('express');
const compression = require('compression');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const crypto = require('crypto');

const ROOT = __dirname;
const PORT = process.env.PORT || 3017;
const PYTHON = path.join(ROOT, 'venv', 'bin', 'python');
const PIPELINE = path.join(ROOT, 'pipeline.py');
const OUTPUT_DIR = path.join(ROOT, 'output');
const INPUT_DIR = path.join(ROOT, 'input');
const JOBS_LOG = path.join(ROOT, 'data', 'jobs.json');

if (!fs.existsSync(path.join(ROOT, 'data'))) fs.mkdirSync(path.join(ROOT, 'data'));
if (!fs.existsSync(JOBS_LOG)) fs.writeFileSync(JOBS_LOG, JSON.stringify({ jobs: [] }));

const CHAPTER_RE = /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;

// ─── meta helpers ───────────────────────────────────────────────────────────
function readChapterMeta(chapterId) {
  // Ищем .meta.json в input/ (download кладёт сюда) и output/ (как кеш).
  for (const base of [INPUT_DIR, OUTPUT_DIR]) {
    const p = path.join(base, chapterId, '.meta.json');
    if (fs.existsSync(p)) {
      try { return JSON.parse(fs.readFileSync(p, 'utf-8')); } catch {}
    }
  }
  return null;
}

function metaLabel(meta) {
  // "Vol.7 Ch.52 · Otaku ni Yasashii Gal wa Inai!?"
  if (!meta) return null;
  const parts = [];
  if (meta.volume)       parts.push(`Vol.${meta.volume}`);
  if (meta.chapter)      parts.push(`Ch.${meta.chapter}`);
  const head = parts.join(' ');
  const title = meta.manga_title || '';
  if (head && title) return `${head} · ${title}`;
  return head || title || null;
}

// ───────────────────────────── in-memory job registry ─────────────────────────────

const jobs = new Map();   // id → { id, chapterId, url, status, lines:[], started, finished, exitCode, subscribers:Set<res> }

function loadJobsHistory() {
  try { return JSON.parse(fs.readFileSync(JOBS_LOG, 'utf-8')).jobs || []; } catch { return []; }
}
function saveJobsHistory(arr) {
  fs.writeFileSync(JOBS_LOG, JSON.stringify({ jobs: arr.slice(-50) }, null, 2));
}

function broadcast(job, event, data) {
  const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const res of job.subscribers) {
    try { res.write(payload); } catch {}
  }
}

function startJob(rawInput) {
  const m = rawInput.match(CHAPTER_RE);
  if (!m) throw new Error('UUID главы не распознан в строке');
  const chapterId = m[1].toLowerCase();
  const id = crypto.randomBytes(6).toString('hex');

  const job = {
    id,
    chapterId,
    url: rawInput.trim(),
    status: 'running',
    lines: [],
    pages: [],
    started: Date.now(),
    finished: null,
    exitCode: null,
    subscribers: new Set(),
  };
  jobs.set(id, job);

  // Запуск python pipeline.py <url>
  const env = { ...process.env, PYTHONUNBUFFERED: '1' };
  const child = spawn(PYTHON, ['-u', PIPELINE, rawInput], {
    cwd: ROOT,
    env,
  });
  job.pid = child.pid;

  // Параллельно следим за появлением output/<chapterId>/*.png и шлём событие 'page'
  const chapterOutDir = path.join(OUTPUT_DIR, chapterId);
  let knownPages = new Set();
  const pageWatcher = setInterval(() => {
    if (!fs.existsSync(chapterOutDir)) return;
    for (const f of fs.readdirSync(chapterOutDir)) {
      if (!/^page_\d+\.\w+$/.test(f) || knownPages.has(f)) continue;
      knownPages.add(f);
      job.pages.push(f);
      broadcast(job, 'page', { file: f, url: `/chapters/${chapterId}/${f}` });
    }
  }, 1000);

  const onLine = (line) => {
    if (!line.trim()) return;
    job.lines.push(line);
    if (job.lines.length > 5000) job.lines.shift();
    broadcast(job, 'log', { line });
  };

  let stdoutBuf = '';
  child.stdout.on('data', (chunk) => {
    stdoutBuf += chunk.toString();
    let i;
    while ((i = stdoutBuf.indexOf('\n')) >= 0) {
      onLine(stdoutBuf.slice(0, i));
      stdoutBuf = stdoutBuf.slice(i + 1);
    }
  });
  let stderrBuf = '';
  child.stderr.on('data', (chunk) => {
    stderrBuf += chunk.toString();
    let i;
    while ((i = stderrBuf.indexOf('\n')) >= 0) {
      onLine(stderrBuf.slice(0, i));
      stderrBuf = stderrBuf.slice(i + 1);
    }
  });

  child.on('close', (code) => {
    clearInterval(pageWatcher);
    if (stdoutBuf) onLine(stdoutBuf);
    if (stderrBuf) onLine(stderrBuf);
    job.status = code === 0 ? 'done' : 'failed';
    job.exitCode = code;
    job.finished = Date.now();
    const cbz = path.join(OUTPUT_DIR, `${chapterId}.cbz`);
    job.cbzReady = fs.existsSync(cbz);
    // Скопировать meta из input/ в output/ чтобы /api/chapters/:id и /chapters/:id/ её увидели
    const metaSrc = path.join(INPUT_DIR, chapterId, '.meta.json');
    const metaDst = path.join(OUTPUT_DIR, chapterId, '.meta.json');
    if (fs.existsSync(metaSrc) && !fs.existsSync(metaDst) && fs.existsSync(path.dirname(metaDst))) {
      try { fs.copyFileSync(metaSrc, metaDst); } catch {}
    }
    job.meta = readChapterMeta(chapterId);
    broadcast(job, 'done', {
      exitCode: code,
      status: job.status,
      cbz: job.cbzReady ? `/chapters/${chapterId}.cbz` : null,
      pages: job.pages.length,
      durationMs: job.finished - job.started,
      meta: job.meta,
      metaLabel: metaLabel(job.meta),
    });
    // close all SSE
    for (const res of job.subscribers) { try { res.end(); } catch {} }
    job.subscribers.clear();

    // Persist to history
    const hist = loadJobsHistory();
    hist.push({
      id: job.id,
      chapterId: job.chapterId,
      url: job.url,
      status: job.status,
      pages: job.pages.length,
      cbz: job.cbzReady ? `/chapters/${chapterId}.cbz` : null,
      started: job.started,
      finished: job.finished,
      durationMs: job.finished - job.started,
      meta: job.meta || readChapterMeta(chapterId),
    });
    saveJobsHistory(hist);
  });

  return job;
}

// ──────────────────────────────── Express ────────────────────────────────

const app = express();
app.use(compression());
app.use(express.json());

// статика UI — у нас один HTML/CSS/JS, не страшно кешировать кратко
app.use(express.static(path.join(ROOT, 'public'), { maxAge: '5m' }));

// Статика результатов перевода (страницы манги, CBZ).
// ВАЖНО: maxAge=0 + Cache-Control: no-cache.
// При повторном переводе главы файлы перезаписываются на диске,
// а браузер показывал бы СТАРУЮ закешированную версию → читатель видит
// результат прошлого прогона. no-cache заставляет браузер делать условный
// запрос (If-Modified-Since): если файл не менялся, сервер вернёт 304 и
// браузер использует кеш (быстро); если менялся — отдаст новый файл.
app.use('/chapters', express.static(OUTPUT_DIR, {
  maxAge: 0,
  etag: true,
  lastModified: true,
  setHeaders: (res) => {
    res.setHeader('Cache-Control', 'no-cache, must-revalidate');
  },
}));

// HTML-галерея для папки главы (когда жмут "Открыть папку")
app.get('/chapters/:id/', (req, res) => {
  const id = req.params.id.toLowerCase();
  if (!CHAPTER_RE.test(id)) return res.status(400).send('invalid id');
  const dir = path.join(OUTPUT_DIR, id);
  if (!fs.existsSync(dir)) return res.status(404).send('not found');
  const pages = fs.readdirSync(dir).filter(f => /^page_\d+\.\w+$/.test(f)).sort();
  const cbz = fs.existsSync(path.join(OUTPUT_DIR, `${id}.cbz`)) ? `/chapters/${id}.cbz` : null;
  const meta = readChapterMeta(id);
  const ml = metaLabel(meta);
  const headerLine = ml || `Глава ${id.slice(0,8)}…`;
  const subLine = `${pages.length} страниц · клик — крупный размер${ml ? ' · id ' + id.slice(0,8) + '…' : ''}`;
  res.set('Cache-Control', 'no-store').type('html').send(`<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><title>${id.slice(0,8)} — manga-tr</title>
<link rel="stylesheet" href="/design-system/colors_and_type.css">
<link rel="stylesheet" href="/styles.css">
<link rel="stylesheet" href="/design-system-overlay.css">
<style>
.gallery-app { max-width: 1180px; margin: 0 auto; padding: 28px 24px 80px; display:flex; flex-direction:column; gap:20px; }
.gallery-head { display:flex; justify-content:space-between; align-items:center; gap:16px; flex-wrap:wrap; }
.gallery-head h1 { margin:0; font-size:24px; }
.gallery-pages { display:grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap:14px; }
.gallery-pages a { display:block; aspect-ratio:3/4; border-radius:10px; overflow:hidden; border:1px solid var(--color-line); background:rgba(0,0,0,.4); position:relative; transition:transform .1s, border-color .1s; }
.gallery-pages a:hover { transform: translateY(-2px); border-color: var(--color-line-strong); }
.gallery-pages img { width:100%; height:100%; object-fit:cover; display:block; }
.gallery-pages .pn { position:absolute; left:8px; bottom:8px; background:rgba(0,0,0,.75); color:#fff; padding:3px 8px; border-radius:5px; font-family:var(--nfa-font-mono,monospace); font-size:11px; }
</style></head>
<body><div class="gallery-app">
  <div class="gallery-head">
    <div>
      <a href="/" class="muted small" style="text-decoration:none">← на главную</a>
      <h1 class="page-title">${headerLine}</h1>
      <div class="muted small">${subLine}</div>
    </div>
    <div style="display:flex; gap:10px;">
      ${cbz ? `<a class="primary-btn" href="${cbz}">Скачать CBZ</a>` : ''}
    </div>
  </div>
  <div class="gallery-pages">
    ${pages.map(p => {
      const n = (p.match(/\d+/) || ['?'])[0];
      return `<a href="/chapters/${id}/${p}" target="_blank"><img src="/chapters/${id}/${p}" loading="lazy" alt="page ${n}"><span class="pn">${n}</span></a>`;
    }).join('\n    ')}
  </div>
</div></body></html>`);
});

app.post('/api/jobs', (req, res) => {
  const url = String(req.body.url || '').trim();
  if (!url) return res.status(400).json({ error: 'Не передан url/uuid' });
  try {
    const job = startJob(url);
    res.json({ id: job.id, chapterId: job.chapterId, status: job.status });
  } catch (e) {
    res.status(400).json({ error: e.message });
  }
});

app.get('/api/jobs/:id/stream', (req, res) => {
  const job = jobs.get(req.params.id);
  if (!job) return res.status(404).end();

  res.set({
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  res.write(`event: hello\ndata: ${JSON.stringify({ id: job.id, chapterId: job.chapterId, status: job.status })}\n\n`);

  // отдаём существующий буфер логов
  for (const line of job.lines) res.write(`event: log\ndata: ${JSON.stringify({ line })}\n\n`);
  for (const file of job.pages) res.write(`event: page\ndata: ${JSON.stringify({ file, url: `/chapters/${job.chapterId}/${file}` })}\n\n`);

  if (job.status !== 'running') {
    res.write(`event: done\ndata: ${JSON.stringify({ exitCode: job.exitCode, status: job.status, cbz: job.cbzReady ? `/chapters/${job.chapterId}.cbz` : null, pages: job.pages.length, durationMs: (job.finished || Date.now()) - job.started })}\n\n`);
    return res.end();
  }

  job.subscribers.add(res);
  req.on('close', () => job.subscribers.delete(res));
});

app.get('/api/jobs', (_req, res) => {
  // Для записей где meta не сохранена (старые job'ы) — попробуем подтянуть из файла
  const enriched = loadJobsHistory().map(h => {
    if (!h.meta) h.meta = readChapterMeta(h.chapterId);
    h.metaLabel = metaLabel(h.meta);
    return h;
  });
  res.json({ history: enriched.slice(-20).reverse() });
});

// Список страниц для уже готовой главы (используется при reload)
app.get('/api/chapters/:id', (req, res) => {
  const id = req.params.id.toLowerCase();
  if (!CHAPTER_RE.test(id)) return res.status(400).json({ error: 'invalid id' });
  const dir = path.join(OUTPUT_DIR, id);
  const meta = readChapterMeta(id);
  if (!fs.existsSync(dir)) return res.json({ pages: [], cbz: null, meta, metaLabel: metaLabel(meta) });
  const pages = fs.readdirSync(dir).filter(f => /^page_\d+\.\w+$/.test(f)).sort();
  const cbz = fs.existsSync(path.join(OUTPUT_DIR, `${id}.cbz`)) ? `/chapters/${id}.cbz` : null;
  res.json({ pages, cbz, meta, metaLabel: metaLabel(meta) });
});

app.listen(PORT, () => {
  console.log(`[manga-tr] http://127.0.0.1:${PORT}`);
});
