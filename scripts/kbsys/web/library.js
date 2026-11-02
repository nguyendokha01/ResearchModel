'use strict';
const TOKEN = document.querySelector('meta[name=kb-token]').content;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
let vault = '', polling = null, papers = [];

async function api(path, opts = {}) {
  const r = await fetch(path, {...opts, headers: {'X-KB-Token': TOKEN, ...(opts.headers || {})}});
  const data = await r.json().catch(() => ({error: 'phản hồi lạ'}));
  if (!r.ok) throw new Error(data.error || r.status);
  return data;
}
const post = (path, body) => api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});

const STATUS = {done: 'đơn vị bằng chứng đủ', review: 'cần xem lại', needs_fix: 'đang sửa', pending: 'chờ phân tích'};

async function load() {
  const d = await api('/api/library' + (vault ? '?vault=' + encodeURIComponent(vault) : ''));
  vault = d.vault || d.selected || (d.vaults[0] || '');
  $('#vaultSel').innerHTML = d.vaults.map(v => `<option ${v === vault ? 'selected' : ''}>${esc(v)}</option>`).join('')
    || '<option value="">(chưa có vault — tạo mới bên cạnh)</option>';
  $('#vaultName').textContent = vault;
  $('#inboxCount').textContent = d.inbox.length;
  const a = d.ai.analysis, s = d.ai.synthesis;
  $('#aiLine').textContent = `AI phân tích: ${a.ready ? a.message : 'chưa tự chạy — ' + a.message} · AI tổng hợp: ${s.ready ? s.message : 'chưa tự chạy'}`;
  $('#mcpHint').textContent = d.mcp_hint;
  papers = d.papers;
  const n = d.papers.length, done = d.papers.filter(p => p.analysis_status === 'done' && p.structured_profiles?.complete).length;
  $('#summary').textContent = `${n} bài · ${done} đủ bằng chứng và hồ sơ WH/protocol · ${d.review_pending} mục chờ bạn xem (Review Queue trong Obsidian)`;
  $('#paperRows').innerHTML = d.papers.map(p => `<tr><td>${esc(p.first_author || '')} — ${esc(p.title || p.paper_id)}${(p.other_vaults||[]).length ? '<br><small>Cũng có trong: '+p.other_vaults.map(esc).join(', ')+'</small>' : ''} <a href="/neural?paper_id=${encodeURIComponent(p.paper_id)}">Rà cấu trúc</a></td>
    <td>${esc(p.year || '')}</td><td class="st-${esc(p.analysis_status || 'pending')}">${esc(STATUS[p.analysis_status || 'pending'] || p.analysis_status)}${p.structured_profiles?.pending?.length?'<br><small>Chờ hồ sơ: '+p.structured_profiles.pending.map(esc).join(', ')+'</small>':''}</td>
    <td>${p.units}${p.flagged ? ` (${p.flagged} cờ)` : ''}${p.duplicate_claims ? ` · ${p.duplicate_claims} trùng cần xem` : ''}</td><td>${!p.quality_checked ? 'chưa kiểm sâu' : p.quality_blockers ? `Cần sửa chữ/OCR (${p.quality_blockers})` : esc(p.text_quality || '')}${p.ocr_pages ? ` · OCR ${p.ocr_pages} tr` : ''}${p.uncertain_tables ? ` · ${p.uncertain_tables} bảng cần đối chiếu` : ''}</td>
    <td><button class="rm" data-id="${esc(p.paper_id)}">Xóa bài</button></td></tr>`).join('')
    || '<tr><td colspan="6">Chưa có bài. Thêm nguồn rồi bấm ① Nhập bài.</td></tr>';
  showJob(d.job);
}

function showJob(j) {
  const log = $('#jobLog');
  if (!j || !j.action) { log.textContent = ''; return; }
  const state=j.result?.error?'lỗi':Array.isArray(j.result)&&j.result.some(r=>r.status==='waiting')?'còn chờ':Array.isArray(j.result)&&j.result.some(r=>r.status==='failed')?'lỗi':'kết thúc lượt';
  log.textContent = `[${j.action} · ${j.vault}] ${j.running ? 'đang chạy…' : state+' ' + (j.ended || '')}\n` + j.log.join('\n');
  log.scrollTop = log.scrollHeight;
  document.querySelectorAll('button.big').forEach(b => b.disabled = j.running);
  if (j.running && !polling) polling = setInterval(poll, 2000);
  if (!j.running && polling) { clearInterval(polling); polling = null; load(); }
}
async function removePaper(id) {
  const p = papers.find(x => x.paper_id === id), title = p?.title || id;
  if (!confirm(`Xóa bài “${title}” (${id}) khỏi thư viện?

Bài được chuyển vào knowledge_store/removed/ (có thể khôi phục, chưa xóa hẳn): hồ sơ bài, ghi chú Paper (kể cả phần bạn tự viết) và PDF do hệ thống quản lý. Bài sẽ biến mất khỏi tìm kiếm, vault và Concept notes. Ghi chú trong Mine/ không bị đụng tới.`)) return;
  const msg = $('#removeMsg');
  msg.textContent = `Đang gỡ “${title}”… (có thể mất vài chục giây)`;
  try {
    const r = await post('/api/paper/delete', {paper_id: id});
    msg.textContent = `Đã gỡ “${r.title}”: ${r.files_moved} mục chuyển vào ${r.moved_to} (có manifest.json ghi vị trí cũ).`;
    load();
  } catch (e) { msg.textContent = 'Không gỡ được: ' + e.message; }
}
$('#paperRows').onclick = e => { const b = e.target.closest('button.rm'); if (b) removePaper(b.dataset.id); };

async function poll() { showJob(await api('/api/job')); }

async function run(action) {
  if (!vault) return alert('Hãy tạo hoặc chọn vault trước.');
  try { showJob(await post('/api/job/start', {action, vault})); } catch (e) { alert(e.message); }
}

async function upload(files) {
  if (!vault) return alert('Hãy tạo hoặc chọn vault trước.');
  for (const f of files) {
    $('#uploadLog').insertAdjacentHTML('beforeend', `<div>Đang chép ${esc(f.name)}…</div>`);
    try {
      const r = await api(`/api/upload?vault=${encodeURIComponent(vault)}&name=${encodeURIComponent(f.name)}`,
        {method: 'POST', body: await f.arrayBuffer()});
      $('#uploadLog').lastElementChild.textContent = `✓ ${f.name} → ${r.saved}`;
    } catch (e) { $('#uploadLog').lastElementChild.textContent = `✗ ${f.name}: ${e.message}`; }
  }
  load();
}

$('#vaultSel').onchange = async e => { vault = e.target.value; await post('/api/vault/select', {name: vault}); load(); };
$('#createVault').onclick = async () => {
  try { const r = await post('/api/vault/create', {name: $('#newName').value, topic: $('#newTopic').value});
        vault = r.vault; $('#newName').value = $('#newTopic').value = ''; load(); }
  catch (e) { alert(e.message); }
};
$('#files').onchange = e => upload(e.target.files);
const drop = $('#drop');
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); upload(e.dataTransfer.files); };
$('#runIngest').onclick = () => run('ingest');
$('#runAnalyze').onclick = () => run('analyze');
$('#runConnect').onclick = () => run('connect');
load().catch(e => { $('#summary').textContent = 'Lỗi: ' + e.message; });
