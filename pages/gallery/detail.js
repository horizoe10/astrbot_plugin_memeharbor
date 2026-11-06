import { html, useEffect, useRef, useState } from './vendor/htm-preact.js';
import * as api from './api.js';
import { Icon, TagInput, copy, fmtDate, fmtSize, refreshShared } from './components.js';
import { confirm, fail, isInboxTag, set, state, toast, useStore } from './store.js';

export const changed = (ids) => window.dispatchEvent(new CustomEvent('mh:changed', { detail: { ids } }));
export async function undo(operation) {
  try {
    const result = await api.action('undo', { operation });
    toast(result.message);
    changed([]);
    await refreshShared();
  } catch (error) { fail(error); }
}

function typing(target) { return target && (target.closest?.('input,textarea,select,[contenteditable=true]')); }

export function Detail() {
  const id = useStore((s) => s.detail);
  const list = useStore((s) => s.detailList);
  const ai = useStore((s) => s.aiEnabled);
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [tags, setTags] = useState([]);
  const [dirty, setDirty] = useState(false);
  const [rec, setRec] = useState([]);
  const [aiText, setAiText] = useState('');
  const [busy, setBusy] = useState(false);
  const input = useRef(null);
  const live = useRef({});
  live.current = { id, dirty, tags, data };

  useEffect(() => {
    if (!id) return;
    let alive = true;
    setData(null); setError(''); setRec([]); setAiText(''); setDirty(false);
    api.detail(id).then((d) => { if (alive) { setData(d); setTags(d.tags); } }).catch((e) => alive && setError(e.message));
    api.get('recommend/' + id).then((r) => alive && setRec(r.tags)).catch(() => {});
    return () => { alive = false; };
  }, [id]);

  const save = async (quiet = false, pending = '') => {
    const { id: current, tags: chosen, data: info } = live.current;
    if (!current || !info || info.deleted) return;
    // Text still in the input (Ctrl+Enter) has not reached state yet; include it.
    let final = [...new Set([...chosen, ...String(pending || '').split(/[\s,，、;；]+/).filter(Boolean)])];
    if (final.some((t) => !isInboxTag(t))) final = final.filter((t) => !isInboxTag(t));
    const result = await api.action('replace', { ids: [current], tags: final.join(' ') });
    setDirty(false); setTags(final);
    const fresh = await api.detail(current, true);
    if (live.current.id === current) setData(fresh);
    changed([current]);
    refreshShared().catch(() => {});
    if (!quiet) toast('图片 #' + current + ' 的标签已保存', 'ok', result.undo);
  };
  const move = async (step) => {
    const index = list.indexOf(id);
    const next = list[index + step];
    if (next === undefined) return toast(step > 0 ? '已经是最后一张了' : '已经是第一张了', 'info');
    if (live.current.dirty) await save(true).catch(fail);
    set({ detail: next });
  };
  const close = async () => { if (live.current.dirty) await save(true).catch(fail); set({ detail: null }); };
  const recycle = async () => {
    const restore = Boolean(data.deleted);
    if (!restore && !(await confirm('移入回收站', '把图片 #' + id + ' 移入回收站？之后可以恢复。', '移入回收站'))) return;
    const result = await api.action(restore ? 'restore' : 'delete', { ids: [id] });
    toast(result.message, 'ok', result.undo);
    changed([id]); refreshShared().catch(() => {});
    const index = list.indexOf(id);
    const next = list[index + 1] ?? list[index - 1];
    set({ detail: next ?? null, detailList: list.filter((x) => x !== id) });
  };
  const describe = async () => {
    setBusy(true); setAiText('正在识别，最长约 45 秒…');
    try { const r = await api.action('describe', { ids: [id] }); setAiText(r.caption + (r.ocr ? '\n图中文字：' + r.ocr : '')); setRec(r.tags.map((tag) => ({ tag, reason: 'AI 建议' })).concat(rec)); }
    catch (e) { setAiText(e.message); } finally { setBusy(false); }
  };
  const toggle = (tag) => { setTags(tags.includes(tag) ? tags.filter((t) => t !== tag) : [...tags, tag]); setDirty(true); };

  useEffect(() => {
    if (!id) return undefined;
    const handler = (event) => {
      if (state.palette || state.confirm || document.querySelector('.triage')) return;
      if (event.key === 'Escape') { event.preventDefault(); close().catch(fail); return; }
      if (typing(event.target)) return;
      if (event.key === 'ArrowRight') { event.preventDefault(); move(1).catch(fail); }
      else if (event.key === 'ArrowLeft') { event.preventDefault(); move(-1).catch(fail); }
      else if (event.key === 't' || event.key === 'e') { event.preventDefault(); input.current?.focus(); }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [id, list]);

  if (!id) return null;
  const deleted = Boolean(data?.deleted);
  return html`<aside class="drawer" aria-label="图片详情">
    <header class="drawer-head">
      <strong>${'#' + id}</strong>
      ${data ? html`<span class="pill">${data.origin}</span>` : null}
      <span class="grow"></span>
      <button class="icon-button" title="上一张 ←" onClick=${() => move(-1).catch(fail)}><${Icon} name="left" /></button>
      <button class="icon-button" title="下一张 →" onClick=${() => move(1).catch(fail)}><${Icon} name="right" /></button>
      <button class="icon-button" title="关闭 Esc" onClick=${() => close()}><${Icon} name="close" /></button>
    </header>
    <div class="drawer-preview">${error ? html`<span class="muted">${error}</span>` : data ? html`<img src=${data.preview} alt=${'图片 ' + id} />` : html`<span class="skeleton"></span>`}</div>
    ${data ? html`<p class="meta">${data.width} × ${data.height} · ${fmtSize(data.bytes)} · ${data.animated ? '动图' : '静态图'} · 收集于 ${fmtDate(data.created)} · 已发送 ${data.sent} 次</p>` : null}
    ${deleted ? html`<p class="notice">这张图片在回收站里，恢复后才能修改标签。</p>` : html`
      <label class="label">标签</label>
      <${TagInput} value=${tags} inputRef=${input} onChange=${(v) => { setTags(v); setDirty(true); }} onSubmit=${({ next, pending }) => (next ? save(false, pending).then(() => move(1)) : save(false, pending)).catch(fail)} />
      ${rec.length ? html`<div class="rec"><span class="label">推荐</span>${rec.map((r) => html`<button key=${r.tag} class=${'rec-tag' + (tags.includes(r.tag) ? ' on' : '')} title=${r.reason} onClick=${() => toggle(r.tag)}>${r.tag}</button>`)}</div>` : null}
      <div class="row">
        <button class="primary" disabled=${!data} onClick=${() => save().catch(fail)}>保存${dirty ? ' •' : ''}</button>
        <button disabled=${!data} onClick=${() => save().then(() => move(1)).catch(fail)}>保存并下一张</button>
        ${ai ? html`<button disabled=${busy} onClick=${() => describe()}><${Icon} name="spark" /> AI 识图</button>` : null}
      </div>
      ${aiText ? html`<p class="ai-text">${aiText}</p>` : null}`}
    ${data && (data.caption || data.ocr) ? html`<p class="muted pre">${[data.caption && '描述：' + data.caption, data.ocr && '图中文字：' + data.ocr].filter(Boolean).join('\n')}</p>` : null}
    <div class="row subtle">
      <button class="ghost" onClick=${() => copy('发图 ' + id).then((ok) => toast(ok ? '已复制「发图 ' + id + '」' : '复制失败', ok ? 'ok' : 'error'))}><${Icon} name="copy" /> 复制发图命令</button>
      <button class="ghost" onClick=${() => api.download('file/' + id).catch(fail)}><${Icon} name="download" /> 下载原图</button>
      <button class=${'ghost' + (deleted ? '' : ' danger')} disabled=${!data} onClick=${() => recycle().catch(fail)}>${deleted ? '恢复这张图片' : '移入回收站'}</button>
    </div>
    <p class="hint">← → 切换 · T 编辑标签 · Ctrl + Enter 保存并下一张 · Esc 关闭</p>
  </aside>`;
}
