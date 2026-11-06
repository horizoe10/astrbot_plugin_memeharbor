import { html, useEffect, useMemo, useState } from '../vendor/htm-preact.js';
import * as api from '../api.js';
import { Empty, Thumb, fmtAgo, refreshShared } from '../components.js';
import { changed } from '../detail.js';
import { confirm, fail, go, toast, useStore } from '../store.js';

const SORTS = { count: (a, b) => b.count - a.count, sent: (a, b) => b.sent - a.sent, searches: (a, b) => b.searches - a.searches, latest: (a, b) => b.latest - a.latest, name: (a, b) => a.tag.localeCompare(b.tag, 'zh') };

async function after(result) {
  toast(result.message, 'ok', result.undo);
  changed([]);
  await refreshShared();
}

function TagRow({ info, names, onDone }) {
  const [mode, setMode] = useState('');
  const [value, setValue] = useState('');
  const submit = async () => {
    const word = value.trim();
    if (!word) return;
    if (mode === 'rename') await after(await api.action('rename', { old: info.tag, new: word }));
    if (mode === 'merge') {
      if (!(await confirm('合并标签', '把「' + info.tag + '」的 ' + info.count + ' 张图都改成「' + word + '」？可以在首页撤销。', '合并', false))) return;
      await after(await api.action('merge_tags', { sources: [info.tag], target: word }));
    }
    if (mode === 'alias') { const r = await api.action('alias_set', { alias: word, tag: info.tag }); toast(r.message); await refreshShared(); }
    setMode(''); setValue(''); onDone();
  };
  const dropAlias = async (alias) => { const r = await api.action('alias_remove', { alias }); toast(r.message); await refreshShared(); onDone(); };
  const placeholder = { rename: '新名字', merge: '合并到哪个标签', alias: '群友可能会说的词，例如「喵」' }[mode];
  return html`<li class=${'tag-row' + (info.inbox ? ' inbox' : '')}>
    <button class="tag-cover" title="查看这些图片" onClick=${() => go('library', { q: info.tag })}><${Thumb} id=${info.cover} /></button>
    <div class="tag-main">
      <div class="tag-name"><strong>${info.tag}</strong>
        ${info.aliases.map((a) => html`<span class="alias" key=${a}>= ${a}<button aria-label=${'删除别名 ' + a} onClick=${() => dropAlias(a).catch(fail)}>×</button></span>`)}</div>
      <div class="tag-stats">${info.count} 张 · 发出 ${info.sent} 次 · 30 天被搜 ${info.searches} 次 · 最近收集 ${fmtAgo(info.latest)}</div>
      ${mode ? html`<form class="row" onSubmit=${(e) => { e.preventDefault(); submit().catch(fail); }}>
        <input autofocus value=${value} list=${mode === 'merge' ? 'tag-names' : undefined} placeholder=${placeholder} onInput=${(e) => setValue(e.currentTarget.value)} />
        <button class="primary">确定</button><button type="button" class="ghost" onClick=${() => setMode('')}>取消</button></form>` : null}
    </div>
    ${info.inbox ? null : html`<div class="tag-actions">
      <button class="ghost" onClick=${() => setMode('rename')}>重命名</button>
      <button class="ghost" onClick=${() => setMode('merge')}>合并到…</button>
      <button class="ghost" onClick=${() => setMode('alias')}>加别名</button>
    </div>`}
  </li>`;
}

export function TagsPage() {
  const tags = useStore((s) => s.tags);
  const [health, setHealth] = useState(null);
  const [filter, setFilter] = useState('');
  const [sort, setSort] = useState('count');
  const [ignored, setIgnored] = useState(() => new Set());
  const loadHealth = () => api.get('tag_health').then(setHealth).catch(fail);
  useEffect(() => { refreshShared().catch(fail); loadHealth(); }, []);
  const shown = useMemo(() => tags.filter((t) => !filter || t.tag.toLowerCase().includes(filter.toLowerCase()) || t.aliases.some((a) => a.includes(filter.toLowerCase()))).sort(SORTS[sort]), [tags, filter, sort]);
  const mergePair = async (pair, keepAlias) => {
    await after(await api.action('merge_tags', { sources: [pair.other], target: pair.keep }));
    if (keepAlias) { try { await api.action('alias_set', { alias: pair.other, tag: pair.keep }); } catch { /* alias is optional */ } await refreshShared(); }
    loadHealth();
  };
  const pairs = (health?.duplicates || []).filter((p) => !ignored.has(p.keep + '|' + p.other));
  return html`<div class="page tags-page">
    <header class="page-head"><div><h1>标签</h1><p class="muted">标签决定群里能不能搜到图。合并重复的写法，再给常用的说法加上别名。</p></div></header>
    <datalist id="tag-names">${tags.map((t) => html`<option key=${t.tag} value=${t.tag} />`)}</datalist>
    <div class="split">
      <section>
        <div class="toolbar">
          <input class="grow" placeholder="筛选标签或别名" value=${filter} onInput=${(e) => setFilter(e.currentTarget.value)} />
          <select value=${sort} onChange=${(e) => setSort(e.currentTarget.value)} aria-label="排序">
            <option value="count">按图片数</option><option value="sent">按发送次数</option><option value="searches">按被搜次数</option><option value="latest">按最近收集</option><option value="name">按名称</option>
          </select>
        </div>
        ${shown.length ? html`<ul class="tag-list">${shown.map((t) => html`<${TagRow} key=${t.tag} info=${t} onDone=${loadHealth} />`)}</ul>` : html`<${Empty} title=${tags.length ? '没有匹配的标签' : '还没有标签'} />`}
      </section>
      <aside class="health">
        <section class="card">
          <h2>可能重复的标签</h2>
          ${!health ? html`<p class="muted">正在检查…</p>` : pairs.length ? html`<ul class="pairs">${pairs.map((p) => html`<li key=${p.keep + p.other}>
            <div><strong>「${p.other}」</strong> ${p.other_count} 张 → <strong>「${p.keep}」</strong> ${p.keep_count} 张<small>${p.reason}</small></div>
            <div class="row">
              <button class="primary" onClick=${() => mergePair(p, true).catch(fail)} title="合并后，群里发旧词也能找到">合并并保留旧词</button>
              <button onClick=${() => mergePair(p, false).catch(fail)}>只合并</button>
              <button class="ghost" onClick=${() => setIgnored((s) => new Set(s).add(p.keep + '|' + p.other))}>不是一回事</button>
            </div></li>`)}</ul>` : html`<p class="muted">没有发现写法相近或总是一起出现的标签。</p>`}
        </section>
        <section class="card">
          <h2>只有 1 张图的标签</h2>
          ${health?.singletons.length ? html`<div class="chips-cloud">${health.singletons.slice(0, 40).map((t) => html`<button key=${t} class="cloud" onClick=${() => go('library', { q: t })}>${t}</button>`)}</div>` : html`<p class="muted">没有。</p>`}
        </section>
        <section class="card">
          <h2>30 天没人搜过</h2>
          ${!health?.has_history ? html`<p class="muted">群里用「搜图」后才会有记录。</p>` : health.unsearched.length ? html`<div class="chips-cloud">${health.unsearched.slice(0, 40).map((t) => html`<button key=${t} class="cloud" onClick=${() => go('library', { q: t })}>${t}</button>`)}</div>` : html`<p class="muted">每个标签都被搜过。</p>`}
        </section>
      </aside>
    </div>
  </div>`;
}
