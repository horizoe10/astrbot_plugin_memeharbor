import { useEffect, useState } from './vendor/htm-preact.js';

// Tiny global store: shared counts, tags and transient UI (toast, detail, dialogs).
const listeners = new Set();
export const state = {
  route: { page: 'home', params: new URLSearchParams() },
  counts: { all: 0, inbox: 0, trash: 0 },
  tags: [], aliases: {}, inboxTags: ['待整理', '临时'],
  aiEnabled: false, scopeMode: 'shared',
  analysis: 0, similar: null,
  toast: null, detail: null, detailList: [], palette: false, confirm: null, importing: null,
  version: 0,
};
export function set(patch) {
  Object.assign(state, typeof patch === 'function' ? patch(state) : patch);
  state.version += 1;
  listeners.forEach((listener) => listener());
}
export function useStore(select) {
  const [, force] = useState(0);
  useEffect(() => { const listener = () => force((n) => n + 1); listeners.add(listener); return () => listeners.delete(listener); }, []);
  return select(state);
}
export function bump() { set({}); }

let toastTimer = null;
export function toast(message, kind = 'ok', undo = null) {
  clearTimeout(toastTimer);
  set({ toast: { message, kind, undo, id: Date.now() } });
  toastTimer = setTimeout(() => set({ toast: null }), undo ? 8000 : 4500);
}
export function fail(error) { toast(error?.message || String(error) || '操作失败，请重试', 'error'); }
export function confirm(title, message, yes = '确认', danger = true) {
  return new Promise((resolve) => set({ confirm: { title, message, yes, danger, resolve } }));
}
export function go(page, params = {}) {
  const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '' && v !== undefined && v !== null)).toString();
  location.hash = '#/' + page + (query ? '?' + query : '');
}
export const isInboxTag = (tag) => state.inboxTags.includes(tag);
