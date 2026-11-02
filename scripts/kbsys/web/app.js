"use strict";
// Local search page (M3.2/M3.3). Talks only to the local server (127.0.0.1).

const TOKEN = document.querySelector('meta[name="kb-token"]').content;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const SECTION = {
  front_matter: "Front matter", abstract: "Abstract", introduction: "Introduction", main_text: "Main text",
  methods: "Methods", results: "Results", results_discussion: "Results and Discussion", discussion: "Discussion",
  conclusion: "Conclusion", references: "References", supplementary: "Supplementary", figure_legends: "Figure legends",
  acknowledgements: "Acknowledgements", funding: "Funding", author_contributions: "Author contributions",
  conflicts: "Conflicts of interest", data_availability: "Data availability",
  notes: "Publisher notes",
};
const TYPE = {
  result: "Kết quả (bài này)", cited_statement: "Bài này trích lại", interpretation: "Tác giả diễn giải",
  hypothesis: "Giả thuyết", method_detail: "Phương pháp", conclusion: "Kết luận tác giả", limitation: "Hạn chế",
  background: "Kiến thức nền", sentence: "Câu gốc chưa phân tích",
};
const VI = /[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]/i;

const state = {
  q: "", view: "evidence4", speed: "fast", vault: "", askAi: true, ai: null,
  queries: [], offset: 0, limit: 20, search: null, answer: null, answerFor: "", answerSeq: 0, searchSeq: 0,
};

// ---------------------------------------------------------------- helpers
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function sec(name) { return SECTION[name] || name || ""; }
function short(id) { return String(id || "").split(":").pop(); }
function supify(html) { // "SSEA-3^+" -> SSEA-3<sup>+</sup> (display only; stored text keeps ^)
  return html.replace(/\^([0-9A-Za-z+\-−–,.]+?)(?=[\s,;.)\]]|$)/g, "<sup>$1</sup>");
}
function highlight(text, terms) {
  let html = esc(text);
  const parts = (terms || []).filter(Boolean).map(t =>
    t.split(/\s+/).map(w => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\w*\\W+"));
  if (parts.length) {
    const re = new RegExp(`(?<![&#\\w])(${parts.join("|")})\\w*`, "gi");
    html = html.replace(re, m => `<mark>${m}</mark>`);
  }
  return supify(html);
}
async function api(path, body) {
  const opt = { headers: { "X-KB-Token": TOKEN } };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({ error: `lỗi ${r.status}` }));
  if (!r.ok) throw new Error(data.error || `lỗi ${r.status}`);
  return data;
}
function saveUi() {
  try { localStorage.setItem("kb-ui", JSON.stringify({ view: state.view, speed: state.speed, askAi: state.askAi, vault: state.vault })); } catch (e) { /* private mode */ }
}
function loadUi() {
  try { return JSON.parse(localStorage.getItem("kb-ui") || "{}"); } catch (e) { return {}; }
}
function setUrl() {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  if (state.view !== "google") p.set("view", state.view);
  if (state.speed !== "fast") p.set("speed", state.speed);
  history.replaceState(null, "", p.toString() ? `?${p}` : "/");
}
function pdfLink(pid, page, label) {
  return `<a href="/pdf/${encodeURIComponent(pid)}#page=${page}" target="_blank" rel="noopener">${label || `Mở PDF trang ${page}`}</a>`;
}

// ---------------------------------------------------------------- filters
function filters() {
  const f = {
    evidence: $('input[name="evidence"]:checked').value,
    unit_types: $$('#fType input:checked').map(x => x.value),
    sections: $$('#fSection input:checked').map(x => x.value),
    species: ($('#fSpecies input:checked') || {}).value || "",
    year_from: $("#yearFrom").value, year_to: $("#yearTo").value,
  };
  return f;
}
function filterQuery(f) {
  const p = new URLSearchParams();
  if (f.evidence) p.set("evidence", f.evidence);
  if (f.unit_types.length) p.set("types", f.unit_types.join(","));
  if (f.sections.length) p.set("sections", f.sections.join(","));
  if (f.species) p.set("species", f.species);
  if (f.year_from) p.set("year_from", f.year_from);
  if (f.year_to) p.set("year_to", f.year_to);
  return p;
}
function renderFacets(facets) {
  const keep = filters();
  const box = (id, counts, name, labels, single) => {
    const el = $(id);
    const title = el.querySelector("h4").outerHTML;
    const chosen = new Set(id === "#fType" ? keep.unit_types : id === "#fSection" ? keep.sections : [keep.species]);
    const entries = Object.entries(counts || {}).sort((a, b) => b[1] - a[1]);
    chosen.forEach(v => { if (v && !entries.find(e => e[0] === v)) entries.push([v, 0]); });
    el.innerHTML = title + (entries.length ? entries.map(([k, n]) =>
      `<label><input type="${single ? "radio" : "checkbox"}" name="${name}" value="${esc(k)}" ${chosen.has(k) ? "checked" : ""}>
       <span>${esc(labels ? (labels[k] || k) : k)}</span><span class="c">${n}</span></label>`).join("")
      : `<span class="c">—</span>`);
    if (single && entries.length) el.insertAdjacentHTML("beforeend",
      `<label><input type="radio" name="${name}" value="" ${keep.species ? "" : "checked"}> <span>Mọi loài</span></label>`);
  };
  box("#fType", facets.type, "ftype", TYPE);
  box("#fSection", facets.section, "fsection", SECTION);
  box("#fSpecies", facets.species, "fspecies", null, true);
}

// ---------------------------------------------------------------- search
async function runSearch(opts = {}) {
  const seq = ++state.searchSeq;
  if (!opts.append) state.offset = 0;
  const f = filters();
  const p = filterQuery(f);
  p.set("q", state.q);
  p.set("speed",state.speed);
  p.set("limit", state.limit);
  p.set("offset", state.offset);
  if (state.vault) p.set("vault", state.vault);
  state.queries.forEach(x => p.append("also", x));
  let res;
  try {
    res = await api(`/api/search?${p}`);
  } catch (e) {
    $("#status").innerHTML = `<span class="err">Lỗi tìm kiếm: ${esc(e.message)}</span>`;
    return;
  }
  if (seq !== state.searchSeq) return;
  if (opts.append && state.search) {
    res.hits = state.search.hits.concat(res.hits);
    res.papers = { ...state.search.papers, ...res.papers };
  }
  state.search = res;
  renderStatus();
  renderFacets(res.facets);
  $("#filters").hidden = false;
  $(".layout").classList.remove("nofilters");
  renderResults();
}

function renderStatus() {
  const r = state.search;
  if (!r) return;
  const bits = [];
  bits.push(`<b>${r.total}</b> mục khớp · đã quét ${r.papers_scanned} bài trong “${esc(r.scope)}”`);
  if (r.mode === "or") bits.push(`<span class="note">ít mục có đủ mọi từ nên hiện cả mục khớp một phần (khớp nhiều từ xếp trước)</span>`);
  if (state.queries.length) bits.push(`đã tìm bằng: ${state.queries.map(q => `“${esc(q)}”`).join(", ")}`);
  const seen = new Set();
  const mesh = (r.expansions || []).filter(e => !seen.has(e.phrase.toLowerCase()) && seen.add(e.phrase.toLowerCase()));
  if (mesh.length) bits.push(`<span title="Từ đồng nghĩa lấy từ từ điển MeSH (NLM)">MeSH: ${mesh.map(e =>
    `“${esc(e.phrase)}” + ${e.added.slice(0, 4).map(esc).join(", ")}${e.added.length > 4 ? "…" : ""}`).join("; ")}</span>`);
  if (r.semantic_error) bits.push(`<span class="note">${esc(r.semantic_error)}</span>`);
  if (r.semantic) bits.push(`<span title="Ngoài khớp từ khóa, máy còn tìm các đoạn gần nghĩa (mô hình đa ngôn ngữ chạy trên máy)">có tìm theo nghĩa</span>`);
  if (r.needs_translation && !state.queries.length)
    bits.push(`<span class="note">câu hỏi tiếng Việt: cần AI để đổi sang từ khóa tiếng Anh (hoặc gõ từ khóa tiếng Anh)</span>`);
  if (r.total && r.hits.every(h => !(h.via || []).includes("từ khóa")))
    bits.push(`<span class="note">không mục nào chứa đúng từ bạn gõ; các mục dưới chỉ gần nghĩa — có thể kho chưa có câu trả lời</span>`);
  if (!r.total) bits.push(`<span class="note">Không tìm thấy trong kho hiện tại (không có nghĩa là không có trong y văn).</span>`);
  $("#status").innerHTML = bits.join(" · ");
}

function hitCard(h, n, terms, papers) {
  const card = papers[h.paper_id] || {};
  const type = h.unit_type || "sentence";
  const badges = [`<span class="badge b-${type}">${esc(h.label)}</span>`];
  if (h.review_paper) badges.push(`<span class="badge b-review">Bài tổng quan</span>`);
  if (h.analysis_origin === "heuristic_draft") badges.push(`<span class="badge heuristic-badge" title="Loại đơn vị được gán bởi script quy tắc; hãy dựa vào câu trích nguyên văn">nháp tự động</span>`);
  if (h.via && !h.via.includes("từ khóa")) badges.push(`<span class="badge b-meaning" title="Không chứa đúng từ bạn gõ; máy thấy gần nghĩa với câu hỏi. Hãy đọc kỹ nguyên văn.">khớp theo nghĩa</span>`);
  if (h.hedging === "hedged") badges.push(`<span class="badge b-sentence" title="Nguồn nói ở mức có thể/gợi ý">mức “có thể”</span>`);
  if ((h.uncertainty || []).length) badges.push(`<span class="badge b-sentence" title="Ngôn ngữ dè dặt của tác giả">${[...new Set(h.uncertainty.map(m => m.text))].map(esc).join(' · ')}</span>`);
  const pages = h.pages || [];
  const where = `${(h.sections || []).map(sec).join(", ")} · trang ${pages.join(", ")} · <code>${esc(short(h.id))}</code>`;
  const stmt = h.statement ? `<details class="statement"><summary>Diễn giải lưu trong kho</summary>${highlight(h.statement, terms)}</details>` : "";
  const nearby=(h.context||[]).length?`<details class="deep-context"><summary>Đọc sâu · câu trước và sau</summary>${h.context.map(s=>`<p><small>${esc(short(s.sentence_id))} · p.${s.page}</small> ${highlight(s.text,terms)}</p>`).join('')}</details>`:'';
  const quote = h.sentence_kind === "table_row"
    ? `<blockquote class="row-table">${highlight(h.text, terms)}</blockquote>`
    : `<blockquote>${highlight(h.text, terms)}</blockquote>`;
  const refs = (h.cited_refs || []).length ? `<div class="refs">${h.cited_refs.map(r =>
    `<button type="button" class="ref" data-ref="${esc(r.ref_id)}" title="${esc(r.title || "")}">[${r.num}] ${esc(r.first_author || "?")} ${r.year || ""}${r.resolved_paper_id ? " · có trong kho" : ""}</button>`).join("")}</div>` : "";
  const acts = [];
  if (card.has_pdf && pages.length) acts.push(pdfLink(h.paper_id, pages[0]));
  acts.push(`<button type="button" data-read="${esc(h.paper_id)}" data-sid="${esc(h.sentence_ids[0] || "")}">Đọc trong bài</button>`);
  acts.push(`<button type="button" data-ctx="${esc(h.sentence_ids[0] || "")}">Câu trước/sau</button>`);
  return `<article class="hit" id="hit-${n}" data-id="${esc(h.id)}">
    <div class="top-line"><span class="num">${n}</span>${badges.join("")}</div>
    <div class="paper"><a href="#" data-read="${esc(h.paper_id)}" data-sid="${esc(h.sentence_ids[0] || "")}">${esc(card.cite || h.paper_id)}</a>
      <span class="t">— ${esc(card.title || "")}</span></div>
    <div class="where">${where}</div>${stmt}${quote}${refs}
    ${nearby}<div class="acts">${acts.join("")}</div><div class="ctxbox"></div></article>`;
}

function renderResults() {
  const r = state.search;
  if (!r) return;
  $("#welcome").hidden = true;
  $("#results").innerHTML = r.hits.map((h, i) => hitCard(h, i + 1, r.highlight, r.papers)).join("");
  $("#more").hidden = r.hits.length >= r.total;
}

// ---------------------------------------------------------------- AI answer
function aiReady() { return state.ai && state.ai.ready && state.askAi; }

async function runAnswer() {
  const view = state.view;
  if (view !== "table" && !aiReady()) {
    ++state.answerSeq;
    if (view === "report5" || view === "evidence4") return fetchAnswer(false); // code-owned evidence works without AI
    $("#aiPanel").hidden = !state.askAi;
    if(state.askAi) $("#aiPanel").innerHTML=`<p class="status">${esc(state.ai?.message||'Đang kiểm tra kết nối AI…')}</p>`;
    return;
  }
  return fetchAnswer(view !== "table");
}

async function fetchAnswer(useAi) {
  const seq = ++state.answerSeq;
  const panel = $("#aiPanel");
  panel.hidden = false;
  const n = state.speed === "deep" ? "nhiều" : "các";
  panel.innerHTML = `<div class="loading"><div class="spin"></div>${useAi
    ? `AI đang đọc ${n} mục bằng chứng tìm được… (${state.speed === "deep" ? "chế độ Sâu: 1–3 phút" : "10–30 giây"})`
    : "Đang dựng từ các mục bằng chứng…"}</div>`;
  try {
    const res = await api("/api/answer", {
      question: state.q, view: state.view, speed: state.speed, vault: state.vault,
      filters: filters(), queries: state.queries, use_ai: useAi,
    });
    if (seq !== state.answerSeq) return;
    state.answer = res;
    renderAnswer();
  } catch (e) {
    if (seq !== state.answerSeq) return;
    panel.innerHTML = `<p class="status"><span class="err">Lỗi: ${esc(e.message)}</span></p>`;
  }
}

function sentenceHtml(s) {
  const cites = (s.evidence || []).map(n => `<button type="button" class="cite" data-item="${n}">[${n}]</button>`).join(" ");
  const text = esc(s.text).replace(/\s*(\[#\d+\])+\s*([.。]?)\s*$/, "$2").replace(/\[#\d+\]/g, "");
  const bad = s.check !== "ok";
  const why = bad ? `<span class="why" title="${esc((s.reasons || []).join("; "))}">bị gạch: ${esc((s.reasons || [])[0] || s.check)}</span>` : "";
  const warn = (s.warnings || []).length && !bad ? `<span class="warn">${esc(s.reasons.slice(-1)[0] || "")}</span>` : "";
  return `<p class="s ${bad ? "struck" : ""}"><span class="t">${supify(text)}</span> ${cites}${why}${warn}</p>`;
}

function stripTrailingCites(text) {
  return String(text || "").replace(/(?:\s*\[#\d+\])+\s*([.。]?)\s*$/, "$1");
}

function renderAnswer() {
  const a = state.answer;
  const panel = $("#aiPanel");
  if (!a) return;
  const errs = (a.errors || []).map(e => `<p class="status"><span class="err">${esc(e)}</span></p>`).join("");
  const ans = a.answer || {};
  let body = "";
  if (a.view === "evidence4") {
    const evidence = ans.evidence || a.evidence || [];
    const evidenceHtml = evidence.length ? evidence.map(row => {
      const item = (a.items || [])[row.item - 1] || {};
      const page = (item.pages || [])[0];
      const pdf = page ? pdfLink(item.paper_id, page, "Đọc nguồn") : "";
      return `<blockquote class="source-quote"><button type="button" class="cite" data-item="${row.item}">[${row.item}]</button> <span class="quote-text">"${esc(row.quote || row.text || "")}"</span> <span class="citation">— ${esc(row.citation || "[không rõ, không rõ, trang không rõ] (không rõ)")}</span> ${pdf}</blockquote>`;
    }).join("") : `<p class="scope">Chưa có trích dẫn được chọn từ kho.</p>`;
    body += `<h2>Trả lời bốn phần từ kho</h2><section class="answer-part evidence-part"><h3>Bằng chứng từ tài liệu</h3>${evidenceHtml}</section>`;
    body += `<section class="answer-part"><h3>Trả lời dựa trên bằng chứng</h3>${(ans.answer || ans.sentences || []).map(sentenceHtml).join("") || '<p class="scope">Cần AI để viết phần này.</p>'}</section>`;
    body += `<section class="answer-part reasoning-block"><h3>[REASONING] Phần suy luận của AI</h3><p class="reasoning-label">Suy luận của AI — không phải nội dung tài liệu.</p>${(ans.reasoning || []).map(row => {
      const refs = row.basis_kind === "ai_knowledge" ? `<em class="basis-label">${esc(row.basis_label)}</em>` : (row.basis_kind && row.basis_kind !== "none" ? "suy luận từ " : "") + (row.basis || []).map(ref => Number.isInteger(ref) ? `<button type="button" class="cite" data-item="${ref}">[${ref}]</button>` : `<code>${esc(ref)}</code>`).join(" ");
      const bad = row.check !== "ok";
      return `<p class="s ${bad ? "struck" : ""}"><span class="t">${supify(esc(stripTrailingCites(row.text || "")))}</span> ${refs}${bad ? `<span class="why">bị gạch: ${esc((row.reasons || [])[0] || row.check)}</span>` : ""}</p>`;
    }).join("") || '<p class="scope">Chưa có suy luận của AI.</p>'}</section>`;
    body += `<section class="answer-part missing-part"><h3>Không tìm thấy / không chắc chắn</h3>${(ans.missing || []).map(m => `<p class="missing">${esc(m)}</p>`).join("") || '<p class="scope">—</p>'}</section>`;
  } else if (a.view === "google") {
    body += `<h2>AI tóm tắt từ kho của bạn</h2>`;
    if (ans.not_found) body += `<p class="missing">Không tìm thấy bằng chứng trong kho hiện tại cho câu hỏi này.</p>`;
    body += (ans.sentences || []).map(sentenceHtml).join("");
    if ((ans.missing || []).length) body += `<p class="missing"><b>Chưa thấy trong kho:</b> ${ans.missing.map(esc).join("; ")}</p>`;
  } else if (a.view === "report5") {
    body += `<h2>Báo cáo 5 phần</h2>`;
    body += `<h3><span class="n">1</span>Kho có gì</h3><ul>${(a.overview || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>`;
    const part = (k, n, title) => `<h3><span class="n">${n}</span>${title}</h3>` +
      ((ans[k] || []).length ? ans[k].map(sentenceHtml).join("") : `<p class="scope">${a.provider.ready ? "—" : "Cần AI để viết phần này."}</p>`);
    body += part("evidence", 2, "Bằng chứng") + part("can_conclude", 3, "Kết luận được") + part("cannot_conclude", 4, "Không kết luận được");
    body += `<h3><span class="n">5</span>Còn thiếu</h3>` + ((ans.missing || []).length
      ? `<ul>${ans.missing.map(m => `<li class="missing">${esc(m)}</li>`).join("")}</ul>` : `<p class="scope">—</p>`);
  } else {
    body += `<h2>Bảng so sánh (dựng bằng code từ các đơn vị bằng chứng, không dùng AI)</h2>`;
    const t = a.table || { columns: [], rows: [] };
    body += t.rows.length ? `<div class="tbl-wrap"><table class="cmp"><thead><tr><th>#</th>${t.columns.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${t.rows.map(r => `<tr title="${esc(r.statement || "")}">
        <td><button type="button" class="cite" data-item="${r.item}">${r.item}</button></td>${r.cells.map(c => `<td>${supify(esc(c))}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
      : `<p class="missing">Chưa có đơn vị bằng chứng có cấu trúc (kết quả/phương pháp) trong các mục tìm được. Bài chưa được AI phân tích thì chỉ có câu gốc.</p>`;
  }
  const s = a.summary_checks || {};
  const foot = [];
  if (s.sentences) foot.push(`Đã kiểm ${s.sentences} câu: ${s.sentences - s.struck} đạt, ${s.struck} bị gạch${s.warnings ? `, ${s.warnings} cảnh báo` : ""}`);
  if (a.provider && a.provider.ready && a.view !== "table") foot.push(esc(a.provider.message));
  foot.push(`${(a.items || []).length} mục bằng chứng · ${a.elapsed_seconds}s`);
  if (a.english_question) foot.push(`câu hỏi tiếng Anh: “${esc(a.english_question)}”`);
  if (a.saved_as) foot.push(`lưu vết: <code>${esc(a.saved_as)}</code>`);
  const sources = (a.items || []).length ? `<details><summary>Các mục bằng chứng đã đưa cho AI (${a.items.length})</summary>
    <div class="srcs">${a.items.map((h, i) => hitCard(h, i + 1, [], a.papers || {})).join("")}</div></details>` : "";
  panel.innerHTML = errs + body + (a.scope_note && !(ans.missing || []).includes(a.scope_note) ? `<p class="scope">${esc(a.scope_note)}</p>` : "") + sources + `<div class="foot">${foot.map(x => `<span>${x}</span>`).join("")}</div>`;
}

function itemPopover(n, anchor) {
  const a = state.answer;
  const h = a && a.items[n - 1];
  if (!h) return;
  const card = (a.papers || {})[h.paper_id] || {};
  const pages = h.pages || [];
  showPop(anchor, `<b>[${n}] ${esc(card.cite || h.paper_id)}</b> · <span class="badge b-${h.unit_type || "sentence"}">${esc(h.label)}</span>
    <div class="where">${(h.sections || []).map(sec).join(", ")} · trang ${pages.join(", ")} · <code>${esc(short(h.id))}</code></div>
    ${h.statement ? `<details><summary>Diễn giải lưu trong kho</summary>${esc(h.statement)}</details>` : ""}
    <blockquote>${supify(esc(h.text))}</blockquote>
    <div class="acts">${card.has_pdf && pages.length ? pdfLink(h.paper_id, pages[0]) : ""}
      <button type="button" data-read="${esc(h.paper_id)}" data-sid="${esc(h.sentence_ids[0] || "")}">Đọc trong bài</button></div>`);
}

// ---------------------------------------------------------------- popover
function showPop(anchor, html) {
  const pop = $("#pop");
  pop.innerHTML = `<button type="button" class="x" aria-label="Đóng">×</button>${html}`;
  pop.hidden = false;
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  let left = Math.min(Math.max(12, r.left), window.innerWidth - w - 12);
  let top = r.bottom + 6;
  if (top + h > window.innerHeight - 12) top = Math.max(12, r.top - h - 6);
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
}
function hidePop() { $("#pop").hidden = true; }

async function refPopover(refId, anchor) {
  showPop(anchor, `<div class="loading"><div class="spin"></div>Đang mở mục tham khảo…</div>`);
  try {
    const r = await api(`/api/ref?id=${encodeURIComponent(refId)}`);
    const links = [];
    if (r.doi) links.push(`<a href="https://doi.org/${esc(r.doi)}" target="_blank" rel="noopener">DOI</a>`);
    if (r.pmid) links.push(`<a href="https://pubmed.ncbi.nlm.nih.gov/${esc(r.pmid)}/" target="_blank" rel="noopener">PubMed</a>`);
    if (r.resolved) links.push(`<button type="button" data-read="${esc(r.resolved.paper_id)}" data-sid="">Mở bài này trong kho (${esc(r.resolved.cite)})</button>`);
    showPop(anchor, `<b>Tài liệu tham khảo [${r.num}]</b> <span class="where">của ${esc(r.paper_id.split("_")[0])}</span>
      <p>${esc(r.raw)}</p>
      <div class="where">${r.resolved ? "Có trong kho." : "Chưa có trong kho."} Độ tin cậy khi tách: ${esc(r.parse_confidence || "")}</div>
      <div class="acts">${links.join("")}</div>`);
  } catch (e) {
    showPop(anchor, `<span class="err">${esc(e.message)}</span>`);
  }
}

// ---------------------------------------------------------------- context
async function toggleContext(btn) {
  const box = btn.closest(".hit").querySelector(".ctxbox");
  if (box.innerHTML) { box.innerHTML = ""; return; }
  const sid = btn.dataset.ctx;
  try {
    const rows = await api(`/api/context?id=${encodeURIComponent(sid)}&w=3`);
    box.innerHTML = `<div class="ctx">${rows.map(r => `<p class="${r.sentence_id === sid ? "me" : ""}">
      <code>${esc(short(r.sentence_id))}</code> ${supify(esc(r.text))}</p>`).join("")}</div>`;
  } catch (e) {
    box.innerHTML = `<div class="ctx err">${esc(e.message)}</div>`;
  }
}

// ---------------------------------------------------------------- reader
const reader = { data: null, sid: "", tab: "text" };

async function openReader(paperId, sid) {
  hidePop();
  $("#reader").hidden = false;
  $("#readerBody").innerHTML = `<div class="loading"><div class="spin"></div>Đang mở bài…</div>`;
  try {
    if (!reader.data || reader.data.paper.paper_id !== paperId) reader.data = await api(`/api/paper?id=${encodeURIComponent(paperId)}`);
  } catch (e) {
    $("#readerBody").innerHTML = `<p class="err">${esc(e.message)}</p>`;
    return;
  }
  reader.sid = sid || "";
  const p = reader.data.paper;
  const links = [];
  if (p.has_pdf) links.push(pdfLink(p.paper_id, 1, "Mở PDF"));
  if (p.doi) links.push(`<a href="https://doi.org/${esc(p.doi)}" target="_blank" rel="noopener">DOI</a>`);
  if (p.pmid) links.push(`<a href="https://pubmed.ncbi.nlm.nih.gov/${esc(p.pmid)}/" target="_blank" rel="noopener">PubMed</a>`);
  $("#readerHead").innerHTML = `<h2>${esc(p.title || p.paper_id)}</h2>
    <div class="meta">${esc((p.authors || []).slice(0, 6).join(", "))}${(p.authors || []).length > 6 ? " et al." : ""} ·
      ${esc(p.journal || "")} ${p.year || ""} · ${esc(p.evidence_type || "")} · ${links.join(" · ")}</div>`;
  setReaderTab(sid ? "text" : reader.tab);
}

function setReaderTab(tab) {
  reader.tab = tab;
  $$("#readerTabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  const d = reader.data;
  const body = $("#readerBody");
  if (tab === "text") body.innerHTML = readerText(d);
  else if (tab === "units") body.innerHTML = readerUnits(d);
  else if (tab === "objects") body.innerHTML = readerObjects(d);
  else body.innerHTML = readerRefs(d);
  if (tab === "text" && reader.sid) {
    const el = document.getElementById(`s-${short(reader.sid)}`);
    if (el) el.scrollIntoView({ block: "center" });
  } else body.scrollTop = 0;
}

function sentenceWithCitations(s) {
  let out = "", pos = 0;
  for (const c of [...s.citations].sort((a, b) => a.start - b.start)) {
    if (c.start < pos) continue;
    out += supify(esc(s.text.slice(pos, c.start)));
    const marker = s.text.slice(c.start, c.end);
    const shown = marker.startsWith("^") ? `<sup>${esc(marker.slice(1))}</sup>` : esc(marker);
    out += `<button type="button" class="cm" data-refnums="${esc(c.ref_nums.join(","))}" title="Tài liệu tham khảo ${esc(c.ref_nums.join(", "))}">${shown}</button>`;
    pos = c.end;
  }
  return out + supify(esc(s.text.slice(pos)));
}

function readerText(d) {
  let html = "", lastSec = null, lastSub = null, para = [];
  const flush = () => { if (para.length) html += `<p class="para">${para.join(" ")}</p>`; para = []; };
  let lastPassage = null;
  for (const s of d.sentences) {
    if (s.section === "references") continue;
    if (s.section !== lastSec) { flush(); html += `<h3>${esc(sec(s.section))}</h3>`; lastSec = s.section; lastSub = null; }
    if (s.subsection && s.subsection !== lastSub) { flush(); html += `<h4>${esc(s.subsection)}</h4>`; lastSub = s.subsection; }
    if (s.kind !== "text") {
      flush();
      if (s.kind === "table_row") continue; // shown under "Bảng & hình"
      html += `<p class="para caption"><span id="s-${short(s.id)}" class="sent ${s.id === reader.sid ? "here" : ""}">${sentenceWithCitations(s)}</span></p>`;
      continue;
    }
    const pid = s.passage_id || s.page;
    if (pid !== lastPassage && para.length > 3) flush();
    lastPassage = pid;
    const cls = ["sent", s.units.length ? "unit" : "", s.id === reader.sid ? "here" : ""].join(" ");
    para.push(`<span id="s-${short(s.id)}" class="${cls}" ${s.units.length ? `data-units="${esc(s.units.join(","))}" title="Thuộc đơn vị bằng chứng ${s.units.map(short).join(", ")}"` : ""}>${sentenceWithCitations(s)}<span class="sid">${short(s.id)}·tr${s.page}</span></span>`);
  }
  flush();
  return html || `<p class="scope">Bài này chưa có câu tách sẵn.</p>`;
}

function readerUnits(d) {
  if (!d.units.length) return `<p class="scope">Bài này chưa được AI phân tích (chưa có đơn vị bằng chứng).</p>`;
  return d.units.map(u => `<div class="unit-card" id="u-${short(u.unit_id)}">
    <div class="top-line"><span class="num">${short(u.unit_id)}</span><span class="badge b-${u.unit_type}">${esc(u.label)}</span>
      <span class="where">${(u.sections || []).map(sec).join(", ")} · trang ${(u.pages || []).join(", ")}</span></div>
    <blockquote>${supify(esc(u.quote))}</blockquote><details><summary>Diễn giải và kiểm tra nguồn</summary><p>${esc(u.statement)}</p><small>${esc((u.flags||[]).map(f=>f.message||f.code||f).join('; '))}</small></details>
    <div class="acts"><button type="button" data-goto="${esc((u.sentence_ids || [])[0] || "")}">Xem trong toàn văn</button></div></div>`).join("");
}

function readerObjects(d) {
  const pid = d.paper.paper_id;
  const img = o => o.crop ? `<img loading="lazy" src="/crop/${encodeURIComponent(pid)}/${esc(o.crop)}" alt="${esc(o.label)}">` : "";
  const caption = ids => (ids || []).map(id => (d.sentences.find(s => s.id === id) || {}).text || "").join(" ");
  let html = "";
  for (const t of d.tables) {
    const rows = t.rows || [];
    html += `<div class="obj"><b>${esc(t.label)}</b> <span class="where">trang ${t.page}${t.source && t.source !== "main" ? " · " + esc(t.source) : ""} · ${pdfLink(pid, t.page, "PDF")}</span>
      <p class="caption">${supify(esc(caption(t.caption_sentence_ids)))}</p>${img(t)}
      ${rows.length ? `<div class="tbl-wrap"><table>${(t.header || []).length ? `<tr>${t.header.map(c => `<th>${supify(esc(c))}</th>`).join("")}</tr>` : ""}
        ${rows.map(r => `<tr>${r.map(c => `<td>${supify(esc(c))}</td>`).join("")}</tr>`).join("")}</table></div>`
        : `<p class="scope">Chưa tách được hàng của bảng này; xem ảnh cắt hoặc PDF.</p>`}
      ${t.footnotes ? `<p class="caption">${supify(esc([].concat(t.footnotes).join(" ")))}</p>` : ""}
      ${t.quality === "uncertain" ? `<p class="scope">Máy tách bảng này chưa chắc chắn: đối chiếu với ảnh cắt hoặc PDF.</p>` : ""}</div>`;
  }
  for (const f of d.figures) {
    html += `<div class="obj"><b>${esc(f.label)}</b> <span class="where">trang ${f.page} · ${pdfLink(pid, f.page, "PDF")}</span>
      ${img(f)}<p class="caption">${supify(esc(caption(f.caption_sentence_ids)))}</p>
      <p class="scope">Số liệu chỉ có trong hình thì không được đọc thành số.</p></div>`;
  }
  return html || `<p class="scope">Không có bảng hay hình.</p>`;
}

function readerRefs(d, highlightNums) {
  if (!d.references.length) return `<p class="scope">Không tách được danh mục tham khảo.</p>`;
  const hl = new Set(highlightNums || []);
  return `<div class="reflist">${d.references.map(r => `<p id="r-${r.num}" class="${hl.has(r.num) ? "here" : ""}"><b>[${r.num}]</b> ${esc(r.raw)}
    ${r.doi ? ` <a href="https://doi.org/${esc(r.doi)}" target="_blank" rel="noopener">DOI</a>` : ""}
    ${r.resolved_paper_id ? ` <button type="button" class="ref" data-read="${esc(r.resolved_paper_id)}" data-sid="">có trong kho</button>` : ""}</p>`).join("")}</div>`;
}

function readerCitation(btn) {
  const nums = btn.dataset.refnums.split(",").filter(Boolean).map(Number);
  const refs = reader.data.references.filter(r => nums.includes(r.num));
  if (!refs.length) return showPop(btn, `<p>Không tra được số ${esc(nums.join(", "))} trong danh mục tham khảo.</p>`);
  showPop(btn, refs.map(r => `<p><b>[${r.num}]</b> ${esc(r.raw)}</p>
    ${r.resolved_paper_id ? `<div class="acts"><button type="button" data-read="${esc(r.resolved_paper_id)}" data-sid="">Mở bài này trong kho</button></div>` : ""}`).join(""));
}

// ---------------------------------------------------------------- lexical overlap ("Dò trùng lặp")
function markSpans(text, spans) {
  let out = "", pos = 0;
  for (const sp of spans || []) {
    if (sp.start < pos) continue;
    out += esc(text.slice(pos, sp.start)) + `<mark>${esc(text.slice(sp.start, sp.end))}</mark>`;
    pos = sp.end;
  }
  return out + esc(text.slice(pos));
}
function renderOverlap(res) {
  const pct = x => `${Math.round(x * 100)}%`;
  if (!res.papers.length) return `<p class="status"><span class="note">Không thấy câu nào trong kho hiện tại dùng cùng cụm từ (không có nghĩa là không có trong y văn).</span></p>`;
  return res.papers.map(p => {
    const hits = res.hits.filter(h => h.paper_id === p.paper_id);
    return `<article class="hit"><div class="paper"><a href="#" data-read="${esc(p.paper_id)}" data-sid="">${esc(p.cite || p.paper_id)}</a>
      <span class="t">— ${esc(p.title || "")}</span></div>
      <div class="where">cùng từ ngữ với ${pct(p.coverage)} đoạn dán vào · ${hits.length} câu</div>` + hits.map(h =>
      `<blockquote>${supify(markSpans(h.text, h.matched_spans))}</blockquote>
       <div class="where">${esc(sec(h.section))} · trang ${esc(h.page)} · <code>${esc(short(h.sentence_id))}</code> · giống ${pct(h.score)}</div>
       <div class="acts">${p.has_pdf ? pdfLink(p.paper_id, h.page) : ""}<button type="button" data-read="${esc(p.paper_id)}" data-sid="${esc(h.sentence_id)}">Đọc trong bài</button></div>`).join("") + `</article>`;
  }).join("");
}
async function runOverlap() {
  const text = $("#overlapText").value.trim();
  if (!text) return;
  $("#overlapStatus").textContent = "Đang dò…";
  try {
    const res = await api("/api/overlap", { text, vault: state.vault, limit: 40 });
    $("#overlapStatus").textContent = `Kết quả: ${res.label} (không phải kết luận đạo văn) · kho “${state.vault}”`;
    $("#overlapOut").innerHTML = renderOverlap(res);
  } catch (e) {
    $("#overlapStatus").innerHTML = `<span class="err">Lỗi: ${esc(e.message)}</span>`;
  }
}
function toggleOverlap() {
  const on = $("#overlapPanel").hidden;
  $("#overlapPanel").hidden = !on;
  document.body.classList.toggle("overlap-mode", on);
  if (on) $("#overlapText").focus();
}

// ---------------------------------------------------------------- start / events
async function submit(q) {
  state.q = q.trim();
  state.queries = [];
  state.answer = null;
  $("#aiPanel").hidden = true;
  setUrl();
  if (!state.q) return;
  if (VI.test(state.q) && aiReady()) {
    $("#status").innerHTML = `<span class="loading"><span class="spin"></span> AI đang đổi câu hỏi sang từ khóa tiếng Anh…</span>`;
    try {
      const t = await api("/api/translate", { question: state.q, speed: state.speed });
      if (t.error) $("#status").innerHTML = `<span class="err">${esc(t.error)}</span>`;
      state.queries = t.queries || [];
    } catch (e) {
      $("#status").innerHTML = `<span class="err">${esc(e.message)}</span>`;
    }
  }
  await runSearch();
  runAnswer();
}

function setView(v) {
  state.view = v;
  $$("#viewTabs button").forEach(b => b.classList.toggle("on", b.dataset.view === v));
  saveUi();
  setUrl();
  if (state.search) runAnswer();
}

function setSpeed(s) {
  state.speed = s;
  $$("#speedSeg button").forEach(b => b.classList.toggle("on", b.dataset.speed === s));
  saveUi();
  setUrl();
  if (state.q) { runSearch(); runAnswer(); }
}

async function init() {
  const ui = loadUi();
  const params = new URLSearchParams(location.search);
  state.view = params.get("view") || ui.view || "evidence4";
  state.speed = params.get("speed") || ui.speed || "fast";
  state.askAi = ui.askAi === true;
  $("#askAi").checked = state.askAi;
  setView(state.view);
  setSpeed(state.speed);
  $(".layout").classList.add("nofilters");
  if (window.innerWidth < 820) $("#filters details").open = false; // results first on small screens
  try {
    const st = await api("/api/status");
    state.ai = st.ai;
    const pill = $("#aiStatus");
    pill.textContent = st.ai.ready ? 'AI đã nối · '+(st.ai.model||st.ai.provider) : 'AI chưa nối trực tiếp';
    pill.title = st.ai.message;
    pill.className = `pill ${st.ai.ready ? "ready" : "off"}`;
    const mp = $("#meaningStatus");
    mp.textContent = st.meaning.message + (st.mesh.ready ? " · MeSH" : "");
    mp.className = `pill ${st.meaning.ready ? "ready" : "off"}`;
    const chosen = ui.vault && st.vaults.find(v => v.name === ui.vault) ? ui.vault : st.default_vault;
    $("#vault").innerHTML = st.vaults.map(v => `<option value="${esc(v.name)}" ${v.name === chosen ? "selected" : ""}>${esc(v.name)} (${v.papers} bài)</option>`).join("");
    state.vault = $("#vault").value;
  } catch (e) {
    $("#status").innerHTML = `<span class="err">Không kết nối được máy chủ: ${esc(e.message)}</span>`;
  }
  const q = params.get("q");
  if (q) { $("#q").value = q; submit(q); }
}

$("#overlapToggle").addEventListener("click", e => { e.preventDefault(); toggleOverlap(); });
$("#overlapGo").addEventListener("click", runOverlap);
$("#searchForm").addEventListener("submit", e => { e.preventDefault(); submit($("#q").value); });
$("#viewTabs").addEventListener("click", e => { const b = e.target.closest("button"); if (b) setView(b.dataset.view); });
$("#speedSeg").addEventListener("click", e => { const b = e.target.closest("button"); if (b) setSpeed(b.dataset.speed); });
$("#askAi").addEventListener("change", e => { state.askAi = e.target.checked; saveUi(); if(state.q) runAnswer(); });
$("#vault").addEventListener("change", e => { state.vault = e.target.value; saveUi(); if (state.q) submit(state.q); });
$("#filters").addEventListener("change", () => { if (state.q) runSearch(); });
$("#clearFilters").addEventListener("click", () => {
  $$("#filters input[type=checkbox]").forEach(x => { x.checked = false; });
  $('input[name="evidence"][value=""]').checked = true;
  $("#yearFrom").value = ""; $("#yearTo").value = "";
  if (state.q) runSearch();
});
$("#moreBtn").addEventListener("click", () => { state.offset += state.limit; runSearch({ append: true }); });
$("#readerClose").addEventListener("click", () => { $("#reader").hidden = true; hidePop(); });
$("#readerTabs").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { reader.sid = ""; setReaderTab(b.dataset.tab); } });

document.addEventListener("click", e => {
  const t = e.target.closest("button, a");
  if (!t) {
    if (!e.target.closest("#pop")) hidePop();
    return;
  }
  if (t.classList.contains("x")) return hidePop();
  if (t.dataset.read !== undefined) { e.preventDefault(); return openReader(t.dataset.read, t.dataset.sid); }
  if (t.dataset.ref) return refPopover(t.dataset.ref, t);
  if (t.dataset.item) return itemPopover(Number(t.dataset.item), t);
  if (t.dataset.ctx !== undefined) return toggleContext(t);
  if (t.dataset.refnums !== undefined) return readerCitation(t);
  if (t.dataset.goto) { reader.sid = t.dataset.goto; return setReaderTab("text"); }
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { if (!$("#pop").hidden) hidePop(); else $("#reader").hidden = true; }
  if (e.key === "/" && document.activeElement.tagName !== "INPUT") { e.preventDefault(); $("#q").focus(); }
});

init();
