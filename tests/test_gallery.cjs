const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');

class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.textContent = ''; this.className = ''; this.style = {}; this.dataset = {}; this.value = ''; this.listeners = {}; this.classList = { toggle: () => {} }; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute() {}
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  showModal() { this.open = true; }
  close(value) { if (value !== undefined) this.returnValue = value; this.open = false; const listeners = this.listeners.close || []; this.listeners.close = []; listeners.forEach(fn => fn()); }
}
const root = path.join(__dirname, '../pages/gallery');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const ids = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(m => [m[1], new Element()]));
const add = new Element('button'); add.dataset.action = 'add';
const remove = new Element('button'); remove.dataset.action = 'remove';
ids.recycle.dataset.action = 'delete';
const buttons = [add, remove, ids.recycle];
const requests = [];
let data = Array.from({length: 14}, (_, i) => ({id: i + 1, tags: [i % 2 ? '猫' : '日常'], thumbnail: 'data:image/jpeg;base64,fixture', caption: '', ocr: '', deleted: null}));
const bridge = {
  ready: async () => ({}),
  apiGet: async (endpoint, args) => {
    assert.equal(endpoint, 'gallery');
    let filtered = data.filter(item => Boolean(item.deleted) === (args.deleted === '1'));
    if (args.q) filtered = filtered.filter(item => item.tags.includes(args.q));
    const pages = Math.max(1, Math.ceil(filtered.length / 12)); const page = Math.min(pages, args.page);
    return {items: filtered.slice((page - 1) * 12, page * 12), total: filtered.length, page, pages, ai_enabled: false};
  },
  apiPost: async (endpoint, args) => {
    requests.push({endpoint, ...args});
    for (const item of data.filter(i => (args.ids || []).includes(i.id))) {
      if (args.action === 'delete') item.deleted = true;
      if (args.action === 'restore') item.deleted = null;
      if (args.action === 'replace') item.tags = args.tags.split(/\s+/);
      if (args.action === 'add') item.tags.push(...args.tags.split(/\s+/));
    }
    return {message: 'done'};
  },
  download: async () => {}, upload: async () => {},
};
const document = {
  getElementById: id => { assert.ok(ids[id], `Unknown ID ${id}`); return ids[id]; },
  createElement: tag => new Element(tag),
  querySelectorAll: selector => selector === '[data-action]' ? buttons : [add, remove],
};
vm.runInNewContext(fs.readFileSync(path.join(root, 'app.js'), 'utf8'), {window: {AstrBotPluginPage: bridge}, document, console});
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
function selectFirst() { const check = ids.grid.children[0].children[0].children[1]; check.checked = true; check.onchange(); }
(async () => {
  await flush();
  assert.equal(ids.grid.children.length, 12); assert.equal(ids.page.textContent, '1 / 2');
  await ids.next.onclick(); assert.equal(ids.grid.children.length, 2); assert.equal(ids.page.textContent, '2 / 2');
  ids.query.value = '猫'; ids.search.onsubmit({preventDefault() {}}); await flush();
  assert.equal(ids.grid.children.length, 7); assert.equal(ids.page.textContent, '1 / 1');
  selectFirst(); ids['batch-tags'].value = '憋笑'; await add.onclick(); assert.equal(requests.at(-1).action, 'add');
  const edit = ids.grid.children[0].children[1].children[2].children[1]; edit.onclick();
  assert.equal(ids.editor.open, true); assert.equal(ids.describe.disabled, true);
  ids['edit-tags'].value = '猫 可爱'; await ids['save-edit'].onclick(); assert.equal(requests.at(-1).action, 'replace');
  selectFirst(); const deletion = ids.recycle.onclick(); await flush();
  assert.equal(ids['confirm-dialog'].open, true); ids['confirm-dialog'].close('yes'); await deletion;
  assert.equal(requests.at(-1).action, 'delete');
  await ids.trash.onclick(); assert.equal(ids.grid.children.length, 1); assert.equal(ids.recycle.dataset.action, 'restore');
  selectFirst(); await ids.recycle.onclick(); assert.equal(requests.at(-1).action, 'restore');
  await ids.active.onclick(); selectFirst(); const before = requests.length; const cancellation = ids.recycle.onclick(); await flush(); ids['confirm-dialog'].close('no'); await cancellation; assert.equal(requests.length, before);
  assert.equal(/\bconfirm\(/.test(fs.readFileSync(path.join(root, 'app.js'), 'utf8')), false);
  console.log('PASS: gallery initial load, search, pagination, batch tags, editor, AI-disabled state, recycle confirmation/cancel and restore (DOM unit harness, not a live browser).');
})().catch(error => { console.error(error); process.exitCode = 1; });
