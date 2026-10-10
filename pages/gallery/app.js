const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
let page = 1, pages = 1, deleted = false, items = [], editId = null, suggestions = [], aiEnabled = false;
const selected = new Set();
let busy = false;
function status(message, error = false) { $('status').textContent = message; $('status').style.color = error ? '#b24b44' : ''; }
function selection() { $('selected').textContent = `已选 ${selected.size} 张`; $('select-all').checked = items.length > 0 && selected.size === items.length; }
async function run(action) {
  if (busy) return;
  busy = true;
  try { await action(); } catch (error) { status(error.message || '操作失败，请重试', true); }
  finally { busy = false; }
}
function node(tag, text, className) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el; }
function confirmRecycle(message) {
  return new Promise(resolve => {
    const dialog = $('confirm-dialog'); $('confirm-message').textContent = message; dialog.returnValue = 'no';
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'yes'), {once: true}); dialog.showModal();
  });
}
function render() {
  $('grid').replaceChildren();
  if (!items.length) $('grid').append(node('div', deleted ? '回收站为空' : '没有匹配图片，换个标签试试，或导入你的第一张表情。', 'empty'));
  for (const item of items) {
    const card = node('article', undefined, 'card');
    const wrap = node('div', undefined, 'image-wrap');
    if (item.thumbnail) { const img = node('img'); img.src = item.thumbnail; img.alt = `图片 #${item.id} ${item.tags.join(' ')}`; img.loading = 'lazy'; wrap.append(img); }
    else wrap.append(node('div', '原图缺失或无法预览', 'missing'));
    const check = node('input'); check.type = 'checkbox'; check.setAttribute('aria-label', `选择图片 ${item.id}`);
    check.checked = selected.has(item.id); card.classList.toggle('picked', check.checked);
    check.onchange = () => { check.checked ? selected.add(item.id) : selected.delete(item.id); card.classList.toggle('picked', check.checked); selection(); };
    wrap.append(check); const body = node('div', undefined, 'card-body'); body.append(node('span', `#${item.id}`, 'id'));
    const tags = node('div', undefined, 'tags'); for (const tag of item.tags) tags.append(node('span', tag, 'tag')); if (!item.tags.length) tags.append(node('span', '无标签', 'tag')); body.append(tags);
    const actions = node('div', undefined, 'card-actions');
    const download = node('button', '下载原图', 'secondary'); download.onclick = () => run(() => bridge.download(`file/${item.id}`)); actions.append(download);
    if (!deleted) { const edit = node('button', '编辑标签', 'secondary'); edit.onclick = () => {
      editId = item.id; suggestions = []; $('edit-title').textContent = `图片 #${item.id}`; $('edit-tags').value = item.tags.join(' ');
      $('edit-caption').textContent = [item.caption, item.ocr].filter(Boolean).join('\n'); $('ai-result').textContent = ''; $('accept-ai').hidden = true; $('describe').disabled = !aiEnabled;
      $('editor').showModal();
    }; actions.append(edit); }
    body.append(actions); card.append(wrap, body); $('grid').append(card);
  }
  $('active').classList.toggle('selected', !deleted); $('trash').classList.toggle('selected', deleted);
  $('recycle').textContent = deleted ? '恢复所选图片' : '移入回收站'; $('recycle').dataset.action = deleted ? 'restore' : 'delete';
  document.querySelectorAll('[data-action=add],[data-action=remove]').forEach(el => { el.disabled = deleted; });
  $('page').textContent = `${page} / ${pages}`; $('prev').disabled = page <= 1; $('next').disabled = page >= pages;
  selection();
}
async function load() {
  status('正在加载图库…');
  const data = await bridge.apiGet('gallery', { q: $('query').value, page, deleted: deleted ? '1' : '0' });
  items = data.items; page = data.page; pages = data.pages; aiEnabled = data.ai_enabled; selected.clear();
  $('stats').textContent = `${data.total} 张图片`; render();
  status(data.migration_warnings?.length ? `迁移时有 ${data.migration_warnings.length} 个文件未导入，原文件仍保留。请查看插件日志。` : '');
}
$('search').onsubmit = e => { e.preventDefault(); run(async () => { page = 1; await load(); }); };
$('active').onclick = () => run(async () => { deleted = false; page = 1; await load(); });
$('trash').onclick = () => run(async () => { deleted = true; page = 1; await load(); });
$('prev').onclick = () => run(async () => { page--; await load(); });
$('next').onclick = () => run(async () => { page++; await load(); });
$('select-all').onchange = () => { selected.clear(); if ($('select-all').checked) items.forEach(i => selected.add(i.id)); render(); };
document.querySelectorAll('[data-action]').forEach(button => button.onclick = () => run(async () => {
  if (!selected.size) throw new Error('请先选择图片');
  const action = button.dataset.action;
  if (action === 'delete' && !await confirmRecycle(`将所选 ${selected.size} 张图片移入回收站？之后可恢复。`)) return;
  const result = await bridge.apiPost('action', { action, ids: [...selected], tags: $('batch-tags').value });
  await load(); status(result.message);
}));
$('save-edit').onclick = () => run(async () => { await bridge.apiPost('action', { action: 'replace', ids: [editId], tags: $('edit-tags').value }); $('editor').close(); await load(); status('标签已保存'); });
$('describe').onclick = () => run(async () => {
  $('ai-result').textContent = '正在识别，最长约45秒…';
  try { const result = await bridge.apiPost('action', { action: 'describe', ids: [editId] }); suggestions = result.tags; $('ai-result').textContent = `${result.caption}\n图中文字：${result.ocr || '无'}\n建议标签：${suggestions.join('、')}`; $('accept-ai').hidden = false; }
  catch (error) { $('ai-result').textContent = error.message; }
});
$('accept-ai').onclick = () => { $('edit-tags').value = [...new Set([...$('edit-tags').value.split(/\s+/).filter(Boolean), ...suggestions])].join(' '); };
$('rename').onclick = () => $('rename-dialog').showModal();
$('confirm-rename').onclick = () => run(async () => { const result = await bridge.apiPost('action', { action: 'rename', old: $('old-tag').value.trim(), new: $('new-tag').value.trim() }); $('rename-dialog').close(); await load(); status(result.message); });
$('export').onclick = () => run(async () => { status('正在打包图片和标签…'); await bridge.download('export', {}, 'memeharbor-backup.zip'); status('备份已导出'); });
$('upload').onchange = () => run(async () => {
  const files = [...$('upload').files]; let count = 0; const failed = [];
  for (const file of files) { status(`正在导入 ${count + failed.length + 1}/${files.length}：${file.name}`); try { await bridge.upload('upload', file); count++; } catch (error) { failed.push(`${file.name}：${error.message}`); } }
  $('upload').value = ''; await load(); status(`成功导入 ${count} 个文件${failed.length ? '；失败：' + failed.join('；') : '，新图片使用“待整理”标签。'}`, failed.length > 0);
});
if (!bridge) { status('请从 AstrBot 插件详情 → Pages 打开本图库。', true); }
else { run(async () => { await bridge.ready(); await load(); }); }
