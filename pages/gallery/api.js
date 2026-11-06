// Bridge wrapper plus batched, in-memory thumbnail and preview caches.
// The page runs in a sandboxed iframe without storage, so caches live in memory.
const bridge = window.AstrBotPluginPage;

export const available = Boolean(bridge);
export const ready = () => (bridge ? bridge.ready() : Promise.reject(new Error('NO_BRIDGE')));
export const get = (endpoint, params = {}) => bridge.apiGet(endpoint, params);
export const post = (endpoint, body = {}) => bridge.apiPost(endpoint, body);
export const upload = (endpoint, file) => bridge.upload(endpoint, file);
export const download = (endpoint, params = {}, name) => bridge.download(endpoint, params, name);
export const action = (name, body = {}) => post('action', { action: name, ...body });

function lru(limit) {
  const map = new Map();
  return {
    has: (key) => map.has(key),
    get(key) { const value = map.get(key); if (map.has(key)) { map.delete(key); map.set(key, value); } return value; },
    set(key, value) { map.delete(key); map.set(key, value); while (map.size > limit) map.delete(map.keys().next().value); },
    delete: (key) => map.delete(key),
    clear: () => map.clear(),
  };
}

const thumbs = lru(2000);
const waiting = new Map();
const queue = new Set();
let timer = null;

export function thumb(id) {
  if (thumbs.has(id)) return Promise.resolve(thumbs.get(id));
  return new Promise((resolve) => {
    if (!waiting.has(id)) waiting.set(id, []);
    waiting.get(id).push(resolve);
    queue.add(id);
    if (!timer) timer = setTimeout(flush, 12);
  });
}
async function flush() {
  timer = null;
  // Small batches so the first screen appears while later thumbnails are still being made.
  const ids = [...queue].slice(0, 24);
  ids.forEach((id) => queue.delete(id));
  if (queue.size) timer = setTimeout(flush, 12);
  let got = {};
  try { got = (await get('thumbs', { ids: ids.join(',') })).thumbs || {}; } catch { /* resolve as missing */ }
  for (const id of ids) {
    const url = got[String(id)] ?? null;
    if (url) thumbs.set(id, url);
    (waiting.get(id) || []).forEach((resolve) => resolve(url));
    waiting.delete(id);
  }
}
export function forget(id) { thumbs.delete(id); previews.delete(id); }

const previews = lru(40);
export async function detail(id, fresh = false) {
  if (!fresh && previews.has(id)) return previews.get(id);
  const data = await get('image/' + id);
  previews.set(id, data);
  return data;
}
