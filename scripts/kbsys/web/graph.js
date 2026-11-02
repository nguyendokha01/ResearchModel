"use strict";
const TOKEN = document.querySelector('meta[name="kb-token"]').content;
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let data = null;

async function get(path) {
  const res = await fetch(path, {headers:{"X-KB-Token":TOKEN}});
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}
async function post(path, body) {
  const res=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-KB-Token':TOKEN},body:JSON.stringify(body)});
  const data=await res.json(); if(!res.ok) throw new Error(data.error||`HTTP ${res.status}`); return data;
}
function initOptions(select, values, label) {
  const old = select.value;
  select.innerHTML = `<option value="">${esc(label)}</option>` + values.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join("");
  if (values.includes(old)) select.value = old;
}
async function load() {
  const p = new URLSearchParams();
  if ($('#vault').value) p.set('vault', $('#vault').value);
  if ($('#yearFrom').value) p.set('year_from', $('#yearFrom').value);
  if ($('#yearTo').value) p.set('year_to', $('#yearTo').value);
  if ($('#species').value) p.set('species', $('#species').value);
  if ($('#relation').value) p.set('relation_type', $('#relation').value);
  $('#status').textContent = 'Đang tải graph…';
  try {
    data = await get(`/api/graph?${p}`);
    initOptions($('#species'), data.species, 'Mọi loài');
    initOptions($('#relation'), data.relation_types, 'Mọi loại');
    $('#counts').textContent = `${data.vault}: ${data.counts.papers} bài · ${data.counts.concepts} khái niệm · ${data.counts.edges} links` +
      (data.truncated ? ' · Đã giới hạn để bảo vệ bộ nhớ; lọc hẹp hơn để xem thêm.' : '');
    $('#status').textContent = data.nodes.some(n=>n.kind==='candidate')
      ? 'Ứng viên OpenAlex chưa xác minh được hiển thị riêng' : 'Dữ liệu trong vault';
    draw();
  } catch(e) { $('#status').textContent = `Lỗi: ${e.message}`; }
}
function draw() {
  if (!data) return;
  const enabled = new Set($$('[data-layer]:checked').map(x=>x.dataset.layer));
  const nodes = data.nodes;
  const type=$('#nodeKind').value;
  const matching=new Set(nodes.filter(n=>n.kind==='concept'&&(!type||n.entity_kind===type)).map(n=>n.id));
  const linked=new Set(data.edges.filter(e=>matching.has(e.source)||matching.has(e.target)).flatMap(e=>[e.source,e.target]));
  const visible = nodes.filter(n => ['paper','concept','candidate'].includes(n.kind)&&(!type||matching.has(n.id)||n.kind==='paper'&&linked.has(n.id)));
  const papers = visible.filter(n=>n.kind==='paper'), concepts = visible.filter(n=>n.kind==='concept');
  const candidates = visible.filter(n=>n.kind==='candidate'&&enabled.has('discovered_citation'));
  const visibleNodes = visible.filter(n=>n.kind!=='candidate'||enabled.has('discovered_citation'));
  const gapX = 720, candidateX = 1450, rowH = 42, width = 2800;
  const height = Math.max(480, Math.max(papers.length,concepts.length,candidates.length)*rowH+100);
  const pos = new Map();
  papers.forEach((n,i)=>pos.set(n.id,{x:150,y:50+i*rowH}));
  concepts.forEach((n,i)=>pos.set(n.id,{x:gapX+150,y:50+i*rowH}));
  candidates.forEach((n,i)=>pos.set(n.id,{x:candidateX+150,y:50+i*rowH}));
  const edges = data.edges.filter(e=>enabled.has(e.kind));
  const svg = $('#graph'); svg.setAttribute('viewBox',`0 0 ${width} ${height}`); svg.setAttribute('height',height);
  let html = `<text x="30" y="25" fill="currentColor" font-weight="bold">Papers (${papers.length})</text><text x="${gapX+30}" y="25" fill="currentColor" font-weight="bold">Concepts (${concepts.length})</text><text x="${candidateX+30}" y="25" fill="currentColor" font-weight="bold">OpenAlex candidates (${candidates.length})</text>`;
  for (const e of edges) {
    const a=pos.get(e.source), b=pos.get(e.target); if(!a||!b) continue;
    const cls=e.kind==='paper_concept' ? (e.level==='mentioned'?'mentioned':'paper_concept') : e.kind==='relation' ? `relation rel-${e.label}` : e.kind;
    const dash=e.kind==='citation'?' marker-end="url(#arrow)"':'';
    html += `<path class="edge ${esc(cls)}" d="M${a.x+9},${a.y} C${a.x+150},${a.y} ${b.x-150},${b.y} ${b.x-9},${b.y}"${dash}><title>${e.label==='contradicts'?'🔴 mâu thuẫn: ':e.label==='direction_differs'?'🟠 ngược chiều (chưa rõ so sánh được): ':''}${esc(e.label||e.kind)}${e.measure?` · ${esc(e.measure)}`:''}${e.n_sentences?` · ${e.n_sentences} câu`:''}</title></path>`;
  }
  html += '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L8,3 z" fill="var(--citation)"/></marker></defs>';
  for (const n of visibleNodes) {
    const p=pos.get(n.id), kind=n.kind;
    const title=n.title?`${n.label} — ${n.title}`:`${n.label}${n.concept_type?` (${n.concept_type})`:''}`;
    const label=String(n.label||'').length>72?String(n.label).slice(0,69)+'…':n.label;
    const color=/^#[0-9a-f]{6}$/i.test(n.color||'')?n.color:'#64748B';
    html += `<g class="node ${kind}" data-id="${esc(n.id)}" transform="translate(${p.x},${p.y})"><circle r="7" style="fill:${color}"><title>${esc(title)}</title></circle><text x="13" y="4">${esc(label)}</text></g>`;
  }
  svg.innerHTML=html;
  $$('.node').forEach(el=>el.addEventListener('click',()=>showNode(el.dataset.id)));
}
function showNode(id) {
  const n=data.nodes.find(x=>x.id===id); if(!n) return;
  const connected=data.edges.filter(e=>e.source===id||e.target===id);
  const openalexUrl=/^https:\/\/openalex\.org\/W\d+$/.test(n.source_record_url||'')?n.source_record_url:'';
  let extra=n.kind==='paper'?`<br><a target="_blank" rel="noopener" href="/pdf/${encodeURIComponent(n.paper_id)}">Mở PDF</a>`:
    n.kind==='candidate'?`<br>Nguồn: ${esc(n.source||'unknown')} · ${esc(n.year||'n.d.')} · DOI: ${esc(n.doi||'không có')}`+
      (openalexUrl?` · <a target="_blank" rel="noopener" href="${esc(openalexUrl)}">OpenAlex record</a>`:''):
    `<br>Loại: ${esc(n.concept_type||'')} · MeSH: ${esc(n.mesh_ui||'—')}`;
  if(n.kind==='paper'&&n.doi) extra+=`<br><button id="discoverCiting">Tìm bài trích dẫn (gửi DOI tới OpenAlex)</button>`;
  if(n.kind==='candidate'&&!n.already_in_library) extra+=`<br><button id="stageCandidate">Đưa metadata vào inbox (không tải PDF)</button>`;
  $('#detail').hidden=false;
  const heading=n.kind==='candidate'?esc(n.title||n.label):`${esc(n.label)}${n.title?` — ${esc(n.title)}`:''}`;
  const contentLink=n.kind==='paper'?'/neural?'+new URLSearchParams({paper_id:n.paper_id,vault:document.getElementById("vault").value}):'/neural?'+new URLSearchParams({tab:'note',q:n.label,vault:document.getElementById("vault").value});
  $('#detail').innerHTML=`<strong>${heading}</strong>${extra}<br>Loại trình bày: ${esc(n.entity_kind||n.kind)} · Vai trò trong nguồn: ${esc((n.roles||[]).join(', ')||'chưa ghi')}<br>Liên kết hiện có: ${connected.length}<p><a href="${esc(contentLink)}">${n.kind==='paper'?'Xem kết quả, bảng và hình':'Mở ghi chú và bằng chứng'}</a></p>`;
  const conflicts=connected.filter(e=>['contradicts','direction_differs','effect_vs_no_effect'].includes(e.label));
  if(conflicts.length)$('#detail').insertAdjacentHTML('afterbegin',`<div class="conflict-callout"><strong>🔴 Findings cần đối chiếu nguồn</strong>${conflicts.map(e=>`<p>${esc(e.label)} · ${e.status==='human_confirmed'?'Người nghiên cứu đã xác nhận':'Đề xuất · chưa duyệt'}</p>${(e.source_sides||[]).map((side,i)=>`<details><summary>Nguồn ${i+1}</summary>${side.items.map(s=>`<blockquote>${esc(s.text)}<br><small>${esc(s.id)} · trang ${esc(s.pages?.[0])}</small></blockquote>`).join('')}</details>`).join('')}`).join('')}</div>`);
  const discoverButton=$('#discoverCiting');
  if(discoverButton) discoverButton.addEventListener('click',async()=>{
    if(!confirm(`Gửi DOI ${n.doi} tới OpenAlex để tìm bài trích dẫn? Chỉ DOI và định danh bài được gửi.`)) return;
    discoverButton.disabled=true; discoverButton.textContent='Đang truy vấn OpenAlex…';
    try { await post('/api/discover-citations',{paper_id:n.paper_id,vault:$('#vault').value,limit:100}); await load(); }
    catch(e){discoverButton.disabled=false;discoverButton.textContent=`Lỗi: ${e.message}`;}
  });
  const stageButton=$('#stageCandidate');
  if(stageButton) stageButton.addEventListener('click',async()=>{
    stageButton.disabled=true;
    try { const r=await post('/api/stage-discovery',{vault:$('#vault').value,candidate_id:n.candidate_id}); stageButton.textContent=`Đã lưu ${r.path}`; }
    catch(e){stageButton.disabled=false;stageButton.textContent=`Lỗi: ${e.message}`;}
  });
  $('#detail').scrollIntoView({block:'nearest'});
}
async function start() {
  try {
    const state=await get('/api/status');
    $('#vault').innerHTML=state.vaults.map(v=>`<option value="${esc(v.name)}">${esc(v.name)} (${v.papers} bài)</option>`).join('');
    $('#vault').value=state.default_vault||(state.vaults[0]||{}).name||'';
    await load();
  } catch(e) { $('#status').textContent=`Lỗi: ${e.message}`; }
}
$('#apply').addEventListener('click',load);
$('#nodeKind').addEventListener('change',draw);
$$('[data-layer]').forEach(x=>x.addEventListener('change',draw));
start();
