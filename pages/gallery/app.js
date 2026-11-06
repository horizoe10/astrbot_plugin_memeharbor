const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
const GALLERY_VIEWS = new Set(['all', 'inbox', 'trash']);
const TITLES = { all: '全部图片', inbox: '待整理', trash: '回收站', similar: '相似图片', stats: '统计' };
const state = {
  view: 'all', q: '', sort: 'new', size: 48, page: 1, pages: 1, total: 0, items: [],
  tags: [], inboxTags: ['待整理', '临时'], aiEnabled: false,
  selected: new Set(), allResults: false, lastIndex: null,
  active: null, detail: null, chips: [], dirty: false, suggestions: [],
};
const previews = new Map();
let loadSeq = 0;

function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.class) node.className = options.class;
  if (options.text !== undefined) node.textContent = options.text;
  for (const [key, value] of Object.entries(options.attrs || {})) node.setAttribute(key, value);
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
}
function status(message, kind = '') { const box = $('status'); box.textContent = message || ''; box.className = 'status' + (kind ? ' ' + kind : ''); }
async function run(action) {
  try { return await action(); } catch (error) { status(error?.message || '操作失败，请重试', 'error'); }
}
function confirmAction(title, message, yes = '确认', danger = true) {
  return new Promise((resolve) => {
    const dialog = $('confirm-dialog');
    $('confirm-title').textContent = title; $('confirm-message').textContent = message;
    $('confirm-yes').textContent = yes; $('confirm-yes').className = danger ? 'danger' : '';
    dialog.returnValue = 'no';
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'yes'), { once: true });
    dialog.showModal();
  });
}
function splitTags(value) { return value.split(/[\s,，、;；]+/).map((t) => t.trim()).filter(Boolean); }
function isInboxTag(tag) { return state.inboxTags.includes(tag); }
function formatDate(seconds) { const d = new Date(seconds * 1000); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
function formatSize(bytes) { return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' KB'; }
function queryTokens() { return state.q.split(/[\s|]+/).filter(Boolean); }

/* ---------- tags & suggestions ---------- */
async function loadTags() {
  const data = await bridge.apiGet('tags');
  state.tags = data.tags; state.inboxTags = data.inbox_tags || state.inboxTags;
  renderTags();
  $('tag-options').replaceChildren(...state.tags.map((t) => el('option', { attrs: { value: t.tag } })));
}
function renderTags() {
  const filter = $('tag-filter').value.trim().toLowerCase();
  const active = new Set(queryTokens());
  const shown = state.tags.filter((t) => !filter || t.tag.toLowerCase().includes(filter)).slice(0, 300);
  $('tag-list').replaceChildren(...(shown.length ? shown.map((t) => {
    const button = el('button', { class: active.has(t.tag) ? 'active' : '', attrs: { type: 'button', title: '点击筛选，再点一次取消' } }, [el('span', { text: t.tag }), el('small', { text: String(t.count) })]);
    button.onclick = () => toggleTagFilter(t.tag);
    return el('li', {}, button);
  }) : [el('li', { class: 'hint', text: state.tags.length ? '没有匹配的标签' : '还没有标签' })]));
}
function toggleTagFilter(tag) {
  const tokens = queryTokens();
  const next = tokens.includes(tag) ? tokens.filter((t) => t !== tag) : [...tokens, tag];
  $('query').value = next.join(' ');
  submitSearch(GALLERY_VIEWS.has(state.view) ? state.view : 'all');
}
function updateSuggestions() {
  const input = $('query');
  const before = input.value.slice(0, input.selectionStart ?? input.value.length);
  const token = (before.split(/[\s|]+/).pop() || '').replace(/^-/, '');
  const box = $('suggest');
  if (!token) { box.hidden = true; state.suggestions = []; return; }
  const lower = token.toLowerCase();
  state.suggestions = state.tags.filter((t) => t.tag.toLowerCase().includes(lower) && t.tag !== token)
    .sort((a, b) => (b.tag.toLowerCase().startsWith(lower) - a.tag.toLowerCase().startsWith(lower)) || b.count - a.count).slice(0, 8);
  box.replaceChildren(...state.suggestions.map((t, i) => {
    const li = el('li', { attrs: { role: 'option', 'aria-selected': i === 0 ? 'true' : 'false' } }, [el('span', { text: t.tag }), el('small', { text: t.count + ' 张' })]);
    li.onmousedown = (event) => { event.preventDefault(); applySuggestion(t.tag); };
    return li;
  }));
  box.hidden = !state.suggestions.length;
}
function applySuggestion(tag) {
  const input = $('query');
  const caret = input.selectionStart ?? input.value.length;
  const before = input.value.slice(0, caret); const after = input.value.slice(caret);
  const match = before.match(/(^|[\s|])(-?)([^\s|]*)$/);
  const start = before.length - match[3].length;
  input.value = before.slice(0, start) + tag + ' ' + after.trimStart();
  input.setSelectionRange(start + tag.length + 1, start + tag.length + 1);
  $('suggest').hidden = true;
  input.focus();
}
function moveSuggestion(step) {
  const items = [...$('suggest').children]; if (!items.length) return false;
  const current = items.findIndex((li) => li.getAttribute('aria-selected') === 'true');
  const next = (current + step + items.length) % items.length;
  items.forEach((li, i) => li.setAttribute('aria-selected', String(i === next)));
  return true;
}

/* ---------- gallery ---------- */
async function loadGallery() {
  const seq = ++loadSeq;
  status('正在加载…');
  const data = await bridge.apiGet('gallery', { q: state.q, view: state.view, sort: state.sort, page: state.page, page_size: state.size });
  if (seq !== loadSeq) return;
  Object.assign(state, { items: data.items, page: data.page, pages: data.pages, total: data.total, aiEnabled: data.ai_enabled });
  $('count-all').textContent = data.counts.all; $('count-inbox').textContent = data.counts.inbox; $('count-trash').textContent = data.counts.trash;
  renderGrid();
  status('');
}
function renderGrid() {
  const grid = $('grid');
  $('view-title').textContent = TITLES[state.view] + (state.q ? '：' + state.q : '');
  $('result-text').textContent = state.total + ' 张';
  if (!state.items.length) {
    const message = state.view === 'trash' ? '回收站是空的' : state.view === 'inbox' ? '没有待整理的图片，全部整理好了' : state.q ? '没有匹配的图片，换个标签试试' : '图库还是空的，拖入图片或文件夹开始导入';
    grid.replaceChildren(el('div', { class: 'empty', text: message }));
  } else {
    grid.replaceChildren(...state.items.map((item, index) => card(item, index)));
  }
  $('page').textContent = state.page + ' / ' + state.pages;
  $('prev').disabled = state.page <= 1; $('next').disabled = state.page >= state.pages;
  renderBatch();
}
function card(item, index) {
  const picked = state.allResults || state.selected.has(item.id);
  const node = el('article', { class: 'card' + (picked ? ' picked' : '') + (state.active === item.id ? ' active' : ''), attrs: { tabindex: '0', 'data-id': item.id } });
  const thumb = el('div', { class: 'thumb' });
  if (item.thumbnail) thumb.append(el('img', { attrs: { src: item.thumbnail, alt: '图片 ' + item.id + ' ' + item.tags.join(' '), loading: 'lazy' } }));
  else thumb.append(el('div', { class: 'missing', text: '原图缺失或无法预览' }));
  const check = el('input', { attrs: { type: 'checkbox', 'aria-label': '选择图片 ' + item.id } });
  check.checked = picked;
  check.onclick = (event) => { event.stopPropagation(); toggleSelect(item, index, event.shiftKey); };
  const badges = el('div', { class: 'badges' });
  if (item.gif) badges.append(el('span', { class: 'badge', text: 'GIF' }));
  const tags = el('div', { class: 'card-tags' });
  const visible = item.tags.filter((t) => !isInboxTag(t));
  visible.slice(0, 3).forEach((t) => tags.append(el('span', { class: 'tag', text: t })));
  if (visible.length > 3) tags.append(el('span', { class: 'tag muted', text: '+' + (visible.length - 3) }));
  if (!visible.length) tags.append(el('span', { class: 'tag warn', text: item.tags.length ? '待整理' : '未打标签' }));
  const body = el('div', { class: 'card-body' }, [el('div', { class: 'card-id' }, [el('span', { text: '#' + item.id }), el('span', { text: item.sent ? '发送 ' + item.sent + ' 次' : '' })]), tags]);
  node.append(thumb, el('label', { class: 'check' }, check), badges, body);
  node.onclick = (event) => { if (event.shiftKey || event.ctrlKey || event.metaKey) toggleSelect(item, index, event.shiftKey); else openDetail(item.id); };
  node.onkeydown = (event) => { if (event.key === 'Enter') openDetail(item.id); if (event.key === ' ') { event.preventDefault(); toggleSelect(item, index, false); } };
  return node;
}
function toggleSelect(item, index, range) {
  if (state.allResults) { state.allResults = false; state.items.forEach((i) => state.selected.add(i.id)); }
  if (range && state.lastIndex !== null) {
    const [from, to] = [Math.min(state.lastIndex, index), Math.max(state.lastIndex, index)];
    const on = !state.selected.has(item.id);
    state.items.slice(from, to + 1).forEach((i) => on ? state.selected.add(i.id) : state.selected.delete(i.id));
  } else if (state.selected.has(item.id)) state.selected.delete(item.id);
  else state.selected.add(item.id);
  state.lastIndex = index;
  renderGrid();
}
function clearSelection() { state.selected.clear(); state.allResults = false; state.lastIndex = null; }
function renderBatch() {
  const count = state.allResults ? state.total : state.selected.size;
  $('batch').hidden = count === 0;
  $('selected-text').textContent = state.allResults ? '已选中全部 ' + state.total + ' 张搜索结果' : '已选 ' + count + ' 张';
  const more = !state.allResults && count > 0 && state.total > count;
  $('select-results').hidden = !more;
  $('select-results').textContent = '选中全部 ' + state.total + ' 张结果';
  const trash = state.view === 'trash';
  document.querySelectorAll('[data-batch=add],[data-batch=remove]').forEach((b) => { b.hidden = trash; });
  $('batch-tags').parentElement.hidden = trash;
  $('batch-recycle').textContent = trash ? '恢复所选' : '移入回收站';
  $('batch-recycle').dataset.batch = trash ? 'restore' : 'delete';
  $('batch-recycle').className = trash ? '' : 'danger';
  const pageIds = state.items.map((i) => i.id);
  $('select-page').checked = pageIds.length > 0 && (state.allResults || pageIds.every((id) => state.selected.has(id)));
}
function selectionPayload() { return state.allResults ? { select: { q: state.q, view: state.view } } : { ids: [...state.selected] }; }
async function batch(action) {
  const count = state.allResults ? state.total : state.selected.size;
  if (!count) throw new Error('请先选择图片');
  const tags = $('batch-tags').value.trim();
  if ((action === 'add' || action === 'remove') && !tags) throw new Error('请先输入标签');
  if (action === 'delete' && !await confirmAction('移入回收站', '把所选的 ' + count + ' 张图片移入回收站？之后可以在回收站恢复。', '移入回收站')) return;
  const result = await bridge.apiPost('action', { action, tags, ...selectionPayload() });
  clearSelection(); $('batch-tags').value = '';
  await Promise.all([loadGallery(), loadTags()]);
  status(result.message, 'ok');
}
function submitSearch(view = state.view) {
  state.q = $('query').value.trim(); state.page = 1; clearSelection();
  $('suggest').hidden = true; $('clear-query').hidden = !state.q;
  switchView(GALLERY_VIEWS.has(view) ? view : 'all');
}

/* ---------- detail panel ---------- */
function findItem(id) { return state.items.find((i) => i.id === id); }
async function openDetail(id) {
  if (state.dirty && state.active && state.active !== id) await saveDetail(false, true);
  state.active = id; state.seen.add(id);
  document.querySelectorAll('.card.active').forEach((c) => c.classList.remove('active'));
  document.querySelector('.card[data-id="' + id + '"]')?.classList.add('active');
  $('detail').classList.add('open');
  $('detail-empty').hidden = true; $('detail-body').hidden = false;
  $('ai-result').replaceChildren();
  const known = findItem(id);
  if (known) fillDetail(known);
  $('detail-image').hidden = true; $('detail-loading').hidden = false; $('detail-loading').textContent = '正在加载原图…';
  try {
    let full = previews.get(id);
    if (!full) {
      full = await bridge.apiGet('image/' + id);
      previews.set(id, full);
      if (previews.size > 24) previews.delete(previews.keys().next().value);
    }
    if (state.active !== id) return;
    if (!known) fillDetail(full);
    state.detail = { ...full, tags: (known || full).tags };
    $('detail-image').src = full.preview; $('detail-image').alt = '图片 ' + id;
    $('detail-image').hidden = false; $('detail-loading').hidden = true;
    const parts = [full.width + ' × ' + full.height, formatSize(full.bytes), (full.animated ? '动图' : '静态图'), '收集于 ' + formatDate(full.created), '已发送 ' + full.sent + ' 次'];
    $('detail-meta').textContent = parts.join(' · ');
  } catch (error) {
    if (state.active === id) $('detail-loading').textContent = error.message || '原图读取失败';
  }
}
function fillDetail(item) {
  state.detail = item; state.dirty = false;
  state.chips = [...item.tags];
  $('detail-title').textContent = '#' + item.id;
  $('detail-origin').textContent = item.origin || '';
  $('detail-meta').textContent = '收集于 ' + formatDate(item.created) + ' · 已发送 ' + item.sent + ' 次';
  $('detail-text').textContent = [item.caption && '描述：' + item.caption, item.ocr && '图中文字：' + item.ocr].filter(Boolean).join('\n');
  const deleted = Boolean(item.deleted);
  $('chips').classList.toggle('readonly', deleted);
  $('chip-input').disabled = deleted;
  $('save').hidden = $('save-next').hidden = deleted;
  $('describe').hidden = deleted || !state.aiEnabled;
  $('detail-recycle').textContent = deleted ? '恢复这张图片' : '移入回收站';
  $('detail-recycle').className = deleted ? 'ghost' : 'ghost danger-text';
  $('detail-hint').textContent = deleted ? '这张图片在回收站里，恢复后才能修改标签' : state.view === 'inbox' ? '保存时会自动去掉「待整理」，然后跳到下一张' : '';
  renderChips();
}
function renderChips() {
  const box = $('chips'); const input = $('chip-input');
  box.replaceChildren(...state.chips.map((tag, index) => {
    const remove = el('button', { text: '×', attrs: { type: 'button', 'aria-label': '移除标签 ' + tag } });
    remove.onclick = () => { state.chips.splice(index, 1); state.dirty = true; renderChips(); input.focus(); };
    return el('span', { class: 'chip' + (isInboxTag(tag) ? ' warn' : '') }, [el('span', { text: tag }), remove]);
  }), input);
}
function addChips(value) {
  const tags = splitTags(value).filter((t) => !state.chips.includes(t));
  if (!tags.length) return false;
  state.chips.push(...tags); state.dirty = true; renderChips(); return true;
}
async function saveDetail(next = false, quiet = false) {
  const item = state.detail; if (!item || item.deleted) return;
  addChips($('chip-input').value); $('chip-input').value = '';
  let tags = [...state.chips];
  // Finishing an image moves it out of the inbox: drop the inbox markers once real tags exist.
  if (tags.some((t) => !isInboxTag(t))) tags = tags.filter((t) => !isInboxTag(t));
  const id = item.id;
  await bridge.apiPost('action', { action: 'replace', ids: [id], tags: tags.join(' ') });
  state.dirty = false;
  const index = state.items.findIndex((i) => i.id === id);
  const local = findItem(id);
  if (local) { local.tags = tags; local.inbox = !tags.length || tags.some(isInboxTag); }
  previews.delete(id);
  const leaves = state.view === 'inbox' && local && !local.inbox;
  if (leaves) { state.items.splice(index, 1); state.total -= 1; }
  renderGrid(); loadTags().catch(() => {});
  if (leaves) refreshCounts().catch(() => {});
  if (!quiet) status('图片 ' + id + ' 的标签已保存', 'ok');
  if (next) await stepDetail(1, leaves ? index - 1 : index);
  else if (!quiet) { state.chips = tags; renderChips(); }
}
async function stepDetail(step, from = null) {
  const index = from ?? state.items.findIndex((i) => i.id === state.active);
  const target = index + step;
  if (target >= 0 && target < state.items.length) return openDetail(state.items[target].id);
  if (step > 0 && state.view === 'inbox') {
    // Finished images leave the inbox, so reload this page instead of jumping past items.
    await loadGallery();
    let fresh = state.items.find((i) => !state.seen.has(i.id));
    while (!fresh && state.page < state.pages) { state.page += 1; await loadGallery(); fresh = state.items.find((i) => !state.seen.has(i.id)); }
    if (fresh) return openDetail(fresh.id);
    if (!state.items.length) { closeDetail(); return status('待整理的图片都处理完了', 'ok'); }
    return status('这一轮看完了，剩下的是你跳过的图片');
  }
  if (step > 0 && state.page < state.pages) { state.page += 1; await loadGallery(); if (state.items.length) return openDetail(state.items[0].id); }
  else if (step < 0 && state.page > 1) { state.page -= 1; await loadGallery(); if (state.items.length) return openDetail(state.items[state.items.length - 1].id); }
  else status(step > 0 ? '已经是最后一张了' : '已经是第一张了');
}
function closeDetail() {
  state.active = null; state.detail = null; state.dirty = false;
  $('detail').classList.remove('open'); $('detail-body').hidden = true; $('detail-empty').hidden = false;
  document.querySelectorAll('.card.active').forEach((c) => c.classList.remove('active'));
}
async function recycleDetail() {
  const item = state.detail; if (!item) return;
  const restore = Boolean(item.deleted);
  if (!restore && !await confirmAction('移入回收站', '把图片 #' + item.id + ' 移入回收站？之后可以恢复。', '移入回收站')) return;
  const index = state.items.findIndex((i) => i.id === item.id);
  await bridge.apiPost('action', { action: restore ? 'restore' : 'delete', ids: [item.id] });
  previews.delete(item.id);
  state.dirty = false;
  await Promise.all([loadGallery(), loadTags()]);
  status('图片 ' + item.id + (restore ? ' 已恢复' : ' 已移入回收站'), 'ok');
  if (state.items.length) await openDetail(state.items[Math.min(Math.max(index, 0), state.items.length - 1)].id); else closeDetail();
}
async function describeDetail() {
  const item = state.detail; if (!item) return;
  const box = $('ai-result');
  box.replaceChildren(document.createTextNode('正在识别，最长约 45 秒…'));
  const result = await bridge.apiPost('action', { action: 'describe', ids: [item.id] }).catch((error) => { box.textContent = error.message; return null; });
  if (!result) return;
  const accept = el('button', { class: 'secondary', text: '采纳建议标签', attrs: { type: 'button' } });
  accept.onclick = () => { addChips(result.tags.join(' ')); accept.remove(); };
  box.replaceChildren(document.createTextNode(result.caption + '\n图中文字：' + (result.ocr || '无') + '\n建议标签：' + (result.tags.join('、') || '无') + '\n'), accept);
  $('detail-text').textContent = ['描述：' + result.caption, result.ocr && '图中文字：' + result.ocr].filter(Boolean).join('\n');
}

/* ---------- similar & stats ---------- */
let similarTimer = null;
async function loadSimilar() {
  clearTimeout(similarTimer);
  const data = await bridge.apiGet('similar');
  if (state.view !== 'similar') return;
  $('count-similar').textContent = data.total_groups || '';
  $('similar-text').textContent = data.pending ? '正在分析旧图片，还剩 ' + data.pending + ' 张…' : '共 ' + data.total_groups + ' 组' + (data.total_groups > data.groups.length ? '，先显示前 ' + data.groups.length + ' 组' : '');
  const list = $('similar-list');
  if (!data.groups.length) list.replaceChildren(el('div', { class: 'empty', text: data.pending ? '正在分析，请稍候…' : '没有发现相似的图片' }));
  else list.replaceChildren(...data.groups.map(similarGroup));
  if (data.pending) similarTimer = setTimeout(() => run(loadSimilar), 400);
}
function similarGroup(group) {
  let keep = group[0].id;
  const box = el('div', { class: 'similar-group' });
  const items = el('div', { class: 'similar-items' });
  const name = 'keep-' + group[0].id;
  const nodes = group.map((item) => {
    const radio = el('input', { attrs: { type: 'radio', name, value: item.id } });
    radio.checked = item.id === keep;
    const node = el('div', { class: 'similar-item' + (item.id === keep ? ' keep' : '') }, [
      el('div', { class: 'thumb' }, item.thumbnail ? el('img', { attrs: { src: item.thumbnail, alt: '图片 ' + item.id } }) : el('div', { class: 'missing', text: '无法预览' })),
      el('div', { class: 'card-body' }, [el('label', {}, [radio, document.createTextNode('#' + item.id + ' 保留这张')]), el('div', { class: 'card-tags' }, (item.tags.length ? item.tags.slice(0, 3) : ['未打标签']).map((t) => el('span', { class: 'tag', text: t })))]),
    ]);
    const choose = () => { keep = item.id; radio.checked = true; nodes.forEach((n) => n.classList.toggle('keep', n === node)); };
    node.onclick = choose;
    return node;
  });
  items.append(...nodes);
  const merge = el('button', { text: '合并到选中的图片', attrs: { type: 'button' } });
  merge.onclick = () => run(async () => {
    const ids = group.map((i) => i.id);
    if (!await confirmAction('合并相似图片', '保留 #' + keep + '，其余 ' + (ids.length - 1) + ' 张的标签和发送次数并入这张后移入回收站？', '合并', false)) return;
    const result = await bridge.apiPost('action', { action: 'merge', keep, ids });
    status(result.message, 'ok'); await Promise.all([loadSimilar(), loadTags(), refreshCounts()]);
  });
  const dismiss = el('button', { class: 'secondary', text: '不是同一张', attrs: { type: 'button' } });
  dismiss.onclick = () => run(async () => {
    const result = await bridge.apiPost('action', { action: 'dismiss', ids: group.map((i) => i.id) });
    status(result.message, 'ok'); box.remove();
  });
  box.append(items, el('div', { class: 'similar-actions' }, [el('p', { class: 'hint', text: group.length + ' 张看起来几乎一样' }), dismiss, merge]));
  return box;
}
async function refreshCounts() {
  const data = await bridge.apiGet('gallery', { view: 'all', page_size: 24, page: 1 });
  $('count-all').textContent = data.counts.all; $('count-inbox').textContent = data.counts.inbox; $('count-trash').textContent = data.counts.trash;
}
async function loadStats() {
  const s = await bridge.apiGet('stats');
  const cards = [['图片', s.total], ['待整理', s.inbox], ['标签', s.tags], ['累计发送', s.sent], ['近 7 天新增', s.added_week], ['回收站', s.deleted]];
  $('stat-cards').replaceChildren(...cards.map(([label, value]) => el('div', { class: 'stat' }, [el('b', { text: String(value) }), el('span', { text: label })])));
  const max = Math.max(1, ...s.top_tags.map((t) => t.count));
  $('top-tags').replaceChildren(...(s.top_tags.length ? s.top_tags.map((t) => {
    const bar = el('i'); bar.style.width = Math.max(4, (t.count / max) * 100) + '%';
    const button = el('button', { attrs: { type: 'button', title: '查看带「' + t.tag + '」的图片' } }, [el('span', { text: t.tag }), el('div', {}, bar), el('em', { text: String(t.count) })]);
    button.onclick = () => { $('query').value = t.tag; submitSearch('all'); };
    return el('li', {}, button);
  }) : [el('li', { class: 'hint', text: '还没有标签' })]));
  $('top-sent').replaceChildren(...(s.top_sent.length ? s.top_sent.map((item) => {
    const node = el('div', { class: 'card' }, [el('div', { class: 'thumb' }, item.thumbnail ? el('img', { attrs: { src: item.thumbnail, alt: '图片 ' + item.id } }) : null), el('div', { class: 'card-body', text: '#' + item.id + ' · ' + item.sent + ' 次' })]);
    node.onclick = () => openDetail(item.id);
    return node;
  }) : [el('p', { class: 'hint', text: '还没有发送记录' })]));
}

/* ---------- views ---------- */
async function switchView(view) {
  const changed = view !== state.view;
  state.view = view;
  if (changed) { state.page = 1; clearSelection(); state.seen = new Set(); }
  document.querySelectorAll('.view').forEach((b) => b.classList.toggle('selected', b.dataset.view === view));
  const gallery = GALLERY_VIEWS.has(view);
  $('gallery-view').hidden = !gallery; $('similar-view').hidden = view !== 'similar'; $('stats-view').hidden = view !== 'stats';
  renderTags();
  if (gallery) await loadGallery();
  else { $('batch').hidden = true; if (view === 'similar') await loadSimilar(); else await loadStats(); }
}

/* ---------- import ---------- */
const IMAGE = /\.(png|jpe?g|gif|webp|bmp)$/i;
let importing = false;
function importable(file, path = file.name) {
  return (IMAGE.test(file.name) || /\.zip$/i.test(file.name)) && !path.split('/').some((p) => p === '__MACOSX' || p.startsWith('._'));
}
async function importFiles(entries) {
  // A drop during a running import would race the progress bar and totals.
  if (importing) { status('正在导入上一批文件，请等它完成后再导入'); return; }
  const files = entries.filter(({ file, path }) => importable(file, path));
  const skipped = entries.length - files.length;
  if (!files.length) { status(entries.length ? '没有可以导入的图片或 ZIP' : '', entries.length ? 'error' : ''); return; }
  importing = true;
  const totals = { added: 0, duplicates: 0, skipped, failed: 0 };
  const errors = [];
  $('progress').hidden = false;
  document.querySelectorAll('#upload,#upload-folder').forEach((i) => { i.disabled = true; });
  try {
    for (let index = 0; index < files.length; index++) {
      const { file, path } = files[index];
      $('progress-fill').style.width = (index / files.length) * 100 + '%';
      $('progress-text').textContent = '正在导入 ' + (index + 1) + ' / ' + files.length + '：' + path + '（已新增 ' + totals.added + ' 张）';
      try {
        const result = await bridge.upload('upload', file);
        totals.added += result.added; totals.duplicates += result.duplicates; totals.skipped += result.skipped; totals.failed += result.failed;
        (result.errors || []).forEach((e) => errors.length < 20 && errors.push(path + '：' + e));
      } catch (error) { totals.failed += 1; if (errors.length < 20) errors.push(path + '：' + error.message); }
    }
    $('progress-fill').style.width = '100%';
  } finally {
    importing = false;
    document.querySelectorAll('#upload,#upload-folder').forEach((i) => { i.disabled = false; i.value = ''; });
    setTimeout(() => { $('progress').hidden = true; $('progress-fill').style.width = '0'; }, 1200);
  }
  const summary = '导入完成：新增 ' + totals.added + ' 张，已存在 ' + totals.duplicates + ' 张，跳过 ' + totals.skipped + ' 个非图片文件，失败 ' + totals.failed + ' 项。';
  await Promise.all([loadTags(), GALLERY_VIEWS.has(state.view) ? loadGallery() : refreshCounts()]).catch(() => {});
  status(summary + (errors.length ? ' ' + errors.join('；') : totals.added ? ' 新图片在「待整理」里。' : ''), totals.failed ? 'error' : 'ok');
}
async function readEntry(entry, prefix = '') {
  if (entry.isFile) return new Promise((resolve) => entry.file((file) => resolve([{ file, path: prefix + file.name }]), () => resolve([])));
  if (!entry.isDirectory) return [];
  const reader = entry.createReader(); const children = [];
  for (;;) { const batchEntries = await new Promise((resolve) => reader.readEntries(resolve, () => resolve([]))); if (!batchEntries.length) break; children.push(...batchEntries); }
  const nested = await Promise.all(children.map((child) => readEntry(child, prefix + entry.name + '/')));
  return nested.flat();
}

/* ---------- events ---------- */
function typing(target) { return target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable); }
function bind() {
  $('search').onsubmit = (event) => { event.preventDefault(); run(() => submitSearch()); };
  $('query').addEventListener('input', updateSuggestions);
  $('query').addEventListener('blur', () => setTimeout(() => { $('suggest').hidden = true; }, 120));
  $('query').addEventListener('keydown', (event) => {
    if ($('suggest').hidden) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { if (moveSuggestion(event.key === 'ArrowDown' ? 1 : -1)) event.preventDefault(); }
    else if (event.key === 'Tab' || (event.key === 'Enter' && !event.ctrlKey)) {
      const pick = [...$('suggest').children].findIndex((li) => li.getAttribute('aria-selected') === 'true');
      if (pick >= 0 && state.suggestions[pick] && event.key === 'Tab') { event.preventDefault(); applySuggestion(state.suggestions[pick].tag); }
      else $('suggest').hidden = true;
    } else if (event.key === 'Escape') $('suggest').hidden = true;
  });
  $('clear-query').onclick = () => { $('query').value = ''; run(() => submitSearch()); };
  $('tag-filter').addEventListener('input', renderTags);
  $('sort').onchange = () => { state.sort = $('sort').value; state.page = 1; run(loadGallery); };
  $('page-size').onchange = () => { state.size = Number($('page-size').value); state.page = 1; run(loadGallery); };
  $('prev').onclick = () => run(async () => { state.page -= 1; await loadGallery(); window.scrollTo(0, 0); });
  $('next').onclick = () => run(async () => { state.page += 1; await loadGallery(); window.scrollTo(0, 0); });
  document.querySelectorAll('.view').forEach((button) => {
    // Choosing a view from the sidebar shows all of it, so drop any active search.
    button.onclick = () => run(() => {
      if (state.q) { state.q = ''; $('query').value = ''; $('clear-query').hidden = true; state.page = 1; }
      return switchView(button.dataset.view);
    });
  });
  $('select-page').onchange = () => {
    if ($('select-page').checked) state.items.forEach((i) => state.selected.add(i.id));
    else { if (state.allResults) clearSelection(); state.items.forEach((i) => state.selected.delete(i.id)); }
    renderGrid();
  };
  $('select-results').onclick = () => { state.allResults = true; renderGrid(); };
  $('select-clear').onclick = () => { clearSelection(); renderGrid(); };
  document.querySelectorAll('[data-batch]').forEach((button) => { button.onclick = () => run(() => batch(button.dataset.batch)); });
  $('batch-tags').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); run(() => batch('add')); } });

  $('chips').onclick = (event) => { if (event.target === $('chips')) $('chip-input').focus(); };
  $('chip-input').addEventListener('keydown', (event) => {
    const input = $('chip-input');
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); run(() => saveDetail(true)); }
    else if (event.key === 'Enter' || event.key === ' ' || event.key === ',' || event.key === '，') {
      if (input.value.trim()) { event.preventDefault(); addChips(input.value); input.value = ''; }
      else if (event.key === 'Enter') { event.preventDefault(); run(() => saveDetail(state.view === 'inbox')); }
    } else if (event.key === 'Backspace' && !input.value && state.chips.length) { state.chips.pop(); state.dirty = true; renderChips(); input.focus(); }
  });
  $('chip-input').addEventListener('change', () => { if (addChips($('chip-input').value)) $('chip-input').value = ''; });
  $('save').onclick = () => run(() => saveDetail(false));
  $('save-next').onclick = () => run(() => saveDetail(true));
  $('describe').onclick = () => run(describeDetail);
  $('detail-prev').onclick = () => run(() => stepDetail(-1));
  $('detail-next').onclick = () => run(() => stepDetail(1));
  $('detail-close').onclick = () => run(async () => { if (state.dirty) await saveDetail(false, true); closeDetail(); });
  $('download').onclick = () => run(() => bridge.download('file/' + state.active));
  $('detail-recycle').onclick = () => run(recycleDetail);

  document.addEventListener('keydown', (event) => {
    if (document.querySelector('dialog[open]')) return;
    if (event.key === '/' && !typing(event.target)) { event.preventDefault(); $('query').focus(); return; }
    if (!state.active || typing(event.target)) return;
    if (event.key === 'ArrowRight') { event.preventDefault(); run(() => stepDetail(1)); }
    else if (event.key === 'ArrowLeft') { event.preventDefault(); run(() => stepDetail(-1)); }
    else if (event.key === 'Escape') run(async () => { if (state.dirty) await saveDetail(false, true); closeDetail(); });
    else if (event.key === 'Delete') run(recycleDetail);
    else if (event.key === 't' || event.key === 'e') { event.preventDefault(); $('chip-input').focus(); }
  });

  $('rename').onclick = () => { $('old-tag').value = ''; $('new-tag').value = ''; $('rename-dialog').showModal(); };
  $('confirm-rename').onclick = () => run(async () => {
    const result = await bridge.apiPost('action', { action: 'rename', old: $('old-tag').value.trim(), new: $('new-tag').value.trim() });
    $('rename-dialog').close(); previews.clear();
    await Promise.all([loadTags(), GALLERY_VIEWS.has(state.view) ? loadGallery() : Promise.resolve()]);
    status(result.message, 'ok');
  });
  $('export').onclick = () => run(async () => { status('正在打包图片和标签…'); await bridge.download('export', {}, 'memeharbor-backup.zip'); status('备份已导出', 'ok'); });
  $('upload').onchange = () => run(() => importFiles([...$('upload').files].map((file) => ({ file, path: file.name }))));
  $('upload-folder').onchange = () => run(() => importFiles([...$('upload-folder').files].map((file) => ({ file, path: file.webkitRelativePath || file.name }))));

  let depth = 0;
  const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes('Files');
  window.addEventListener('dragenter', (event) => { if (!hasFiles(event)) return; depth += 1; $('drop').hidden = false; });
  window.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) $('drop').hidden = true; });
  window.addEventListener('dragover', (event) => { if (hasFiles(event)) event.preventDefault(); });
  window.addEventListener('drop', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault(); depth = 0; $('drop').hidden = true;
    const items = [...event.dataTransfer.items].map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
    run(async () => {
      const entries = items.length ? (await Promise.all(items.map((entry) => readEntry(entry)))).flat() : [...event.dataTransfer.files].map((file) => ({ file, path: file.name }));
      await importFiles(entries);
    });
  });
}

state.seen = new Set();
if (!bridge) status('请从 AstrBot 后台：插件 → 表情港湾 MemeHarbor → Pages 打开图库', 'error');
else run(async () => { bind(); await bridge.ready(); await Promise.all([loadTags(), loadGallery()]); });

