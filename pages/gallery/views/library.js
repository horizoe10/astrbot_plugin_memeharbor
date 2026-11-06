import { html, useEffect, useMemo, useRef, useState } from '../vendor/htm-preact.js';
import * as api from '../api.js';
import { Empty, Icon, TagInput, Tags, Thumb, chatCommand, copy, describeQuery, refreshShared } from '../components.js';
import { changed } from '../detail.js';
import { confirm, fail, go, set, toast, useStore } from '../store.js';

const VIEWS = [['all', '全部', 'all'], ['inbox', '待整理', 'inbox'], ['trash', '回收站', 'trash']];
const GAP = 8;

/** Justified rows (like photo apps): each row is scaled to fill the width. */
function layout(items, width, target) {
  const rows = [];
  let row = [], ratio = 0, top = 0;
  const flush = (last) => {
    const height = last ? Math.min(target, (width - GAP * (row.length - 1)) / ratio) : (width - GAP * (row.length - 1)) / ratio;
    let left = 0;
    rows.push({ top, height, cells: row.map((entry) => { const w = entry.ratio * height; const cell = { ...entry, left, width: w }; left += w + GAP; return cell; }) });
    top += height + GAP; row = []; ratio = 0;
  };
  items.forEach((item, index) => {
    const r = item.width && item.height ? Math.min(2.4, Math.max(0.45, item.width / item.height)) : 1;
    row.push({ item, index, ratio: r }); ratio += r;
    if (ratio * target + GAP * (row.length - 1) >= width) flush(false);
  });
  if (row.length) flush(true);
  return { rows, height: Math.max(0, top - GAP) };
}

function QueryBox({ value, onSubmit }) {
  const tags = useStore((s) => s.tags);
  const [text, setText] = useState(value);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  useEffect(() => setText(value), [value]);
  const token = (text.split(/[\s|]+/).pop() || '').replace(/^-/, '');
  const options = token ? tags.filter((t) => t.tag.toLowerCase().includes(token.toLowerCase()) && t.tag !== token).sort((a, b) => (b.tag.startsWith(token) - a.tag.startsWith(token)) || b.count - a.count).slice(0, 8) : [];
  const apply = (tag) => { const match = text.match(/(^|[\s|])(-?)([^\s|]*)$/); setText(text.slice(0, text.length - match[3].length) + tag + ' '); setOpen(false); };
  const key = (e) => {
    if (e.isComposing) return;
    if (open && options.length && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) { e.preventDefault(); setActive((active + (e.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length); }
    else if (open && options.length && e.key === 'Tab') { e.preventDefault(); apply(options[active].tag); }
    else if (e.key === 'Enter') { e.preventDefault(); setOpen(false); onSubmit(text.trim()); }
    else if (e.key === 'Escape') setOpen(false);
  };
  return html`<div class="query">
    <span class="query-icon"><${Icon} name="search" /></span>
    <input id="library-query" value=${text} placeholder="搜标签：猫 憋笑 · 排除：-伤心 · 任一：猫 | 狗 · 编号：#12" aria-label="搜索图库"
      onInput=${(e) => { setText(e.currentTarget.value); setOpen(true); setActive(0); }} onKeyDown=${key} onBlur=${() => setTimeout(() => setOpen(false), 120)} />
    ${text ? html`<button class="icon-button" title="清除" onClick=${() => { setText(''); onSubmit(''); }}><${Icon} name="close" /></button>` : null}
    ${open && options.length ? html`<ul class="suggest">${options.map((o, i) => html`<li key=${o.tag} aria-selected=${i === active} onMouseDown=${(e) => { e.preventDefault(); apply(o.tag); }}><span>${o.tag}</span><small>${o.count} 张</small></li>`)}</ul>` : null}
  </div>`;
}

export function Library() {
  const route = useStore((s) => s.route);
  const counts = useStore((s) => s.counts);
  const params = route.params;
  const view = params.get('view') || 'all';
  const q = params.get('q') || '';
  const sort = params.get('sort') || 'new';
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(null);
  const [width, setWidth] = useState(0);
  const [scroll, setScroll] = useState(0);
  const [selected, setSelected] = useState(() => new Set());
  const [allResults, setAll] = useState(false);
  const [batchTags, setBatchTags] = useState([]);
  const box = useRef(null);
  const anchor = useRef(null);
  const loading = useRef(false);
  const seq = useRef(0);
  const known = useRef([]);
  known.current = items;

  const fetchRange = async (offset, limit, token) => {
    const data = await api.get('list', { q, view, sort, offset, limit });
    if (token !== seq.current) return null;
    set({ counts: data.counts });
    return data;
  };
  const reset = async (keep = 0) => {
    const token = ++seq.current;
    loading.current = true;
    try {
      const data = await fetchRange(0, Math.min(3000, Math.max(300, keep)), token);
      if (data) { setItems(data.items); setTotal(data.total); }
    } catch (e) { fail(e); } finally { if (token === seq.current) loading.current = false; }
  };
  const more = async () => {
    if (loading.current || total === null || known.current.length >= total) return;
    const token = seq.current;
    loading.current = true;
    try {
      const data = await fetchRange(known.current.length, 300, token);
      if (data) { setItems((old) => old.concat(data.items)); setTotal(data.total); }
    } catch (e) { fail(e); } finally { loading.current = false; }
  };
  useEffect(() => { setSelected(new Set()); setAll(false); setItems([]); setTotal(null); window.scrollTo(0, 0); reset(); }, [q, view, sort]);
  useEffect(() => { const h = () => reset(known.current.length); window.addEventListener('mh:changed', h); return () => window.removeEventListener('mh:changed', h); }, [q, view, sort]);
  useEffect(() => {
    const observer = new ResizeObserver(() => setWidth(box.current?.clientWidth || 0));
    if (box.current) observer.observe(box.current);
    let frame = 0;
    const onScroll = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => setScroll(window.scrollY)); };
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => { observer.disconnect(); window.removeEventListener('scroll', onScroll); };
  }, []);

  const target = width < 520 ? 118 : width < 900 ? 150 : 176;
  const { rows, height } = useMemo(() => (width ? layout(items, width, target) : { rows: [], height: 0 }), [items, width]);
  const offsetTop = box.current ? box.current.getBoundingClientRect().top + window.scrollY : 0;
  const viewTop = scroll - offsetTop - 900, viewBottom = scroll - offsetTop + window.innerHeight + 900;
  const visible = rows.filter((r) => r.top + r.height >= viewTop && r.top <= viewBottom);
  useEffect(() => { if (rows.length && visible.length && rows.indexOf(visible[visible.length - 1]) >= rows.length - 4) more(); }, [scroll, rows.length, total]);

  const nav = (patch) => go('library', { q, view, sort, ...patch });
  const count = allResults ? total : selected.size;
  const toggle = (item, index, range) => {
    const next = new Set(allResults ? items.map((i) => i.id) : selected);
    if (allResults) setAll(false);
    if (range && anchor.current !== null) {
      const [a, b] = [Math.min(anchor.current, index), Math.max(anchor.current, index)];
      const on = !next.has(item.id);
      items.slice(a, b + 1).forEach((i) => (on ? next.add(i.id) : next.delete(i.id)));
    } else if (next.has(item.id)) next.delete(item.id); else next.add(item.id);
    anchor.current = index;
    setSelected(next);
  };
  const open = (item) => set({ detail: item.id, detailList: items.map((i) => i.id) });
  const payload = () => (allResults ? { select: { q, view } } : { ids: [...selected] });
  const batch = async (name) => {
    if (!count) return;
    if ((name === 'add' || name === 'remove') && !batchTags.length) return toast('先输入要添加或移除的标签', 'error');
    if (name === 'delete' && !(await confirm('移入回收站', '把所选的 ' + count + ' 张图片移入回收站？之后可以恢复。', '移入回收站'))) return;
    const result = await api.action(name, { tags: batchTags.join(' '), ...payload() });
    toast(result.message, 'ok', result.undo);
    setSelected(new Set()); setAll(false); setBatchTags([]);
    changed([]); refreshShared().catch(() => {});
  };

  const meaning = describeQuery(q);
  const command = chatCommand(q);
  return html`<div class="page library">
    <header class="page-head stack">
      <div class="toolbar">
        <${QueryBox} value=${q} onSubmit=${(text) => nav({ q: text })} />
        <select value=${sort} aria-label="排序" onChange=${(e) => nav({ sort: e.currentTarget.value })}>
          <option value="new">最新收集</option><option value="old">最早收集</option><option value="sent">最常发送</option><option value="inbox">待整理优先</option>
        </select>
        <button onClick=${() => window.dispatchEvent(new CustomEvent('mh:import', { detail: 'files' }))}><${Icon} name="upload" /> 导入</button>
      </div>
      <div class="tabs">
        ${VIEWS.map(([key, label]) => html`<button key=${key} class=${'tab' + (view === key ? ' on' : '')} onClick=${() => nav({ view: key })}>${label}<small>${counts[key] ?? ''}</small></button>`)}
        <span class="grow"></span>
        ${q ? html`<span class="interpret" title=${meaning}>${meaning ? meaning + ' · ' : ''}在群里发：<code>${command}</code><button class="link" onClick=${() => copy(command).then((ok) => toast(ok ? '已复制「' + command + '」' : '复制失败', ok ? 'ok' : 'error'))}>复制</button></span>` : null}
        <span class="muted">${total === null ? '加载中…' : total + ' 张'}</span>
      </div>
    </header>
    <div class="justified" ref=${box} style=${'height:' + height + 'px'}>
      ${visible.map((row) => row.cells.map(({ item, index, left, width: w }) => {
        const on = allResults || selected.has(item.id);
        return html`<div key=${item.id} class=${'tile' + (on ? ' on' : '') + (item.inbox ? ' inbox' : '')} style=${'left:' + left + 'px;top:' + row.top + 'px;width:' + w + 'px;height:' + row.height + 'px'}
          tabindex="0" role="button" aria-label=${'图片 ' + item.id}
          onClick=${(e) => (e.shiftKey || e.ctrlKey || e.metaKey || selected.size || allResults ? toggle(item, index, e.shiftKey) : open(item))}
          onKeyDown=${(e) => { if (e.key === 'Enter') open(item); if (e.key === ' ') { e.preventDefault(); toggle(item, index, false); } }}>
          <${Thumb} id=${item.id} gif=${item.gif} hoverPlay=${true} alt=${'图片 ' + item.id} />
          <label class="tile-check" onClick=${(e) => e.stopPropagation()}><input type="checkbox" checked=${on} aria-label=${'选择图片 ' + item.id} onClick=${(e) => toggle(item, index, e.shiftKey)} /></label>
          <span class="tile-badges">${item.gif ? html`<b>GIF</b>` : null}${item.sent ? html`<b title="发送次数">↗${item.sent}</b>` : null}</span>
          <span class="tile-foot"><span class="tile-id">${'#' + item.id}</span><${Tags} tags=${item.tags} limit=${2} /></span>
        </div>`;
      }))}
    </div>
    ${total === 0 ? html`<${Empty} title=${view === 'trash' ? '回收站是空的' : view === 'inbox' ? '没有待整理的图片' : q ? '没有找到匹配的图片' : '图库还是空的'}
      hint=${q ? '换个标签试试，或者看看「标签」页有哪些可用的词' : view === 'all' ? '把图片、ZIP 或整个文件夹拖进页面即可导入' : ''} />` : null}
    ${count ? html`<div class="batchbar" role="region" aria-label="批量操作">
      <strong>${allResults ? '已选中全部 ' + total + ' 张' : '已选 ' + count + ' 张'}</strong>
      ${!allResults && total > count ? html`<button class="link" onClick=${() => setAll(true)}>选中全部 ${total} 张结果</button>` : null}
      ${view !== 'trash' ? html`<${TagInput} compact=${true} value=${batchTags} onChange=${setBatchTags} placeholder="要加减的标签" onSubmit=${() => batch('add').catch(fail)} />
        <button onClick=${() => batch('add').catch(fail)}>添加标签</button><button onClick=${() => batch('remove').catch(fail)}>移除标签</button>
        <button class="danger" onClick=${() => batch('delete').catch(fail)}>移入回收站</button>`
        : html`<button class="primary" onClick=${() => batch('restore').catch(fail)}>恢复所选</button>`}
      <button class="ghost" onClick=${() => { setSelected(new Set()); setAll(false); }}>取消选择</button>
    </div>` : null}
  </div>`;
}
