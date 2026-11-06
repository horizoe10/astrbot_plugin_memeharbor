import { html, render, useEffect, useRef, useState } from './vendor/htm-preact.js';
import * as api from './api.js';
import { Icon, refreshShared } from './components.js';
import { Detail, changed, undo } from './detail.js';
import { fail, go, set, state, toast, useStore } from './store.js';
import { Home } from './views/home.js';
import { Insights } from './views/insights.js';
import { Library } from './views/library.js';
import { Similar } from './views/similar.js';
import { TagsPage } from './views/tags.js';
import { Triage } from './views/triage.js';

const PAGES = {
  home: { title: '首页', icon: 'home', view: Home },
  triage: { title: '整理', icon: 'triage', view: Triage, count: 'inbox' },
  library: { title: '图库', icon: 'grid', view: Library },
  tags: { title: '标签', icon: 'tag', view: TagsPage },
  insights: { title: '洞察', icon: 'chart', view: Insights },
  similar: { title: '相似图片', icon: 'copy', view: Similar, count: 'similar' },
};

function parseRoute() {
  const [path, query = ''] = location.hash.replace(/^#\/?/, '').split('?');
  return { page: PAGES[path] ? path : 'home', params: new URLSearchParams(query) };
}

/* ---------- import (buttons, drag and drop, folders) ---------- */
const IMAGE = /\.(png|jpe?g|gif|webp|bmp)$/i;
const importable = (file, path = file.name) => (IMAGE.test(file.name) || /\.zip$/i.test(file.name)) && !path.split('/').some((p) => p === '__MACOSX' || p.startsWith('._'));
async function importFiles(entries) {
  if (state.importing) return toast('正在导入上一批文件，请等它完成', 'info');
  const files = entries.filter(({ file, path }) => importable(file, path));
  const skipped = entries.length - files.length;
  if (!files.length) return entries.length && toast('没有可以导入的图片或 ZIP', 'error');
  const totals = { added: 0, duplicates: 0, failed: 0, skipped };
  const errors = [];
  try {
    for (let i = 0; i < files.length; i++) {
      const { file, path } = files[i];
      set({ importing: { done: i, total: files.length, name: path, added: totals.added } });
      try {
        const r = await api.upload('upload', file);
        totals.added += r.added; totals.duplicates += r.duplicates; totals.failed += r.failed; totals.skipped += r.skipped;
        (r.errors || []).forEach((e) => errors.length < 5 && errors.push(path + '：' + e));
      } catch (error) { totals.failed += 1; if (errors.length < 5) errors.push(path + '：' + error.message); }
    }
  } finally { set({ importing: null }); }
  toast('导入完成：新增 ' + totals.added + ' 张，已存在 ' + totals.duplicates + ' 张' + (totals.skipped ? '，跳过 ' + totals.skipped + ' 个非图片文件' : '') + (totals.failed ? '，失败 ' + totals.failed + ' 项：' + errors.join('；') : '') + (totals.added ? '。新图在「整理」里' : ''), totals.failed ? 'error' : 'ok');
  changed([]); refreshShared().catch(() => {}); startAnalysis();
}
async function readEntry(entry, prefix = '') {
  if (entry.isFile) return new Promise((resolve) => entry.file((file) => resolve([{ file, path: prefix + file.name }]), () => resolve([])));
  if (!entry.isDirectory) return [];
  const reader = entry.createReader(); const children = [];
  for (;;) { const batch = await new Promise((r) => reader.readEntries(r, () => r([]))); if (!batch.length) break; children.push(...batch); }
  return (await Promise.all(children.map((c) => readEntry(c, prefix + entry.name + '/')))).flat();
}

/* ---------- background analysis of older images (hash + size) ---------- */
let analysing = false;
async function startAnalysis() {
  if (analysing) return;
  analysing = true;
  try {
    for (;;) {
      const { pending } = await api.post('analyze');
      set({ analysis: pending });
      if (!pending) break;
      await new Promise((r) => setTimeout(r, 150));
    }
  } catch { /* retried on next visit */ } finally { analysing = false; }
}

/* ---------- command palette ---------- */
function commands(text) {
  const t = text.trim();
  const list = [
    { label: '打开首页', run: () => go('home') },
    { label: '开始整理新图', run: () => go('triage') },
    { label: '按系列整理', run: () => go('triage', { mode: 'clusters' }) },
    { label: '打开图库', run: () => go('library') },
    { label: '查看待整理的图', run: () => go('library', { view: 'inbox' }) },
    { label: '打开回收站', run: () => go('library', { view: 'trash' }) },
    { label: '管理标签', run: () => go('tags') },
    { label: '群聊洞察：大家在搜什么', run: () => go('insights') },
    { label: '查找相似图片', run: () => go('similar') },
    { label: '导入图片 / ZIP', run: () => window.dispatchEvent(new CustomEvent('mh:import', { detail: 'files' })) },
    { label: '导入整个文件夹', run: () => window.dispatchEvent(new CustomEvent('mh:import', { detail: 'folder' })) },
    { label: '导出备份', run: () => api.download('export', {}, 'memeharbor-backup.zip').then(() => toast('备份已导出')).catch(fail) },
    { label: '撤销上一次修改', run: () => api.get('operations').then((d) => { const op = d.operations.find((o) => !o.undone); return op ? undo(op.id) : toast('没有可以撤销的修改', 'info'); }).catch(fail) },
  ];
  const special = [];
  let m;
  if ((m = t.match(/^合并\s*(.+?)\s*(?:到|->|→)\s*(\S+)$/))) {
    const sources = m[1].split(/[\s,，、]+/).filter(Boolean);
    special.push({ label: '把' + sources.map((s) => '「' + s + '」').join('') + '合并到「' + m[2] + '」', run: () => api.action('merge_tags', { sources, target: m[2] }).then((r) => { toast(r.message, 'ok', r.undo); changed([]); return refreshShared(); }).catch(fail) });
  }
  if ((m = t.match(/^(?:重命名|改名)\s*(\S+)\s*(?:为|成|到|->|→)\s*(\S+)$/))) special.push({ label: '把「' + m[1] + '」改名为「' + m[2] + '」', run: () => api.action('rename', { old: m[1], new: m[2] }).then((r) => { toast(r.message, 'ok', r.undo); changed([]); return refreshShared(); }).catch(fail) });
  if ((m = t.match(/^别名\s*(\S+)\s*(?:是|=|等于|->|→)\s*(\S+)$/))) special.push({ label: '设置别名：「' + m[1] + '」等于「' + m[2] + '」', run: () => api.action('alias_set', { alias: m[1], tag: m[2] }).then((r) => { toast(r.message); return refreshShared(); }).catch(fail) });
  if ((m = t.match(/^#?(\d+)$/))) special.push({ label: '打开图片 #' + m[1], run: () => set({ detail: Number(m[1]), detailList: [Number(m[1])] }) });
  const tagHits = t ? state.tags.filter((x) => x.tag.toLowerCase().includes(t.toLowerCase())).slice(0, 5).map((x) => ({ label: '查看标签「' + x.tag + '」 · ' + x.count + ' 张', run: () => go('library', { q: x.tag }) })) : [];
  const matched = t ? list.filter((c) => c.label.toLowerCase().includes(t.toLowerCase())) : list;
  const search = t && !special.length ? [{ label: '在图库中搜索「' + t + '」', run: () => go('library', { q: t }) }] : [];
  return [...special, ...matched, ...tagHits, ...search].slice(0, 12);
}
function Palette() {
  const open = useStore((s) => s.palette);
  const [text, setText] = useState('');
  const [active, setActive] = useState(0);
  const input = useRef(null);
  useEffect(() => { if (open) { setText(''); setActive(0); setTimeout(() => input.current?.focus(), 0); } }, [open]);
  if (!open) return null;
  const list = commands(text);
  const pick = (c) => { set({ palette: false }); c?.run(); };
  const key = (e) => {
    if (e.isComposing) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((active + 1) % Math.max(1, list.length)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((active - 1 + list.length) % Math.max(1, list.length)); }
    else if (e.key === 'Enter') { e.preventDefault(); pick(list[active]); }
    else if (e.key === 'Escape') { e.preventDefault(); set({ palette: false }); }
  };
  return html`<div class="overlay" onMouseDown=${(e) => { if (e.target === e.currentTarget) set({ palette: false }); }}>
    <div class="palette" role="dialog" aria-label="命令面板">
      <input ref=${input} value=${text} placeholder="输入命令或标签：合并 猫猫 到 猫 · 别名 喵 是 猫 · 改名 A 为 B · #12" onInput=${(e) => { setText(e.currentTarget.value); setActive(0); }} onKeyDown=${key} />
      <ul>${list.map((c, i) => html`<li key=${c.label} aria-selected=${i === active} onMouseEnter=${() => setActive(i)} onMouseDown=${(e) => { e.preventDefault(); pick(c); }}>${c.label}</li>`)}</ul>
      <p class="hint">↑↓ 选择 · Enter 执行 · Esc 关闭</p>
    </div>
  </div>`;
}

function Confirm() {
  const dialog = useStore((s) => s.confirm);
  const yes = useRef(null);
  useEffect(() => { if (dialog) setTimeout(() => yes.current?.focus(), 0); }, [dialog]);
  if (!dialog) return null;
  const close = (value) => { set({ confirm: null }); dialog.resolve(value); };
  return html`<div class="overlay" onKeyDown=${(e) => { if (e.key === 'Escape') close(false); }}>
    <div class="modal" role="alertdialog" aria-label=${dialog.title}>
      <h2>${dialog.title}</h2><p>${dialog.message}</p>
      <div class="row end"><button class="ghost" onClick=${() => close(false)}>取消</button><button ref=${yes} class=${dialog.danger ? 'danger' : 'primary'} onClick=${() => close(true)}>${dialog.yes}</button></div>
    </div>
  </div>`;
}

function Toast() {
  const t = useStore((s) => s.toast);
  if (!t) return null;
  return html`<div class=${'toast ' + t.kind} role="status" key=${t.id}><span>${t.message}</span>
    ${t.undo ? html`<button class="link" onClick=${() => { set({ toast: null }); undo(t.undo); }}><${Icon} name="undo" size=${16} /> 撤销</button>` : null}
    <button class="icon-button" aria-label="关闭" onClick=${() => set({ toast: null })}><${Icon} name="close" size=${16} /></button></div>`;
}

function ImportStatus() {
  const job = useStore((s) => s.importing);
  if (!job) return null;
  return html`<div class="import-status"><div class="bar"><i style=${'width:' + (job.done / job.total) * 100 + '%'}></i></div><span>正在导入 ${job.done + 1} / ${job.total}：${job.name} · 已新增 ${job.added} 张</span></div>`;
}

function App() {
  const route = useStore((s) => s.route);
  const counts = useStore((s) => s.counts);
  const similar = useStore((s) => s.similar);
  const analysis = useStore((s) => s.analysis);
  const [drop, setDrop] = useState(false);
  const files = useRef(null);
  const folder = useRef(null);
  useEffect(() => {
    const onHash = () => set({ route: parseRoute() });
    window.addEventListener('hashchange', onHash);
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); set({ palette: !state.palette }); }
      else if (e.key === '/' && !e.target.closest?.('input,textarea,select') && !document.querySelector('.triage')) { e.preventDefault(); go('library', { q: state.route.params.get('q') || '' }); setTimeout(() => document.getElementById('library-query')?.focus(), 50); }
    };
    window.addEventListener('keydown', onKey);
    const onImport = (e) => (e.detail === 'folder' ? folder : files).current?.click();
    window.addEventListener('mh:import', onImport);
    let depth = 0;
    const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
    const enter = (e) => { if (hasFiles(e)) { depth += 1; setDrop(true); } };
    const leave = () => { depth = Math.max(0, depth - 1); if (!depth) setDrop(false); };
    const over = (e) => { if (hasFiles(e)) e.preventDefault(); };
    const dropped = (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault(); depth = 0; setDrop(false);
      const entries = [...e.dataTransfer.items].map((i) => i.webkitGetAsEntry?.()).filter(Boolean);
      (entries.length ? Promise.all(entries.map((x) => readEntry(x))).then((l) => l.flat()) : Promise.resolve([...e.dataTransfer.files].map((file) => ({ file, path: file.name })))).then(importFiles).catch(fail);
    };
    window.addEventListener('dragenter', enter); window.addEventListener('dragleave', leave); window.addEventListener('dragover', over); window.addEventListener('drop', dropped);
    return () => { window.removeEventListener('hashchange', onHash); window.removeEventListener('keydown', onKey); window.removeEventListener('mh:import', onImport); };
  }, []);
  const Page = PAGES[route.page].view;
  const badge = (key) => (key === 'inbox' ? counts.inbox : key === 'similar' ? similar : null);
  return html`<div class="shell">
    <nav class="rail" aria-label="导航">
      <div class="brand"><img class="mark" src="./logo.png" alt="" width="38" height="38" /><span><strong>表情港湾</strong><small>MemeHarbor 2.0</small></span></div>
      ${Object.entries(PAGES).map(([key, p]) => html`<a key=${key} href=${'#/' + key} title=${p.title} class=${'nav' + (route.page === key ? ' on' : '')} aria-current=${route.page === key ? 'page' : undefined}>
        <${Icon} name=${p.icon} /><span>${p.title}</span>${badge(p.count) ? html`<b>${badge(p.count)}</b>` : null}</a>`)}
      <span class="grow"></span>
      <button class="nav" title="命令面板 Ctrl K" onClick=${() => set({ palette: true })}><${Icon} name="command" /><span>命令</span><kbd>Ctrl K</kbd></button>
      <button class="nav" title="导入图片或 ZIP" onClick=${() => files.current?.click()}><${Icon} name="upload" /><span>导入图片</span></button>
      <button class="nav" title="导入整个文件夹" onClick=${() => folder.current?.click()}><${Icon} name="folder" /><span>导入文件夹</span></button>
      <button class="nav" title="导出备份" onClick=${() => api.download('export', {}, 'memeharbor-backup.zip').then(() => toast('备份已导出')).catch(fail)}><${Icon} name="download" /><span>导出备份</span></button>
      ${analysis ? html`<p class="rail-note">正在分析旧图片，还剩 ${analysis} 张</p>` : null}
    </nav>
    <main class="content"><${Page} key=${route.page} /></main>
    <${Detail} />
    <${Palette} /><${Confirm} /><${Toast} /><${ImportStatus} />
    ${drop ? html`<div class="drop"><div>松开鼠标即可导入图片、ZIP 或整个文件夹</div></div>` : null}
    <input ref=${files} type="file" hidden multiple accept="image/png,image/jpeg,image/gif,image/webp,image/bmp,.zip"
      onChange=${(e) => { const list = [...e.currentTarget.files].map((file) => ({ file, path: file.name })); e.currentTarget.value = ''; importFiles(list).catch(fail); }} />
    <input ref=${folder} type="file" hidden multiple webkitdirectory
      onChange=${(e) => { const list = [...e.currentTarget.files].map((file) => ({ file, path: file.webkitRelativePath || file.name })); e.currentTarget.value = ''; importFiles(list).catch(fail); }} />
  </div>`;
}

async function start() {
  const root = document.getElementById('app');
  if (!api.available) { root.innerHTML = '<p class="boot">请从 AstrBot 后台：插件 → 表情港湾 MemeHarbor → Pages 打开图库</p>'; return; }
  root.innerHTML = '<p class="boot">正在打开表情港湾…</p>';
  try { await api.ready(); } catch (error) { root.innerHTML = '<p class="boot">连接 AstrBot 失败，请刷新页面重试</p>'; return; }
  set({ route: parseRoute() });
  root.innerHTML = '';
  render(html`<${App} />`, root);
  try {
    const [home] = await Promise.all([api.get('home'), refreshShared()]);
    set({ aiEnabled: home.ai_enabled, scopeMode: home.library_scope, analysis: home.analysis_pending, similar: home.similar_groups });
    if (home.analysis_pending) startAnalysis();
  } catch (error) { fail(error); }
}
start();
