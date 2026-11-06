import { html, useEffect, useMemo, useRef, useState } from './vendor/htm-preact.js';
import * as api from './api.js';
import { isInboxTag, set, state, useStore } from './store.js';

const PATHS = {
  home: 'M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z',
  triage: 'M4 5h16v4H4zM4 11h10v8H4zM16 11h4v8h-4z',
  grid: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
  tag: 'M3 12V4h8l10 10-8 8zM7.5 7.5h.01',
  chart: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
  copy: 'M8 8h11v11H8zM5 16V5h11',
  trash: 'M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13',
  upload: 'M12 16V4M7 9l5-5 5 5M4 20h16',
  folder: 'M3 7a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1zM12 11v5M9.5 13.5L12 11l2.5 2.5',
  download: 'M12 4v12M7 11l5 5 5-5M4 20h16',
  search: 'M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM21 21l-5-5',
  undo: 'M9 14L4 9l5-5M4 9h10a6 6 0 0 1 0 12h-3',
  close: 'M6 6l12 12M18 6L6 18',
  check: 'M5 12l5 5 9-10',
  left: 'M15 6l-6 6 6 6', right: 'M9 6l6 6-6 6',
  spark: 'M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6',
  command: 'M8 4a2 2 0 1 0 0 4h8a2 2 0 1 0 0-4v16a2 2 0 1 0 0-4H8a2 2 0 1 0 0 4z',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
};
export function Icon({ name, size = 18 }) {
  return html`<svg class="icon" width=${size} height=${size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d=${PATHS[name] || ''} /></svg>`;
}

export function Thumb({ id, alt = '', gif = false, hoverPlay = false, eager = false }) {
  const [url, setUrl] = useState(null);
  const [failed, setFailed] = useState(false);
  const [playing, setPlaying] = useState(null);
  const ref = useRef(null);
  const hover = useRef(null);
  useEffect(() => {
    let live = true;
    setUrl(null); setFailed(false); setPlaying(null);
    const load = () => api.thumb(id).then((value) => { if (!live) return; value ? setUrl(value) : setFailed(true); });
    if (eager || !('IntersectionObserver' in window)) { load(); return () => { live = false; }; }
    const observer = new IntersectionObserver((entries) => { if (entries.some((e) => e.isIntersecting)) { observer.disconnect(); load(); } }, { rootMargin: '600px' });
    if (ref.current) observer.observe(ref.current);
    return () => { live = false; observer.disconnect(); };
  }, [id]);
  const enter = () => {
    if (!hoverPlay || !gif) return;
    hover.current = setTimeout(() => api.detail(id).then((d) => setPlaying(d.preview)).catch(() => {}), 350);
  };
  const leave = () => { clearTimeout(hover.current); setPlaying(null); };
  return html`<div class="thumb" ref=${ref} onMouseEnter=${enter} onMouseLeave=${leave}>
    ${failed ? html`<span class="thumb-missing">无法预览</span>` : url ? html`<img src=${playing || url} alt=${alt} draggable="false" />` : html`<span class="skeleton"></span>`}
  </div>`;
}

export function TagInput({ value, onChange, placeholder = '输入标签，回车添加', onSubmit, autoFocus = false, inputRef, compact = false, allowInbox = true }) {
  const tags = useStore((s) => s.tags);
  const aliases = useStore((s) => s.aliases);
  const [text, setText] = useState('');
  const [active, setActive] = useState(0);
  const own = useRef(null);
  const input = inputRef || own;
  useEffect(() => { if (autoFocus) input.current?.focus(); }, [autoFocus]);
  const options = useMemo(() => {
    const word = text.trim().toLowerCase();
    if (!word) return [];
    const list = tags
      .filter((t) => t.tag.toLowerCase().includes(word) && !value.includes(t.tag) && (allowInbox || !t.inbox))
      .sort((a, b) => (b.tag.toLowerCase().startsWith(word) - a.tag.toLowerCase().startsWith(word)) || b.count - a.count)
      .slice(0, 7)
      .map((t) => ({ tag: t.tag, hint: t.count + ' 张' }));
    const alias = aliases[word];
    if (alias && !value.includes(alias)) list.unshift({ tag: alias, hint: '「' + text.trim() + '」是它的别名' });
    return list;
  }, [text, tags, aliases, value]);
  const add = (words) => {
    const next = [...value];
    for (const word of words.split(/[\s,，、;；]+/).map((w) => w.trim()).filter(Boolean)) if (!next.includes(word)) next.push(word);
    if (next.length !== value.length) onChange(next);
    setText(''); setActive(0);
  };
  const key = (event) => {
    if (event.isComposing) return;
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); const pending = text.trim(); if (pending) add(pending); onSubmit?.({ next: true, pending }); return; }
    if (event.key === 'ArrowDown' && options.length) { event.preventDefault(); setActive((active + 1) % options.length); return; }
    if (event.key === 'ArrowUp' && options.length) { event.preventDefault(); setActive((active - 1 + options.length) % options.length); return; }
    if (event.key === 'Tab' && options.length) { event.preventDefault(); add(options[active].tag); return; }
    if (event.key === 'Enter') {
      event.preventDefault();
      if (text.trim()) add(options.length && active > 0 ? options[active].tag : text);
      else onSubmit?.({ next: false });
      return;
    }
    if ((event.key === ' ' || event.key === ',' || event.key === '，') && text.trim()) { event.preventDefault(); add(text); return; }
    if (event.key === 'Backspace' && !text && value.length) { onChange(value.slice(0, -1)); }
    if (event.key === 'Escape') { setText(''); }
  };
  return html`<div class=${'tag-input' + (compact ? ' compact' : '')} onClick=${(e) => { if (e.target === e.currentTarget) input.current?.focus(); }}>
    ${value.map((tag) => html`<span class=${'chip' + (isInboxTag(tag) ? ' warn' : '')} key=${tag}>${tag}<button type="button" aria-label=${'移除 ' + tag} onClick=${() => onChange(value.filter((t) => t !== tag))}>×</button></span>`)}
    <input ref=${input} value=${text} placeholder=${value.length ? '' : placeholder} aria-label="输入标签"
      onInput=${(e) => { setText(e.currentTarget.value); setActive(0); }} onKeyDown=${key} />
    ${options.length ? html`<ul class="suggest" role="listbox">${options.map((o, i) => html`<li key=${o.tag} role="option" aria-selected=${i === active} onMouseDown=${(e) => { e.preventDefault(); add(o.tag); }}><span>${o.tag}</span><small>${o.hint}</small></li>`)}</ul>` : null}
  </div>`;
}

export function Empty({ title, hint, children }) {
  return html`<div class="empty"><strong>${title}</strong>${hint ? html`<p>${hint}</p>` : null}${children}</div>`;
}

export function Tags({ tags, limit = 3 }) {
  const shown = tags.filter((t) => !isInboxTag(t));
  if (!shown.length) return html`<span class="tag warn">${tags.length ? '待整理' : '未打标签'}</span>`;
  return html`${shown.slice(0, limit).map((t) => html`<span class="tag" key=${t}>${t}</span>`)}${shown.length > limit ? html`<span class="tag muted">+${shown.length - limit}</span>` : null}`;
}

export const fmtDate = (s) => { const d = new Date(s * 1000); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); };
export const fmtAgo = (s) => {
  const minutes = Math.round((Date.now() / 1000 - s) / 60);
  if (minutes < 1) return '刚刚'; if (minutes < 60) return minutes + ' 分钟前';
  const hours = Math.round(minutes / 60); if (hours < 24) return hours + ' 小时前';
  return Math.round(hours / 24) + ' 天前';
};
export const fmtSize = (b) => (b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB');

export async function copy(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch {
    const area = document.createElement('textarea'); area.value = text; document.body.append(area); area.select();
    let ok = false; try { ok = document.execCommand('copy'); } catch { ok = false; } area.remove(); return ok;
  }
}

/** Refresh counts and the tag catalogue after any change. */
export async function refreshShared() {
  const [tags, list] = await Promise.all([api.get('tags'), api.get('list', { limit: 1 })]);
  set({ tags: tags.tags, aliases: tags.aliases, inboxTags: tags.inbox_tags, counts: list.counts });
}

export function chatCommand(query) {
  const q = query.trim();
  return q ? '搜图 ' + q : '查看图片';
}

/** Human description of the search syntax shared with the chat command. */
export function describeQuery(query) {
  const groups = query.split('|').map((g) => g.trim()).filter(Boolean);
  if (!groups.length) return '';
  return groups.map((g) => {
    const words = g.split(/[\s,，、;；]+/).filter(Boolean);
    const inc = words.filter((w) => !w.startsWith('-') && !w.startsWith('#'));
    const exc = words.filter((w) => w.startsWith('-')).map((w) => w.slice(1));
    const ids = words.filter((w) => w.startsWith('#'));
    return [inc.length ? '同时带' + inc.map((w) => '「' + w + '」').join('') : '', exc.length ? '不带' + exc.map((w) => '「' + w + '」').join('') : '', ids.length ? '编号 ' + ids.join(' ') : ''].filter(Boolean).join('，');
  }).join('；或者 ');
}
export { state };
