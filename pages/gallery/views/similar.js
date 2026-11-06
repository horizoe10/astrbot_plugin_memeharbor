import { html, useEffect, useRef, useState } from '../vendor/htm-preact.js';
import * as api from '../api.js';
import { Empty, Tags, refreshShared } from '../components.js';
import { changed } from '../detail.js';
import { confirm, fail, set, toast } from '../store.js';

function Group({ group, onDone }) {
  const [keep, setKeep] = useState(group[0].id);
  const merge = async () => {
    const ids = group.map((i) => i.id);
    if (!(await confirm('合并相似图片', '保留 #' + keep + '，其余 ' + (ids.length - 1) + ' 张的标签和发送次数并入这张，原图移入回收站。', '合并', false))) return;
    const r = await api.action('merge', { keep, ids });
    toast(r.message, 'ok', r.undo); changed(ids); refreshShared().catch(() => {}); onDone();
  };
  const dismiss = async () => { const r = await api.action('dismiss', { ids: group.map((i) => i.id) }); toast(r.message); onDone(); };
  return html`<section class="card similar">
    <div class="similar-items">${group.map((item) => html`<button key=${item.id} class=${'similar-item' + (item.id === keep ? ' keep' : '')} onClick=${() => setKeep(item.id)} onDblClick=${() => set({ detail: item.id, detailList: group.map((g) => g.id) })}>
      <div class="thumb">${item.thumbnail ? html`<img src=${item.thumbnail} alt=${'图片 ' + item.id} />` : null}</div>
      <span class="similar-meta">${item.id === keep ? '✓ 保留这张' : '#' + item.id}${item.sent ? ' · 发出 ' + item.sent + ' 次' : ''}</span>
      <span class="similar-tags"><${Tags} tags=${item.tags} limit=${3} /></span></button>`)}</div>
    <div class="row"><span class="muted grow">${group.length} 张看起来几乎一样 · 双击查看大图</span><button class="ghost" onClick=${() => dismiss().catch(fail)}>不是同一张</button><button class="primary" onClick=${() => merge().catch(fail)}>合并到 ${'#' + keep}</button></div>
  </section>`;
}

export function Similar() {
  const [data, setData] = useState(null);
  const timer = useRef(null);
  const load = async () => {
    clearTimeout(timer.current);
    const d = await api.get('similar');
    setData(d); set({ similar: d.total_groups, analysis: d.pending });
    if (d.pending) timer.current = setTimeout(() => load().catch(fail), 300);
  };
  useEffect(() => { load().catch(fail); return () => clearTimeout(timer.current); }, []);
  return html`<div class="page">
    <header class="page-head"><div><h1>相似图片</h1><p class="muted">同一张表情经过压缩、转发后，文件会变得不同。选出要保留的一张合并，其余进回收站，可以撤销。</p></div></header>
    ${!data ? html`<div class="skeleton block"></div>` : html`
      ${data.pending ? html`<p class="notice">正在分析旧图片，还剩 ${data.pending} 张…</p>` : null}
      ${data.groups.length ? data.groups.map((g) => html`<${Group} key=${g.map((i) => i.id).join()} group=${g} onDone=${() => load().catch(fail)} />`)
        : html`<${Empty} title=${data.pending ? '正在分析…' : '没有发现相似的图片'} hint="判断标准经过真实表情包校准，同一角色的不同表情不会被当作重复。" />`}`}
  </div>`;
}
