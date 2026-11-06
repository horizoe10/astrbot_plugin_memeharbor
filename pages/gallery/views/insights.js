import { html, useEffect, useState } from '../vendor/htm-preact.js';
import * as api from '../api.js';
import { Empty, Thumb, copy, fmtAgo, refreshShared } from '../components.js';
import { fail, go, set, toast, useStore } from '../store.js';

function AliasFix({ query, onDone }) {
  const [open, setOpen] = useState(false);
  const [tag, setTag] = useState('');
  if (/[\s|]/.test(query.trim())) return null;
  if (!open) return html`<button class="ghost" onClick=${() => setOpen(true)} title="群友这样说时，按已有标签找图">当作别名…</button>`;
  const save = async () => { const r = await api.action('alias_set', { alias: query, tag: tag.trim() }); toast(r.message); await refreshShared(); setOpen(false); onDone(); };
  return html`<form class="row" onSubmit=${(e) => { e.preventDefault(); save().catch(fail); }}>
    <input autofocus list="tag-names" placeholder="等于哪个标签" value=${tag} onInput=${(e) => setTag(e.currentTarget.value)} />
    <button class="primary">设置</button><button type="button" class="ghost" onClick=${() => setOpen(false)}>取消</button></form>`;
}

function Preview() {
  const [scopes, setScopes] = useState(null);
  const [scope, setScope] = useState('');
  const [mode, setMode] = useState('search');
  const [query, setQuery] = useState('');
  const [result, setResult] = useState(null);
  useEffect(() => { api.get('scopes').then(setScopes).catch(fail); }, []);
  const run = async () => { if (!query.trim()) return; setResult(await api.get('preview', { q: query.trim(), scope, mode })); };
  const groupMode = scopes?.mode === 'group';
  return html`<section class="card">
    <h2>群里会看到什么</h2>
    <p class="muted">输入群友会发的内容，按插件在群里的规则预览结果，包括图库范围和防重复发送。</p>
    <form class="toolbar wrap" onSubmit=${(e) => { e.preventDefault(); run().catch(fail); }}>
      <select value=${scope} onChange=${(e) => setScope(e.currentTarget.value)} aria-label="哪个群">
        <option value="">${groupMode ? '不限群（网页视角）' : '所有群（共用图库）'}</option>
        ${(scopes?.scopes || []).map((s) => html`<option key=${s.scope} value=${s.scope}>${s.label}</option>`)}
      </select>
      <div class="segmented"><button type="button" class=${mode === 'search' ? 'on' : ''} onClick=${() => setMode('search')}>搜图</button><button type="button" class=${mode === 'quick' ? 'on' : ''} onClick=${() => setMode('quick')}>直接发标签</button></div>
      <input class="grow" placeholder=${mode === 'search' ? '例如：猫 憋笑' : '例如：猫'} value=${query} onInput=${(e) => setQuery(e.currentTarget.value)} />
      <button class="primary">预览</button>
    </form>
    ${result ? html`<div class="preview-result">
      <p><code>${result.command}</code> ${result.mode === 'quick'
        ? (result.total ? '会从 ' + result.total + ' 张图里随机发一张' + (result.recent ? '，先避开最近发过的 ' + result.recent + ' 张' : '') : '不会触发斗图：没有完全相同的标签或别名')
        : (result.total ? '会看到 ' + result.total + ' 张图的预览' : '会提示找不到图片')}
        ${!groupMode && scope ? '（现在是所有群共用图库，各群结果相同）' : ''}</p>
      ${result.items.length ? html`<div class="mini-grid">${result.items.map((i) => html`<button key=${i.id} class=${'mini' + (i.recent ? ' recent' : '')} onClick=${() => set({ detail: i.id, detailList: result.items.map((x) => x.id) })}>
        <${Thumb} id=${i.id} gif=${i.gif} /><small>${'#' + i.id}${i.recent ? ' · 最近发过' : ''}</small></button>`)}</div>` : null}
    </div>` : null}
  </section>`;
}

export function Insights() {
  const tags = useStore((s) => s.tags);
  const [data, setData] = useState(null);
  const load = () => api.get('insights', { days: 30 }).then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  const max = Math.max(1, ...(data?.top || []).map((t) => t.count));
  const dayMax = Math.max(1, ...(data?.daily || []).map((d) => d.count));
  return html`<div class="page insights">
    <header class="page-head"><div><h1>群聊洞察</h1><p class="muted">最近 30 天，群里用「搜图」「随机图」「AI 搜图」共搜了 ${data ? data.searches : '…'} 次。</p></div></header>
    <datalist id="tag-names">${tags.map((t) => html`<option key=${t.tag} value=${t.tag} />`)}</datalist>
    <div class="columns">
      <section class="card">
        <h2>搜不到的词</h2>
        <p class="muted">群友想要、但图库里没有的图。补几张图，或者把它设为已有标签的别名。</p>
        ${!data ? html`<p class="muted">加载中…</p>` : data.misses.length ? html`<ul class="misses">${data.misses.map((m) => html`<li key=${m.query} class=${m.now ? 'filled' : ''}>
          <div><strong>${m.query}</strong><small>${m.count} 次 · ${m.chats} 个群 · ${fmtAgo(m.last)}</small></div>
          <div class="row">${m.now ? html`<span class="good">现在能找到 ${m.now} 张</span>` : html`<span class="bad">还没有</span>`}
            <button class="ghost" onClick=${() => go('library', { q: m.query })}>去图库看看</button>
            ${m.now ? null : html`<${AliasFix} query=${m.query} onDone=${load} />`}
            <button class="ghost" onClick=${() => copy('收集 ' + m.query).then((ok) => ok && toast('已复制「收集 ' + m.query + '」，在群里发图时用'))}>复制收集命令</button></div>
        </li>`)}</ul>` : html`<${Empty} title="最近没有搜不到的情况" hint="群里有人搜图且没结果时，会出现在这里。" />`}
      </section>
      <section class="card">
        <h2>常搜的词</h2>
        ${data?.top.length ? html`<ul class="bars">${data.top.map((t) => html`<li key=${t.query}><button class="link" onClick=${() => go('library', { q: t.query })}>${t.query}</button><i style=${'width:' + Math.max(6, (t.count / max) * 100) + '%'}></i><em>${t.count}</em></li>`)}</ul>` : html`<p class="muted">还没有记录。</p>`}
        ${data?.daily.length ? html`<h3>每天搜图次数</h3><div class="spark">${data.daily.map((d) => html`<span key=${d.day} title=${d.day + '：' + d.count + ' 次'}><i style=${'height:' + Math.max(4, (d.count / dayMax) * 100) + '%'}></i><small>${d.day.slice(3)}</small></span>`)}</div>` : null}
      </section>
    </div>
    <${Preview} />
  </div>`;
}
