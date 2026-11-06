import { html, useEffect, useRef, useState } from '../vendor/htm-preact.js';
import * as api from '../api.js';
import { Empty, Tags, refreshShared } from '../components.js';
import { changed } from '../detail.js';
import { confirm, fail, set, toast } from '../store.js';

function Group({ group, keep, onKeep, onDone }) {
  const merge = async () => {
    const ids = group.map((i) => i.id);
    if (!(await confirm('合并相似图片', '保留 #' + keep + '，其余 ' + (ids.length - 1) + ' 张的标签和发送次数并入这张，原图移入回收站。', '合并', false))) return;
    const r = await api.action('merge', { keep, ids });
    toast(r.message, 'ok', r.undo); changed(ids); refreshShared().catch(() => {}); onDone();
  };
  const dismiss = async () => { const r = await api.action('dismiss', { ids: group.map((i) => i.id) }); toast(r.message); onDone(); };
  return html`<section class="card similar">
    <div class="similar-items">${group.map((item) => html`<button key=${item.id} class=${'similar-item' + (item.id === keep ? ' keep' : '')} onClick=${() => onKeep(item.id)} onDblClick=${() => set({ detail: item.id, detailList: group.map((g) => g.id) })}>
      <div class="thumb">${item.thumbnail ? html`<img src=${item.thumbnail} alt=${'图片 ' + item.id} />` : null}</div>
      <span class="similar-meta">${item.id === keep ? '✓ 保留这张' : '#' + item.id}${item.sent ? ' · 发出 ' + item.sent + ' 次' : ''}</span>
      <span class="similar-tags"><${Tags} tags=${item.tags} limit=${3} /></span></button>`)}</div>
    <div class="row"><span class="muted grow">${group.length} 张看起来几乎一样 · 点一张设为保留 · 双击查看大图</span><button class="ghost" onClick=${() => dismiss().catch(fail)}>不是同一张</button><button class="primary" onClick=${() => merge().catch(fail)}>合并到 ${'#' + keep}</button></div>
  </section>`;
}

export function Similar() {
  const [data, setData] = useState(null);
  const [keeps, setKeeps] = useState({});
  const [busy, setBusy] = useState(false);
  const timer = useRef(null);
  const load = async () => {
    clearTimeout(timer.current);
    const d = await api.get('similar');
    setData(d); set({ similar: d.total_groups, analysis: d.pending });
    if (d.pending) timer.current = setTimeout(() => load().catch(fail), 300);
  };
  useEffect(() => { load().catch(fail); return () => clearTimeout(timer.current); }, []);
  const keyOf = (group) => group.map((i) => i.id).join();
  const keepOf = (group) => { const chosen = keeps[keyOf(group)]; return group.some((i) => i.id === chosen) ? chosen : group[0].id; };
  const mergeAll = async () => {
    const total = data.total_groups;
    const shown = data.groups.length;
    const extra = data.groups.reduce((n, g) => n + g.length - 1, 0);
    const scope = total > shown ? '全部 ' + total + ' 组相似图片（包括页面上没显示的 ' + (total - shown) + ' 组）' : '全部 ' + total + ' 组相似图片（共 ' + extra + ' 张会移入回收站）';
    if (!(await confirm('全部合并', '合并' + scope + '。每组保留标着「✓ 保留这张」的图，其余图片的标签和发送次数并进来，原图移入回收站。可以在首页或提示条里一键撤销。', '全部合并', false))) return;
    setBusy(true);
    try {
      const chosen = {};
      data.groups.forEach((g) => { const keep = keepOf(g); chosen[keep] = keep; });
      const r = await api.action('merge_all', { keeps: chosen });
      toast(r.message, 'ok', r.undo);
      changed([]); refreshShared().catch(() => {}); setKeeps({}); await load();
    } finally { setBusy(false); }
  };
  const groups = data?.groups || [];
  return html`<div class="page">
    <header class="page-head"><div><h1>相似图片</h1><p class="muted">同一张表情经过压缩、转发后，文件会变得不同。选出要保留的一张合并，其余进回收站，可以撤销。</p></div>
      ${groups.length ? html`<button class="primary" disabled=${busy || Boolean(data.pending)} title=${data.pending ? '等旧图片分析完再合并' : ''} onClick=${() => mergeAll().catch(fail)}>${busy ? '正在合并…' : '全部合并（' + data.total_groups + ' 组）'}</button>` : null}</header>
    ${!data ? html`<div class="skeleton block"></div>` : html`
      ${data.pending ? html`<p class="notice">正在分析旧图片，还剩 ${data.pending} 张…</p>` : null}
      ${groups.length ? groups.map((g) => html`<${Group} key=${keyOf(g)} group=${g} keep=${keepOf(g)} onKeep=${(id) => setKeeps((k) => ({ ...k, [keyOf(g)]: id }))} onDone=${() => load().catch(fail)} />`)
        : html`<${Empty} title=${data.pending ? '正在分析…' : '没有发现相似的图片'} hint="判断标准经过真实表情包校准，同一角色的不同表情不会被当作重复。" />`}`}
  </div>`;
}
