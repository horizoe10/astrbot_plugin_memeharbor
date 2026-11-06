import { html, useEffect, useState } from '../vendor/htm-preact.js';
import * as api from '../api.js';
import { Icon, Thumb, fmtAgo } from '../components.js';
import { undo } from '../detail.js';
import { fail, go, useStore } from '../store.js';

function Task({ icon, title, value, unit, hint, action, onClick, tone = '' }) {
  return html`<button class=${'task ' + tone} onClick=${onClick}>
    <span class="task-icon"><${Icon} name=${icon} size=${22} /></span>
    <span class="task-body"><span class="task-title">${title}</span><span class="task-value">${value}<small>${unit}</small></span><span class="task-hint">${hint}</span></span>
    <span class="task-action">${action} →</span>
  </button>`;
}

export function Home() {
  const analysis = useStore((s) => s.analysis);
  const [data, setData] = useState(null);
  const load = () => api.get('home').then(setData).catch(fail);
  useEffect(() => { load(); const h = () => load(); window.addEventListener('mh:changed', h); return () => window.removeEventListener('mh:changed', h); }, []);
  useEffect(() => { if (data && data.similar_groups === null && analysis === 0) load(); }, [analysis]);
  if (!data) return html`<div class="page"><div class="skeleton block"></div></div>`;
  const s = data.stats;
  const misses = data.misses;
  const similar = data.similar_groups;
  const backup = data.backup_days ? (data.last_backup ? '上次自动备份 ' + fmtAgo(data.last_backup) : '还没有自动备份') : '自动备份已关闭';
  return html`<div class="page home">
    <header class="page-head">
      <div><h1>今天的表情港湾</h1><p class="muted">图库共 ${s.total} 张 · ${s.tags} 个标签 · 近 7 天新增 ${s.added_week} 张 · 累计发出 ${s.sent} 次 · ${backup}</p></div>
    </header>
    <section class="tasks">
      ${s.review ? html`<${Task} icon="check" title="待审核投稿" value=${s.review} unit=" 张" tone="warn"
        hint="群友收集的图，通过后群里才能搜到" action="去审核" onClick=${() => go('library', { view: 'review' })} />` : null}
      <${Task} icon="triage" title="待整理" value=${s.inbox} unit=" 张" tone=${s.inbox ? 'warn' : 'done'}
        hint=${s.inbox ? (s.auto ? '其中 ' + s.auto + ' 张是群里流行、自动收进来的' : '打上标签后，群里才能搜到它们') : '全部整理好了'} action=${s.inbox ? '开始整理' : '去图库'} onClick=${() => go(s.inbox ? 'triage' : 'library')} />
      <${Task} icon="copy" title="相似图片" value=${similar === null ? '…' : similar} unit=${similar === null ? '' : ' 组'}
        hint=${similar === null ? '正在分析旧图片，还剩 ' + (analysis || data.analysis_pending) + ' 张' : similar ? '同一张图被重复收集了' : '没有发现重复的图'} action="去合并" onClick=${() => go('similar')} />
      <${Task} icon="search" title="群里搜不到" value=${misses.reduce((n, m) => n + m.count, 0)} unit=" 次" tone=${misses.length ? 'warn' : ''}
        hint=${misses.length ? '「' + misses[0].query + '」等 ' + misses.length + ' 个词没有结果' : data.searches ? '最近大家搜的都能找到' : '群里搜图后，这里会显示搜不到的词'} action="看看缺什么" onClick=${() => go('insights')} />
      <${Task} icon="tag" title="标签健康" value=${data.health.duplicates} unit=" 组" tone=${data.health.duplicates ? 'warn' : ''}
        hint=${data.health.example ? '「' + data.health.example.other + '」和「' + data.health.example.keep + '」可能是同一个意思' : data.health.singletons + ' 个标签只有 1 张图'} action="整理标签" onClick=${() => go('tags')} />
    </section>
    <div class="columns">
      <section class="card">
        <h2>群友最近在搜</h2>
        ${data.top_searches.length ? html`<div class="chips-cloud">${data.top_searches.map((t) => html`<button key=${t.query} class="cloud" onClick=${() => go('library', { q: t.query })}>${t.query}<small>${t.count}</small></button>`)}</div>`
          : html`<p class="muted">群里用「搜图」「随机图」后，这里会显示大家常搜的词。</p>`}
        ${misses.length ? html`<h3>搜不到的词</h3><div class="chips-cloud">${misses.map((m) => html`<button key=${m.query} class="cloud miss" title="点开看看怎么补" onClick=${() => go('insights')}>${m.query}<small>${m.count} 次</small></button>`)}</div>` : null}
      </section>
      <section class="card">
        <h2>最近 7 天收集</h2>
        ${data.recent.length ? html`<ul class="bars">${data.recent.map((r) => html`<li key=${r.origin}><span>${r.origin}</span><i style=${'width:' + Math.max(6, (r.count / data.recent[0].count) * 100) + '%'}></i><em>${r.count}</em></li>`)}</ul>`
          : html`<p class="muted">最近还没有收集新图。</p>`}
        <h3>最近的修改</h3>
        ${data.operations.length ? html`<ul class="ops">${data.operations.map((o) => html`<li key=${o.id}><span>${o.summary}</span><small>${fmtAgo(o.at)}</small>${o.undone ? html`<em>已撤销</em>` : html`<button class="link" onClick=${() => undo(o.id).then(load)}>撤销</button>`}</li>`)}</ul>`
          : html`<p class="muted">网页里的修改会记录在这里，随时可以撤销。</p>`}
      </section>
    </div>
  </div>`;
}
