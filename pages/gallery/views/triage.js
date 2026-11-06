import { html, useEffect, useRef, useState } from '../vendor/htm-preact.js';
import * as api from '../api.js';
import { Empty, Icon, TagInput, Thumb, fmtDate, refreshShared } from '../components.js';
import { changed } from '../detail.js';
import { confirm, fail, go, isInboxTag, state, toast, useStore } from '../store.js';

const split = (text) => String(text || '').split(/[\s,，、;；]+/).filter(Boolean);
const typing = (t) => t && t.closest?.('input,textarea,select');

function Single() {
  const ai = useStore((s) => s.aiEnabled);
  const [queue, setQueue] = useState(null);
  const [total, setTotal] = useState(0);
  const [index, setIndex] = useState(0);
  const [data, setData] = useState(null);
  const [rec, setRec] = useState([]);
  const [chosen, setChosen] = useState([]);
  const [history, setHistory] = useState([]);
  const [done, setDone] = useState(0);
  const input = useRef(null);
  const touch = useRef(null);
  const current = queue ? queue[index] : null;
  const live = useRef({});
  live.current = { current, chosen, rec, index, queue, history };

  const loadQueue = async () => {
    const list = await api.get('list', { view: 'inbox', sort: 'new', limit: 300 });
    setQueue(list.items.map((i) => i.id)); setTotal(list.total); setIndex(0);
  };
  useEffect(() => { loadQueue().catch(fail); }, []);
  useEffect(() => {
    if (!current) return undefined;
    let alive = true;
    setData(null); setRec([]);
    api.detail(current).then((d) => { if (!alive) return; setData(d); setChosen(d.tags.filter((t) => !isInboxTag(t))); }).catch(fail);
    api.get('recommend/' + current).then((r) => alive && setRec(r.tags)).catch(() => {});
    const next = queue[index + 1];
    if (next) api.detail(next).catch(() => {});
    return () => { alive = false; };
  }, [current]);

  const toggle = (tag) => setChosen((list) => (list.includes(tag) ? list.filter((t) => t !== tag) : [...list, tag]));
  const save = async (pending = '') => {
    const { current: id, chosen: picked } = live.current;
    if (!id) return;
    const tags = [...new Set([...picked, ...split(pending)])].filter((t) => !isInboxTag(t));
    if (!tags.length) { toast('先选一个推荐标签或输入标签，再保存', 'error'); return; }
    const result = await api.action('replace', { ids: [id], tags: tags.join(' ') });
    api.forget(id);
    setHistory((h) => [...h, { id, undo: result.undo, at: live.current.index }]);
    setDone((n) => n + 1);
    setQueue((q) => q.filter((x) => x !== id));
    setChosen([]);
    changed([id]); refreshShared().catch(() => {});
  };
  const skip = () => setIndex((i) => Math.min(i + 1, (live.current.queue || []).length));
  const back = () => setIndex((i) => Math.max(0, i - 1));
  const undoLast = async () => {
    const last = live.current.history[live.current.history.length - 1];
    if (!last) return toast('还没有可以撤销的整理', 'info');
    const result = await api.action('undo', { operation: last.undo });
    api.forget(last.id);
    setHistory((h) => h.slice(0, -1));
    setDone((n) => Math.max(0, n - 1));
    setQueue((q) => { const copy = q.filter((x) => x !== last.id); copy.splice(Math.min(last.at, copy.length), 0, last.id); return copy; });
    setIndex(last.at);
    toast(result.message);
    changed([last.id]); refreshShared().catch(() => {});
  };
  const recycle = async () => {
    const id = live.current.current;
    if (!id || !(await confirm('移入回收站', '这张图不要了？会移入回收站，之后可以恢复。', '移入回收站'))) return;
    const result = await api.action('delete', { ids: [id] });
    setHistory((h) => [...h, { id, undo: result.undo, at: live.current.index }]);
    setQueue((q) => q.filter((x) => x !== id));
    toast('已移入回收站', 'ok', result.undo);
    changed([id]); refreshShared().catch(() => {});
  };
  const describe = async () => {
    const id = live.current.current;
    toast('AI 正在识别，最长约 45 秒…', 'info');
    const r = await api.action('describe', { ids: [id] });
    setRec((old) => [...r.tags.map((tag) => ({ tag, reason: 'AI 建议' })), ...old.filter((o) => !r.tags.includes(o.tag))]);
  };

  useEffect(() => {
    const handler = (event) => {
      if (state.palette || state.confirm || state.detail) return;
      const inField = typing(event.target);
      const emptyInput = event.target === input.current && !input.current.value;
      if (event.key === 'Escape' && event.target === input.current) { input.current.blur(); return; }
      if (!inField || emptyInput) {
        const number = Number(event.key);
        if (number >= 1 && number <= 9 && !event.ctrlKey && !event.metaKey) {
          const pick = live.current.rec[number - 1];
          if (pick) { event.preventDefault(); toggle(pick.tag); }
          return;
        }
      }
      const run = (fn) => { event.preventDefault(); fn().catch?.(fail); };
      if (emptyInput) {
        if (event.key === 'ArrowRight') run(async () => skip());
        else if (event.key === 'ArrowLeft') run(async () => back());
        else if (event.key === 'Delete') run(() => recycle());
        return;
      }
      if (inField) return;
      if (event.key === 'Enter') run(() => save());
      else if (event.key === 's' || event.key === 'S' || event.key === 'ArrowRight') run(async () => skip());
      else if (event.key === 'ArrowLeft') run(async () => back());
      else if (event.key === 'z' || event.key === 'Z') run(() => undoLast());
      else if (event.key === 'Delete') run(() => recycle());
      else if (event.key === 't' || event.key === '/') run(async () => input.current?.focus());
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  if (!queue) return html`<div class="skeleton block"></div>`;
  if (!queue.length) return html`<${Empty} title=${done ? '整理完成，一共处理了 ' + done + ' 张' : '没有待整理的图片'} hint="新收集、没写标签的图会出现在这里。">
    <div class="row center">${history.length ? html`<button onClick=${() => undoLast().catch(fail)}><${Icon} name="undo" /> 撤销上一张</button>` : null}<button class="primary" onClick=${() => go('library')}>去图库</button></div>
  <//>`;
  if (index >= queue.length) return html`<${Empty} title="这一轮看完了" hint=${'还剩 ' + queue.length + ' 张你跳过的图。'}>
    <div class="row center"><button class="primary" onClick=${() => setIndex(0)}>从头再看一遍跳过的</button><button onClick=${() => go('home')}>回首页</button></div>
  <//>`;
  const left = Math.max(0, total - done);
  return html`<div class="triage-single">
    <div class="triage-stage" onTouchStart=${(e) => { touch.current = e.touches[0].clientX; }}
      onTouchEnd=${(e) => { const dx = e.changedTouches[0].clientX - (touch.current ?? 0); if (Math.abs(dx) > 70) (dx < 0 ? skip : back)(); }}>
      ${data ? html`<img src=${data.preview} alt=${'图片 ' + current} />` : html`<span class="skeleton"></span>`}
      <div class="stage-meta">${'#' + current}${data ? ' · ' + data.origin + ' · ' + data.width + '×' + data.height + (data.animated ? ' · 动图' : '') + ' · ' + fmtDate(data.created) : ''}</div>
    </div>
    <div class="triage-panel">
      <div class="progress-line"><span>第 ${index + 1} / ${queue.length} 张 · 已整理 ${done} 张 · 还剩 ${left} 张</span><i style=${'width:' + (total ? (done / total) * 100 : 0) + '%'}></i></div>
      <h3>推荐标签 <small>按数字键选择</small></h3>
      <div class="rec-grid">
        ${rec.length ? rec.map((r, i) => html`<button key=${r.tag} class=${'rec-key' + (chosen.includes(r.tag) ? ' on' : '')} onClick=${() => toggle(r.tag)} title=${r.reason}>
          <kbd>${i + 1}</kbd><span>${r.tag}</span><small>${r.reason}</small></button>`) : html`<p class="muted">还没有可以参考的标签，直接输入吧。</p>`}
      </div>
      <h3>这张图的标签 <small>按 T 输入，Esc 退出输入</small></h3>
      <${TagInput} value=${chosen} onChange=${setChosen} inputRef=${input} placeholder="输入标签，回车添加；空着再按回车保存" allowInbox=${false}
        onSubmit=${({ pending }) => save(pending).catch(fail)} />
      <div class="row">
        <button class="primary big" onClick=${() => save().catch(fail)}><${Icon} name="check" /> 保存，下一张 <kbd>Enter</kbd></button>
        <button onClick=${skip}>跳过 <kbd>S</kbd></button>
        <button disabled=${!history.length} onClick=${() => undoLast().catch(fail)}><${Icon} name="undo" /> 撤销 <kbd>Z</kbd></button>
      </div>
      <div class="row subtle">
        ${ai ? html`<button class="ghost" onClick=${() => describe().catch(fail)}><${Icon} name="spark" /> AI 推荐</button>` : null}
        <button class="ghost danger" onClick=${() => recycle().catch(fail)}>不要这张 <kbd>Del</kbd></button>
      </div>
      <p class="hint">1–9 选推荐 · Enter 保存 · S / → 跳过 · ← 上一张 · Z 撤销 · T 输入标签 · 手机上左右滑动切换</p>
    </div>
  </div>`;
}

function Clusters() {
  const [data, setData] = useState(null);
  const [picked, setPicked] = useState({});
  const [tags, setTags] = useState({});
  const load = () => api.get('clusters').then((d) => { setData(d); setPicked(Object.fromEntries(d.clusters.map((g, i) => [i, new Set(g.map((x) => x.id))]))); }).catch(fail);
  useEffect(() => { load(); }, []);
  if (!data) return html`<div class="skeleton block"></div>`;
  if (!data.clusters.length) return html`<${Empty} title="没有可以一起整理的系列" hint="待整理的图里没有发现长得像的一组，可以切回逐张整理。" />`;
  const apply = async (i) => {
    const ids = [...picked[i]];
    const words = tags[i] || [];
    if (!ids.length || !words.length) return toast('先勾选图片并输入标签', 'error');
    const result = await api.action('finish', { ids, tags: words.join(' ') });
    toast(result.message, 'ok', result.undo);
    changed(ids); refreshShared().catch(() => {}); load();
  };
  return html`<div class="clusters">
    <p class="muted">发现 ${data.total} 组看起来是同一系列的新图。给整组打上标签，再到逐张整理里补充细节。</p>
    ${data.clusters.map((group, i) => html`<section class="card cluster" key=${group[0].id}>
      <div class="cluster-strip">${group.map((item) => {
        const on = picked[i]?.has(item.id);
        return html`<label key=${item.id} class=${'cluster-item' + (on ? ' on' : '')}>
          <input type="checkbox" checked=${on} onChange=${() => setPicked((p) => { const s = new Set(p[i]); s.has(item.id) ? s.delete(item.id) : s.add(item.id); return { ...p, [i]: s }; })} />
          <${Thumb} id=${item.id} gif=${item.gif} hoverPlay=${true} /><small>${'#' + item.id}</small></label>`;
      })}</div>
      <div class="row">
        <${TagInput} value=${tags[i] || []} onChange=${(v) => setTags((t) => ({ ...t, [i]: v }))} placeholder="这一组的共同标签" allowInbox=${false} onSubmit=${() => apply(i).catch(fail)} />
        <button class="primary" onClick=${() => apply(i).catch(fail)}>给选中的 ${picked[i]?.size || 0} 张打标签</button>
      </div>
    </section>`)}
  </div>`;
}

export function Triage() {
  const inbox = useStore((s) => s.counts.inbox);
  const route = useStore((s) => s.route);
  const mode = route.params.get('mode') || 'single';
  return html`<div class="page triage">
    <header class="page-head">
      <div><h1>整理新图</h1><p class="muted">还有 ${inbox} 张没有标签的图。打好标签，群里才能搜到它们。</p></div>
      <div class="tabs"><button class=${'tab' + (mode === 'single' ? ' on' : '')} onClick=${() => go('triage')}>逐张整理</button>
        <button class=${'tab' + (mode === 'clusters' ? ' on' : '')} onClick=${() => go('triage', { mode: 'clusters' })}>按系列整理</button></div>
    </header>
    ${mode === 'clusters' ? html`<${Clusters} />` : html`<${Single} />`}
  </div>`;
}
