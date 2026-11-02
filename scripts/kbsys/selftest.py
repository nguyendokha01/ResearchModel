"""`kb.py selftest`: check the v2 data contracts (docs/CONTRACTS.md) automatically.

Two parts:
  - unit checks of the Tier 1 parsers on fixed examples (no data needed);
  - invariant checks of every stored paper (files, ids, I1-I4, database counts).
Every agent runs this before committing; a failure must be fixed, not ignored.
"""

import sqlite3
import json
import re
import unicodedata
from pathlib import Path

from . import concepts, concept_notes, config, crosscheck, db, discovery, evidence, extract, graph, ghosts, notes, objects, presentation, refmanager, references, relations, sentences

SECTIONS = {"front_matter", "abstract", "introduction", "methods", "results", "results_discussion",
            "discussion", "conclusion", "main_text", "figure_legends", "supplementary", "acknowledgements",
            "funding", "author_contributions", "conflicts", "data_availability", "references", "notes"}
KINDS = {"text", "caption", "table_row"}
STYLES = {"numeric", "superscript", "author_year"}
PAPER_STYLES = {"numeric", "superscript", "author_year", "mixed", "none", "unknown"}
CONFIDENCE = {"high", "low"}
PARSE_CONFIDENCE = {"high", "medium", "low"}
TABLE_METHODS = {"pymupdf_tables", "word_positions", "text_fallback", "csv", "xlsx"}
TIER1_FIELDS = {"built_at", "from", "sentences", "references", "figures", "tables", "cited_by_links",
                "citation_counts"}
EVIDENCE_FIELDS = {"unit_type", "statement", "sentence_ids", "quote", "pages", "sections", "model",
                   "intervention", "outcome", "quality", "hedging", "cited_refs", "terms", "status", "flags",
                   "analyst", "attempt", "created_at"}
NOTE_MOC_HEADINGS = tuple("## " + h for h in concept_notes.MOC_SECTIONS)
ATTACHMENT_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf", ".csv", ".xlsx"}
TERM_TYPES = ("topics", "concepts", "methods", "species", "molecules", "cell_types")


class Report:
    def __init__(self):
        self.errors, self.warnings, self.checked = [], [], 0

    def error(self, where, message):
        self.errors.append(f"{where}: {message}")

    def warn(self, where, message):
        self.warnings.append(f"{where}: {message}")

    def expect(self, condition, where, message):
        self.checked += 1
        if not condition:
            self.error(where, message)


# ---------------------------------------------------------------- unit checks

def _texts(text):
    return [text[a:b] for a, b in sentences.split_sentences(text)]


def unit_checks(report):
    r = report.expect
    r(extract.normalize_font_glyphs("SSEA-3þ, GFPþ/CK7þ; þ cell") == "SSEA-3+, GFP+/CK7+; + cell"
      and extract.normalize_font_glyphs("\x07", "AdvP4C4E74") == "±", "text/font_glyphs",
      "repair positive-marker glyphs and numeric standard-error separator")
    r(extract.normalize_font_glyphs("ART\t\x07Assisted", "TimesNewRomanPSMT") == "ART\t Assisted",
      "text/font_glyphs", "BEL outside the verified font is an invisible tab leader, not ±")
    r(extract.normalize_font_glyphs("P,0.005 \x06 \x05", "AdvP7DA6") == "P<0.005 \x06 \x05"
      and extract.normalize_font_glyphs("a-MEM Ó", "Symbol") == "α-MEM ©",
      "text/font_glyphs", "Advent '<' printed as ',' and Adobe Symbol-encoded letters")
    line = {"dir": (1, 0), "spans": [
        {"text": "fertility.", "size": 10, "flags": 0, "origin": (0, 100), "font": "T"},
        {"text": "2 ", "size": 6, "flags": 1, "origin": (40, 96), "font": "T"},
        {"text": "The combina\xad", "size": 10, "flags": 0, "origin": (45, 100), "font": "T"}]}
    r(extract.line_text(line) == "fertility.^2 The combina-", "extract/line_text",
      f"space after a superscript kept; soft hyphen at a line end is a hyphen: {extract.line_text(line)!r}")
    r(extract.normalize_font_glyphs("þing") == "þing", "text/font_glyphs", "preserve thorn inside a word")
    r(extract.normalize_font_glyphs("6") == "6", "text/font_glyphs", "ordinary-font six remains six")
    r(extract.normalize_font_glyphs("6", "AdvPS586B") == "±", "text/font_glyphs", "font A plus-minus font")
    r(extract.normalize_font_glyphs("6", "ABC+AdvPS586B") == "±", "text/font_glyphs",
      "subset-prefixed font names use the verified base-font map")
    r(extract.normalize_font_glyphs("6", "AdvP80675") == "±", "text/font_glyphs", "font B plus-minus font")
    r(extract.normalize_font_glyphs("3 2", "AdvPS586B") == "× −", "text/font_glyphs",
      "font A multiplication and minus symbols")
    r(extract.normalize_font_glyphs("1 2 5", "AdvP80675") == "+ - =", "text/font_glyphs",
      "font B plus, hyphen and equals symbols")
    r(extract.normalize_font_glyphs("a b m", "AdvPS3D56D5") == "α β μ", "text/font_glyphs",
      "font A Greek glyphs")
    r(extract.normalize_font_glyphs("9", "AdvPS7DA6") == "′", "text/font_glyphs",
      "font A prime glyph")
    r(extract.normalize_font_glyphs("p", "AdvPS7DC6") == "*", "text/font_glyphs",
      "font A asterisk glyph (rendered '*, p < .05')")
    r(extract.normalize_font_glyphs("0", "AdvP4C4E74") == "′", "text/font_glyphs",
      "font B prime glyph")
    r(extract.normalize_font_glyphs("8", "AdvP7DA6") == "°", "text/font_glyphs",
      "font B degree glyph")
    r(extract.normalize_font_glyphs("l p", "AdvPSMP10") == "μ β", "text/font_glyphs",
      "font B micro and beta glyphs")
    r(extract.normalize_font_glyphs("0", "AdvP4C4E74") == "′", "text/font_glyphs",
      "4-prime 6-prime glyph")
    r(extract.normalize_font_glyphs("a b m", "AdvP7DA6") == "α β μ", "text/font_glyphs",
      "font C Greek glyphs")
    r(extract.normalize_font_glyphs("8", "AdvPi1") == "°", "text/font_glyphs",
      "font C degree glyph")
    r(extract.normalize_font_glyphs("0 ¼ þ", "AdvP4C4E74") == "′ = +", "text/font_glyphs",
      "font C prime, equals and verified positive glyphs")
    r(extract.normalize_font_glyphs("8", "AdvP7DA6") == "°", "text/font_glyphs",
      "font B degree glyph")
    r(extract.normalize_font_glyphs("9 3 p", "AdvPS7DA6") == "′ 3 p", "text/font_glyphs",
      "font A prime mapping leaves unmapped codes unchanged")
    r(extract.unmapped_advent_glyphs("p", "AdvPS7DC6") == set(), "text/font_glyphs",
      "verified asterisk code is not reported")
    r(extract.unmapped_advent_glyphs(",", "AdvP7DB7") == set() and extract.unmapped_advent_glyphs(";", "AdvP7DB7") == {";"},
      "text/font_glyphs", "punctuation in a symbol font is reviewable unless verified")
    r(extract.unmapped_advent_glyphs("A10Mm", "ABC+AdvP2A67") == set(), "text/font_glyphs",
      "verified ordinary text subset font is exempt")
    r(extract.unmapped_advent_glyphs("A10Mm", "ABC+AdvP2AA1") == set(), "text/font_glyphs",
      "verified ordinary text subset fonts are individually exempt")
    r(extract.unmapped_advent_glyphs("A10", "ABC+AdvPUnknown") == {"A", "1", "0"},
      "text/font_glyphs", "unverified Advent font remains reviewable")
    r(extract.unmapped_advent_glyphs("A6", "ABC+AdvPS586B") == {"A"}, "text/font_glyphs",
      "unknown alphanumeric code remains reviewable after subset-prefix removal")
    crosscheck.unit_checks(report)
    r(ghosts.ghost_id({"doi": "10.1234/ABC."}) == "ghost:doi:10.1234/abc",
      "ghost_id", "DOI IDs are case-normalized")
    r(ghosts.ghost_id({"title": "A Study: Cells", "year": 2020}) ==
      ghosts.ghost_id({"title": "a study cells", "year": 2020}),
      "ghost_id", "normalized title/year IDs are stable")
    r(ghosts.ghost_id({"title": "Title only"}) is None,
      "ghost_id", "incomplete metadata is not merged")
    r(discovery._selfcheck() == 5, "citation_discovery", "metadata normalization and DOI/title-year duplicate checks")
    r(refmanager._selfcheck() == 14, "reference_manager", "CSL JSON, BibTeX, RIS, Zotero fixtures, malformed records, and SQLite refresh")
    r(notes._selfcheck() == 7, "notes", "Windows-safe note names; index notes keep human text")
    r(evidence._selfcheck() == 17, "evidence/validator", "V1-V12 on a fixture: numbers, association/hypothesis "
      "wording, citing sentences, malformed types, boilerplate never must-cover")
    r(concept_notes._selfcheck() == 10, "concept_notes",
      "checked rows survive offline refresh; no blank-line growth; orphaned machine notes retired reversibly")
    test_registry = concepts.Registry([
        {"concept_id": "local:muse-cell", "name": "Muse cell", "synonyms": ["Alpha stress-resistant cell"]}
    ])
    r(concept_notes._text_concept_ids({"quote": "Muse cell was observed."}, {"local:muse-cell"}, test_registry)
      == {"local:muse-cell"} and
      concept_notes._text_concept_ids({"quote": "Muse cells were observed."}, {"local:muse-cell"}, test_registry)
      == {"local:muse-cell"} and
      not concept_notes._text_concept_ids({"quote": "amusement cells"}, {"local:muse-cell"}, test_registry),
      "concept_notes/linking", "registry aliases match whole words in evidence prose")
    original_load_paper = db.load_paper
    original_relink_paper = concepts.relink_paper
    reload_calls = []
    try:
        db.load_paper = lambda con, paper_id: reload_calls.append(("load", paper_id))
        concepts.relink_paper = lambda con, paper_id, *before: reload_calls.append(("relink", paper_id))
        fixture_con = sqlite3.connect(":memory:")
        fixture_con.executescript("CREATE TABLE concept_links (concept_id, paper_id); CREATE TABLE paper_terms (term, paper_id);")
        db.reload_paper(fixture_con, "paper-fixture")
        fixture_con.close()
        r(reload_calls == [("load", "paper-fixture"), ("relink", "paper-fixture")],
          "db/reload_paper", "single-paper reload always relinks after loading")
    finally:
        db.load_paper = original_load_paper
        concepts.relink_paper = original_relink_paper
    # Changed concepts are checked against every paper's exact sentence matcher,
    # independently of FTS candidate recall.
    original_read_sentences = concepts._read_sentences
    original_read_studied = concepts._read_studied_terms
    original_execute = None
    class FakeConnection:
        def __init__(self): self.rows = []
        def execute(self, sql, params):
            self.rows.append((sql, params))
            return self
    try:
        concepts._read_sentences = lambda pid: [
            {"sentence_id": f"{pid}:S0001", "seq": 1, "section": "results", "kind": "text",
             "text": "Muse cell was observed." if pid == "paper-b" else "Other cell was observed."}]
        concepts._read_studied_terms = lambda pid, registry: set()
        fake_con = FakeConnection()
        found = concepts._link_changed_exact(fake_con, test_registry, test_registry.records,
                                             ["paper-a", "paper-b", "paper-c"])
        r(found == 1 and len(fake_con.rows) == 1 and fake_con.rows[0][1][0] == "local:muse-cell"
          and fake_con.rows[0][1][1] == "paper-b",
          "concept_links/changed_exact", "exact changed-concept scan finds match even without FTS candidates")
    finally:
        concepts._read_sentences = original_read_sentences
        concepts._read_studied_terms = original_read_studied
    ambiguous_registry = concepts.Registry([
        {"concept_id": "local:a", "name": "Concept A", "synonyms": ["Shared alias", "MSC"]},
        {"concept_id": "local:b", "name": "Concept B", "synonyms": ["Shared alias"]},
    ])
    r(not concept_notes._text_concept_ids({"quote": "Shared alias."}, {"local:a"}, ambiguous_registry),
      "concept_notes/linking", "ambiguous aliases are ignored")
    r(concept_notes._text_concept_ids({"quote": "MSC."}, {"local:a"}, ambiguous_registry) == {"local:a"}
      and not concept_notes._text_concept_ids({"quote": "MSCinside."}, {"local:a"}, ambiguous_registry),
      "concept_notes/linking", "unique short aliases require complete source tokens")
    sample_units = [{"paper_id": f"p{i // 12}", "unit_id": f"u{i:03}",
                     "unit_type": "limitation" if i % 3 == 0 else "result",
                     "outcome": {"values": ["x"] if i % 2 else []}} for i in range(120)]
    selected = concept_notes._select_payload_units(sample_units)
    r(len(selected) == 60 and len({u["paper_id"] for u in selected}) == 10 and
      max(sum(u["paper_id"] == pid for u in selected) for pid in {u["paper_id"] for u in selected}) <= 8,
      "concept_notes/payload", "payload is bounded and capped per paper")
    fingerprint_record = {"concept_id": "local:test", "name": "Test concept", "type": "concept"}
    units_a = [{"unit_id": "p:E0001", "paper_id": "p", "status": "ok", "statement": "Evidence one",
                "quote": "Evidence one.", "paper": {"authors": [{"family": "Test"}], "year": 2020},
                "unit_type": "result", "outcome": {"direction": "increase"}}]
    units_mention_changed = [{**units_a[0], "terms": {"concepts": ["Test concept"]}}]
    units_evidence_changed = [{**units_a[0], "statement": "Evidence changed"}]
    payload_a = concept_notes._payload_fingerprint(fingerprint_record, units_a, "en", "mock", "m1")
    units_mention_changed[0]["terms"] = {"concepts": ["Unrelated mention"]}
    r(payload_a == concept_notes._payload_fingerprint(fingerprint_record, units_mention_changed, "en", "mock", "m1"),
      "concept_notes/synthesis_fingerprint", "mention/link metadata changes do not change selected synthesis payload")
    r(payload_a != concept_notes._payload_fingerprint(fingerprint_record, units_evidence_changed, "en", "mock", "m1"),
      "concept_notes/synthesis_fingerprint", "evidence statement changes invalidate synthesis payload")
    synthesis_fixture = {"sentences": [{"text": "Checked sentence.", "evidence": ["p:E0001"]}], "questions": []}
    rendered = concept_notes._render(fingerprint_record, {}, units_a, [], synthesis_fixture["sentences"], [],
                                     "retained", {"status": "done", "provider": "mock", "model": "m1"},
                                     "0" * 40, test_registry)
    for row in synthesis_fixture["sentences"]:
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True)
        rendered = re.sub(r"(?m)^(- Checked sentence\..*)$",
                          lambda m, value=encoded: m.group(1) + " <!-- kb-synthesis:" + value + " -->",
                          rendered, count=1)
    saved = re.search(r"(?m)^- Checked sentence\..*<!-- kb-synthesis:(\{.*\}) -->$", rendered)
    r(bool(saved) and json.loads(saved.group(1)) == synthesis_fixture["sentences"][0],
      "concept_notes/synthesis_preservation", "checked sentence and evidence IDs survive deterministic rerender metadata")
    stale = concept_notes._render(fingerprint_record, {}, units_a, [], synthesis_fixture["sentences"], [],
                                  "Synthesis needs update — evidence changed.",
                                  {"status": "stale", "provider": "mock", "model": "m1"},
                                  "1" * 40, test_registry, synthesis_stale=True)
    r("Checked sentence." in stale and "needs update — evidence changed" in stale and '"status": "stale"' in stale,
      "concept_notes/synthesis_preservation", "changed evidence retains checked synthesis and marks it stale")
    refs, registry = ghosts.build_registry({"citing": [
        {"ref_id": "citing:R001", "doi": "10.1234/a", "resolved_paper_id": None},
        {"ref_id": "citing:R002", "doi": "10.1234/a", "resolved_paper_id": None},
        {"ref_id": "citing:R003", "doi": "10.1234/b", "resolved_paper_id": "in_library"},
    ]})
    r(len(registry) == 1 and registry[next(iter(registry))]["count"] == 2 and
      refs["citing"][2]["ghost_id"] is None,
      "ghost_registry", "deduplicate unresolved refs and clear IDs for resolved refs")
    got = _texts("Cells were counted (Fig. 2A). E. coli was used, e.g. strain K12. Marlow et al. reported "
                 "this. The value was 0.5 ± 0.1 vs. 0.8 in controls. Results are shown.^1,7–10 Next sentence.")
    r(got == ["Cells were counted (Fig. 2A).", "E. coli was used, e.g. strain K12.",
              "Marlow et al. reported this.", "The value was 0.5 ± 0.1 vs. 0.8 in controls.",
              "Results are shown.^1,7–10", "Next sentence."], "split_sentences", f"got {got}")
    r(_texts("A sentence ends here. and a lowercase start continues.") ==
      ["A sentence ends here. and a lowercase start continues."], "split_sentences", "lowercase start must not split")

    r(sentences.expand_numbers("1,7–10") == [1, 7, 8, 9, 10], "expand_numbers", "range expansion")
    r(sentences.expand_numbers("5-3") is None, "expand_numbers", "reversed range must be rejected")

    numbered = [{"num": n, "first_author": "X", "year": 2000} for n in range(1, 21)]
    ctx = sentences.CitationContext(numbered, {"numeric_paren": 5, "superscript": 5})
    cites = sentences.find_citations("As shown before [3, 5–7], cells grew.", ctx, "results")
    r([c["ref_nums"] for c in cites] == [[3, 5, 6, 7]], "citations/bracket", f"got {cites}")
    r(cites and "[3, 5–7]" == cites[0]["marker"], "citations/bracket", "marker text")
    cites = sentences.find_citations("Muse cells repair tissue (12). SSEA-3(1) cells (n = 12) and (Fig. 3).",
                                     ctx, "discussion")
    r([c["ref_nums"] for c in cites] == [[12]], "citations/paren", f"only (12) is a citation, got {cites}")
    cites = sentences.find_citations("Cells were damaged.^4,6 We used 5×10^5 cells in 75cm^2 flasks with SSEA-3^+.",
                                     ctx, "introduction")
    r([c["ref_nums"] for c in cites] == [[4, 6]], "citations/superscript", f"exponents are not citations, got {cites}")
    r(sentences.find_citations("Brandt^1,2, Lindqvist^3", ctx, "front_matter") == [], "citations/front_matter",
      "affiliation markers are not citations")

    ay = [{"num": None, "first_author": "Marlow", "year": 2010}, {"num": None, "first_author": "Okafor", "year": 2011},
          {"num": None, "first_author": "Smith", "year": 2015, "year_suffix": "a"},
          {"num": None, "first_author": "Smith", "year": 2015, "year_suffix": "b"},
          {"num": None, "first_author": "Lee", "year": 2012}, {"num": None, "first_author": "Lee", "year": 2012}]
    ctx = sentences.CitationContext(ay)
    text = ("Muse cells were described (Marlow et al. 2010; Okafor et al., 2011) and later (see Smith 2015b). "
            "Marlow et al. (2010) isolated them. Two Lee papers exist (Lee et al. 2012). In (March 2015) nothing.")
    got = [(c["ref_nums"], c["confidence"]) for c in sentences.find_citations(text, ctx, "introduction")]
    r(got == [([1], "high"), ([2], "high"), ([4], "high"), ([1], "high"), ([5, 6], "low")],
      "citations/author_year", f"got {got}")

    m = sentences.mentions("See Fig. 3B, Figs. 4 and 5, Table 2 and Supplementary Fig. S1.", "p")
    r(m == ["p:F3", "p:F4", "p:F5", "p:T2", "p:FS1"], "mentions", f"got {m}")

    f = references.parse_fields("Marsh K, Oakley C, Bright R. Counting small boats: sailing along a quiet coast. "
                                "Lancet Neurol 2007;6:101-109.")
    r((f["first_author"], f["year"], f["title"], f["journal"]) ==
      ("Marsh", 2007, "Counting small boats: sailing along a quiet coast", "Lancet Neurol"), "references/vancouver", str(f))
    f = references.parse_fields("Reed, O. Y.; Cole, J. S.; Lane, P. H.; Hart, G. Adaptive coastal erosion "
                                "modelling in estuary basins. Ann. Neurol. 57(6):874–882; 2005.")
    r((f["first_author"], f["year"], f["title"]) ==
      ("Reed", 2005, "Adaptive coastal erosion modelling in estuary basins"), "references/semicolon", str(f))
    f = references.parse_fields("Okafor S, Akin H, Hollis Y, Lindqvist M (2014) Reef cells, recently described non-tumorigenic "
                                "microbes, live in coastal mud flats. Pathol Int 64: 1–9. "
                                "doi: 10. 1111/xyz.54321 PMID: 12345678")
    r((f["year"], f["doi"], f["pmid"], f["title"][:10]) == (2014, "10.1111/xyz.54321", "12345678", "Reef cells"),
      "references/author_year", str(f))

    lines = [{"page": 1, "bbox": [60, 100, 300, 110], "text": "Marlow Y, Kendal M. Unique multipotent cells."},
             {"page": 1, "bbox": [45, 100, 55, 110], "text": "1."},
             {"page": 1, "bbox": [60, 112, 300, 122], "text": "Prog Neurobiol 2011;95:213-"},
             {"page": 1, "bbox": [60, 124, 300, 134], "text": "228."},
             {"page": 1, "bbox": [60, 136, 300, 146], "text": "Okafor S. Muse cells. Pathol Int 2014;64:1-9."},
             {"page": 1, "bbox": [45, 136, 55, 146], "text": "2."},
             {"page": 1, "bbox": [60, 148, 300, 158], "text": "3. Lindqvist M. Review. Adv Exp Med Biol 2018;1103:1-9."}]
    refs = references.parse_lines(lines, (set(), set()))
    r([x["num"] for x in refs] == [1, 2, 3] and refs[0]["raw"].endswith("213-228."),
      "references/lines", f"number printed after the first line; '228.' is not entry 228: {refs}")

    def ref_lines(numbers):
        return [{"page": 1, "bbox": [45, 100 + i * 20, 55, 110 + i * 20],
                 "text": f"{n}. Author A. Short title {n}. Journal 2020;1:1-2."}
                for i, n in enumerate(numbers)]
    for label, nums in (("one", [1]), ("two", [1, 2]), ("gapped", [1, 3])):
        got_refs = references.parse_lines(ref_lines(nums), (set(), set()))
        r([x["num"] for x in got_refs] == nums, f"references/{label}", f"got {got_refs}")

    r(objects.row_text(["Group", "n", "Rate (%)"], ["Control", "120", "32.5 ± 2.1"]) ==
      "Group: Control | n: 120 | Rate (%): 32.5 ± 2.1", "table rows", "row text with column names")
    r(objects.row_text(["A", "B"], ["x", "1"], with_names=False) == "x | 1", "table rows", "uncertain tables: no names")
    r(objects.label_of("Table 2. Effects") == ("table", False, 2) and objects.label_of("Figure 1C shows") is not None,
      "labels", "label parsing")

    r(sentences.numbers("1,230 cells, .50 mM, 41.0%, SSEA-3, CD105, H2O2, from 0% to.5%") ==
      {"1230", "0.5", "41", "0"}, "numbers", f"got {sentences.numbers('1,230 cells, .50 mM, 41.0%, SSEA-3, CD105, H2O2, from 0% to.5%')}")
    r(sentences.numbers("-2, −0.5, (–3), 10-20, IL-6, CD-1") == {"-2", "-0.5", "-3", "10", "20"},
      "numbers/sign", f"got {sentences.numbers('-2, −0.5, (–3), 10-20, IL-6, CD-1')}")
    damaged = "(P,0.005) only ,10% vs Muse-poor,12.5%±1.1% injectionof150mg; refs ^1,7–10 [3,4] Hist1h3g"
    r(sentences.numbers(damaged) == {"0.005", "10", "12.5", "1.1", "150", "1", "3"},
      "numbers/pdf damage", f"got {sentences.numbers(damaged)}")
    # page furniture, sidebars, back-matter references and tables read as text (extraction logic)
    r(extract.is_link_only("https://doi.org/10.1371/journal.pone.0000001.g001")
      and not extract.is_link_only("doi:10.1/x.g002 and 30 days post-treatment was better"),
      "extract/link-only", "a link block is furniture, a sentence with a link is not")
    sides = {"Funding: This work was supported by X.": "funding", "Competing interests: none.": "conflicts",
             "Data Availability Statement: All data are in the paper.": "data_availability",
             "Academic Editor: A. B, NIH. Received: May 1, 2015": "front_matter",
             "Funding of stem cell research has grown.": None, "Received data were analysed.": None}
    for text, expected in sides.items():
        r(extract.sidebar_section(text) == expected, "extract/sidebar", f"{text!r} -> {extract.sidebar_section(text)}")
    r(extract.sidebar_section("© The Author(s) 2020 Open Access This article…", True) == "front_matter"
      and extract.sidebar_section("^†These authors contributed equally.", True) == "front_matter"
      and extract.sidebar_section("^†These authors contributed equally.") is None,
      "extract/sidebar", "first-page licence and footnote blocks are front matter")
    r(extract.looks_like_references("21. Fenwick AH, Lorne D (2005) Marking of spools. Curr Opin 17: 1–7.")
      and not extract.looks_like_references("We thank K. Osei and R. M. Blake for providing plasmids in 2010."),
      "extract/references", "reference entries vs acknowledgements")
    r(evidence.table_like("Age 67.6±9.1 70.3±8.8 67.9±9.7 0.30 Sex 39/25 38/6* 64/15* 0.003 HTN 32 (50.0) 34 (77.3)* "
                          "55 (69.6)* 0.007 HL 24 (37.5) 21 (47.7) 53 (67.1)*")
      and not evidence.table_like("On day 0, Muse cells were 149±98 cells/100 μL in AMI and 164±125 in controls."),
      "evidence/table-like", "a table printed as text is not must-cover")
    cut = [{"sentence_id": f"P:S{i}", "kind": k, "text": t, "passage_id": p, "source": "main", "section": "results",
            "page": pg} for i, (k, t, p, pg) in enumerate([
                ("text", "The max. Alpha in the test group was significantly", "a", 5),
                ("caption", "Figure 4. Muse cells.", "b", 6), ("text", "greater than in controls (Figure 2B).", "c", 6),
                ("text", "Values are shown", "c", 6), ("text", "Results were robust.", "d", 7),
                ("text", "(Rudenko and Tsai, 2014).", "d", 7)])]
    cut[5]["citations"] = [{"start": 0, "end": 25}]
    sentences.link_page_breaks(cut)
    r(cut[0].get("continues") == "P:S2" and "continues" not in cut[3] and "continues" not in cut[4],
      "sentences/continues", "a sentence cut by a figure is linked across the caption; a capitalised next "
      "sentence is not; a sentence ending in a period is complete and does not absorb a following "
      "citation-only row (D3.40, tests/test_sentence_continuations.py)")
    ay_refs = [{"num": None, "first_author": "Marlow", "year": 2010},
               {"num": None, "first_author": "Okafor", "year": 2011}]
    r(evidence._reference_by_number(ay_refs)[1]["first_author"] == "Marlow" and
      evidence._reference_by_number(ay_refs)[2]["first_author"] == "Okafor",
      "reading_view/author_year", "author-year references must use reading-order indices")
    r(sentences.split_sentences("ranging from 0 to 10/μL. The mean was 3.5.") == [(0, 24), (25, 42)],
      "split_sentences", "a unit such as μL ends a sentence")
    r(not relations._source_contains("rat", ["s1"], {"s1": {"text": "Cell migration increased."}}),
      "relations/source-boundary", "rat must not match inside migration")
    r(relations._source_contains("rat", ["s1"], {"s1": {"text": "Adult rats were observed."}}),
      "relations/source-plural", "rat matches the regular plural rats")
    r(not relations._source_contains("R", ["s1"], {"s1": {"text": "Adult rats were observed."}}),
      "relations/source-short", "single-character values are rejected")


# ---------------------------------------------------------------- stored papers

ID = {"sentence": r"S\d{4,}", "reference": r"R\d{3,}", "figure": r"FS?\d+", "table": r"TS?\d+"}


def check_paper(report, con, paper_id):
    folder = db.paper_dir(paper_id)
    meta = db.read_json(folder / "metadata.json")
    where = paper_id
    if not meta or (meta.get("extraction_version") or 0) < 2:
        report.warn(where, "Tier 1 not built yet (run `kb.py reextract` for this paper)")
        return
    for source in meta.get("source_files", []):
        raw_path = source.get("path")
        if not raw_path or source.get("role") != "main":
            continue
        pdf_path = Path(raw_path)
        if not pdf_path.is_absolute():
            pdf_path = config.ROOT / pdf_path
        if pdf_path.is_file():
            unknown = extract.scan_unmapped_advent_glyphs(pdf_path)
            if unknown:
                summary = "; ".join(f"{font}: {chars}" for font, chars in unknown.items())
                report.warn(f"{where}:FS2", f"unverified Advent-font glyphs: {summary}")
    report.expect(meta.get("citation_style") in PAPER_STYLES, where, f"citation_style {meta.get('citation_style')!r}")
    tier1 = meta.get("tier1") or {}
    missing_tier1 = sorted(TIER1_FIELDS - set(tier1))
    if missing_tier1:
        report.warn(where, f"legacy metadata.tier1 missing fields {missing_tier1} (run `kb.py reextract --all`)")
    for name in ("sentences.jsonl", "references.jsonl", "figures.jsonl", "tables.jsonl"):
        report.expect((folder / name).exists(), where, f"missing {name}")
    passages = {p["passage_id"]: p for p in db.read_jsonl(folder / "passages.jsonl")}
    # OCR1: OCR-derived passages must lie on pages recorded as OCR'd, with engine provenance.
    ocr_pages = {(p.get("source", "main"), p.get("page")) for p in meta.get("ocr_pages") or []}
    for p in passages.values():
        if p.get("ocr"):
            report.expect((p.get("source", "main"), p.get("page")) in ocr_pages
                          and bool(p["ocr"].get("engine")), f"{where}:OCR1",
                          f"OCR passage {p['passage_id']} is not on a recorded source/page or lacks engine")
    sents = list(db.read_jsonl(folder / "sentences.jsonl"))
    refs = list(db.read_jsonl(folder / "references.jsonl"))
    figs = list(db.read_jsonl(folder / "figures.jsonl"))
    tabs = list(db.read_jsonl(folder / "tables.jsonl"))
    report.expect(tier1.get("sentences") == len(sents), where, "metadata.tier1.sentences must match sentences.jsonl")
    report.expect(tier1.get("references") == len(refs), where, "metadata.tier1.references must match references.jsonl")
    report.expect(tier1.get("figures") == len(figs), where, "metadata.tier1.figures must match figures.jsonl")
    report.expect(tier1.get("tables") == len(tabs), where, "metadata.tier1.tables must match tables.jsonl")
    if "cited_by_links" in tier1:
        report.expect(isinstance(tier1.get("cited_by_links"), int) and tier1.get("cited_by_links") >= 0,
                      where, "metadata.tier1.cited_by_links must be a non-negative integer")
    report.expect(isinstance(tier1.get("citation_counts"), dict), where,
                  "metadata.tier1.citation_counts must be an object")
    ref_nums = {r["num"] for r in refs if r.get("num")} | set(range(1, len(refs) + 1) if not any(
        r.get("num") for r in refs) else [])
    ids = {s["sentence_id"] for s in sents}
    required = set(evidence.required_sentences(evidence.PaperContext(con, paper_id)))
    objects_ids = {f["figure_id"] for f in figs} | {t["table_id"] for t in tabs}
    has_supplement = any(s.get("status") == "extracted" for s in meta.get("supplements") or [])

    # I2 and ids
    report.expect([s["seq"] for s in sents] == list(range(1, len(sents) + 1)), where, "I2: seq must be 1..n")
    covered = {}
    for s in sents:
        w = f"{s.get('sentence_id')}"
        report.expect(re.fullmatch(rf"{re.escape(paper_id)}:{ID['sentence']}", s["sentence_id"] or "") is not None,
                      w, "sentence_id format")
        report.expect(s["section"] in SECTIONS, w, f"section {s['section']!r}")
        report.expect(s["kind"] in KINDS, w, f"kind {s['kind']!r}")
        if s.get("in_table"):
            report.expect(s["kind"] == "text", w, "in_table sentences must have kind text")
            report.expect(s["sentence_id"] not in required, w, "in_table sentences must never be must_cover")
        report.expect(isinstance(s["page"], int), w, "page must be an integer")
        if s["kind"] == "table_row":
            report.expect(s["passage_id"] is None and s.get("object_id") in objects_ids, w,
                          "table_row: passage_id null and object_id of a stored table")
        else:
            p = passages.get(s["passage_id"])
            report.expect(p is not None, w, "passage_id must exist")
            if p is not None:
                report.expect(p["text"][s["start"]:s["end"]] == s["text"], w, "I1: text must be the passage slice")
                covered.setdefault(s["passage_id"], []).append((s["start"], s["end"]))
        for c in s.get("citations", []):
            report.expect(c["style"] in STYLES and c["confidence"] in CONFIDENCE, w, f"citation fields {c}")
            report.expect(s["text"][c["start"]:c["end"]] == c["marker"], w, "citation marker offsets")
            if c["confidence"] == "high":
                report.expect(all(n in ref_nums for n in c["ref_nums"]), w, f"I3: ref_nums {c['ref_nums']} not in references")
        if s["section"] == "references":
            report.expect(not s.get("citations"), w, "I4: no citations inside the reference list")
        for m in s.get("mentions", []):
            # "Fig. S6" lives in a supplementary file: only expected once one is attached
            if m not in objects_ids and (has_supplement or not re.search(r":[FT]S\d+$", m)):
                report.warn(w, f"mentions {m.split(':')[-1]}, which was not detected as a figure/table")
        if s.get("continues"):
            report.expect(s["continues"] in ids, w, "continues must point at a sentence")
    # I1 coverage: only whitespace between the sentences of a passage
    for pid, spans in covered.items():
        text, pos = passages[pid]["text"], 0
        for a, b in sorted(spans):
            report.expect(not text[pos:a].strip(), pid, f"I1: text not covered by sentences near offset {pos}")
            pos = b
        report.expect(not text[pos:].strip(), pid, "I1: end of passage not covered by sentences")
    for pid, p in passages.items():
        if p["kind"] in ("text", "caption") and p["text"].strip() and pid not in covered:
            report.error(pid, "I1: passage has no sentences")

    seen = set()
    for r in refs:
        w = r.get("ref_id", "?")
        report.expect(re.fullmatch(rf"{re.escape(paper_id)}:{ID['reference']}", w) is not None, w, "ref_id format")
        report.expect(w not in seen, w, "duplicate ref_id")
        seen.add(w)
        report.expect(bool(r.get("raw")), w, "raw must be present")
        report.expect(r.get("parse_confidence") in PARSE_CONFIDENCE, w, "parse_confidence")
    for o in figs + tabs:
        oid = o.get("figure_id") or o.get("table_id")
        kind = "figure" if "figure_id" in o else "table"
        report.expect(re.fullmatch(rf"{re.escape(paper_id)}:{ID[kind]}", oid) is not None, oid, "object id format")
        report.expect(all(i in ids for i in o["caption_sentence_ids"] + o["mentioned_in"]), oid,
                      "caption/mention sentence ids must exist")
        if o.get("crop"):
            report.expect((folder / o["crop"]).is_file(), oid, "crop file missing")
        if kind == "table":
            report.expect(o["extraction"] in TABLE_METHODS, oid, f"extraction {o['extraction']!r}")

    n_db = con.execute("SELECT COUNT(*) FROM sentences WHERE paper_id = ?", (paper_id,)).fetchone()[0]
    report.expect(n_db == len(sents), where, f"database has {n_db} sentences, files have {len(sents)} (run rebuild-db)")
    n_db = con.execute("SELECT COUNT(*) FROM references_list WHERE paper_id = ?", (paper_id,)).fetchone()[0]
    report.expect(n_db == len(refs), where, f"database has {n_db} references, files have {len(refs)}")
    expected_citations = {(s["sentence_id"], f"{paper_id}:R{n:03d}", c["style"], c["confidence"])
                         for s in sents for c in s.get("citations", []) for n in c.get("ref_nums") or []}
    actual_citations = {tuple(row) for row in con.execute(
        "SELECT sentence_id, ref_id, style, confidence FROM citations WHERE paper_id = ?", (paper_id,))}
    report.expect(actual_citations == expected_citations, where,
                  "citation table must match citations in sentences.jsonl")

    # Tier 2: every stored quote is exactly the text of its sentences (never typed by an agent)
    by_id = {s["sentence_id"]: s for s in sents}
    units = list(db.read_jsonl(folder / "evidence.jsonl"))
    for k, u in enumerate(units, start=1):
        w = u.get("unit_id", f"{paper_id}:E?")
        report.expect(EVIDENCE_FIELDS <= set(u), w, f"missing evidence fields {sorted(EVIDENCE_FIELDS - set(u))}")
        for field in ("model", "intervention", "outcome", "quality"):
            report.expect(isinstance(u.get(field), dict), w, f"{field} must be an object")
        terms_valid = isinstance(u.get("terms"), dict) and all(
            isinstance(u["terms"].get(t), list) for t in TERM_TYPES)
        if not terms_valid:
            report.warn(w, "legacy terms missing one or more six list fields (recheck reports V9 warnings)")
        report.expect(u.get("unit_id") == f"{paper_id}:E{k:04d}" and u.get("seq") == k, w, "unit_id/seq must be E0001..")
        report.expect(u.get("status") in ("ok", "flagged", "human_confirmed"), w, f"status {u.get('status')!r}")
        missing = [s for s in u.get("sentence_ids", []) if s not in by_id]
        report.expect(not missing, w, f"sentence ids {missing} no longer exist (re-extraction changed ids: re-analyse)")
        if not missing:
            rebuilt, stored = evidence._quote([by_id[s] for s in u["sentence_ids"]]), u.get("quote")
            if stored != rebuilt and isinstance(stored, str) and stored.replace(" […] ", " ") == rebuilt.replace(" […] ", " "):
                report.warn(w, "stored quote has a gap marker the rebuild no longer puts (EV-16: refreshed by recheck-analysis)")
            else:
                report.expect(stored == rebuilt, w, "quote differs from the stored sentences")
        for group, fields in (u.get("field_provenance") or {}).items():
            for field, provenance in fields.items():
                if provenance.get("source") != "ai_enriched":
                    continue
                cited = provenance.get("sentence_ids") or []
                valid_ids = bool(cited) and all(sid in by_id for sid in cited)
                report.expect(valid_ids, w, f"ai_enriched {group}.{field} cites existing sentences")
                if valid_ids:
                    report.expect(_source_value_in_sentences((u.get(group) or {}).get(field), cited, by_id), w,
                                  f"ai_enriched {group}.{field} value appears in its cited sentences")
    if units:
        analysis = db.read_json(folder / "analysis.json", {}) or {}
        report.expect(analysis.get("version") == 2 and analysis.get("n_units") == len(units), where,
                      "analysis.json must be version 2 and count the stored units")
        n_db = con.execute("SELECT COUNT(*) FROM evidence WHERE paper_id = ?", (paper_id,)).fetchone()[0]
        report.expect(n_db == len(units), where, f"database has {n_db} evidence units, file has {len(units)}")
        analysis_terms = {(term_type, term) for term_type in TERM_TYPES
                          for term in (analysis.get(term_type) or [])}
        db_terms = {(row["term_type"], row["term"]) for row in con.execute(
            "SELECT term_type, term FROM paper_terms WHERE paper_id = ?", (paper_id,))}
        report.expect(db_terms == analysis_terms, where, "paper_terms must match the six term lists in analysis.json")
    v13_path = folder / crosscheck.REPORT
    has_v13_flags = any(any(flag.get("code") == "V13" for flag in unit.get("flags", [])) for unit in units)
    if v13_path.exists():
        try:
            v13_report = db.read_json(v13_path)
        except (OSError, ValueError, TypeError) as exc:
            report.error(f"{paper_id}:V13", f"analysis_v13.json is unreadable: {exc}")
        else:
            v13_check = crosscheck.validate_report_units(units, v13_report)
            for message in v13_check["errors"]:
                report.error(f"{paper_id}:V13", message)
            for message in v13_check["warnings"]:
                report.warn(f"{paper_id}:V13", message)
    elif has_v13_flags:
        report.warn(f"{paper_id}:V13", "prior V13 flags are retained pending a current durable analysis_v13.json result")


def check_relations(report):
    """Validate candidate relation IDs, provenance endpoints, and non-accepted status."""
    path = config.STORE / "relations.jsonl"
    if not path.exists():
        return
    seen = set()
    for relation in db.read_jsonl(path):
        rid = relation.get("relation_id")
        report.expect(bool(rid) and rid not in seen, "relations", f"duplicate/missing relation_id {rid!r}")
        seen.add(rid)
        report.expect(relation.get("status") in ("ai_proposed", "human_confirmed", "human_rejected", "withdrawn_by_rule"),
                      f"relation:{rid}", "invalid relation status")
        for key in ("from_unit", "to_unit"):
            unit_id = relation.get(key) or ""
            unit_path = db.paper_dir(unit_id.split(":", 1)[0]) / "evidence.jsonl"
            report.expect(bool(unit_id) and any(u.get("unit_id") == unit_id for u in
                                                 (db.read_jsonl(unit_path) or [])),
                          f"relation:{rid}", f"{key} must reference an existing evidence unit")


def _source_value_in_sentences(value, sentence_ids, by_id):
    norm = lambda x: " ".join(unicodedata.normalize("NFKC", str(x or "")).casefold().split())
    target = norm(value)
    return bool(target) and any(target in norm(by_id[sid].get("text", "")) for sid in sentence_ids if sid in by_id)


def check_concepts(report, con):
    """Registry/link invariants, including the paper-note graph targets."""
    path = config.STORE / "concepts.jsonl"
    n_links = con.execute("SELECT COUNT(*) FROM concept_links").fetchone()[0]
    if not path.exists() and not n_links:
        report.warn("concepts", "concepts.jsonl and concept_links are not built (run `kb.py link-concepts`)")
        return
    records = list(db.read_jsonl(path))
    ids = [r.get("concept_id") for r in records]
    names = [r.get("name", "").lower() for r in records]
    report.expect(len(ids) == len(set(ids)), "concepts", "concept_id must be unique")
    report.expect(len(names) == len(set(names)), "concepts", "one canonical name must have one type")
    valid = {"concept", "method", "molecule", "cell_type", "species", "topic"}
    config_parents = concepts.config_parent_ids(records)  # config/concept_parents.yaml, on top of the MeSH parents
    for record in records:
        record = {**record, "parents": [x for x in record.get("parents", []) if x != config_parents.get(record.get("concept_id"))]}
        report.expect(record.get("type") in valid, "concepts", f"invalid type for {record.get('name')!r}")
        report.expect(isinstance(record.get("synonyms"), list), "concepts", f"synonyms for {record.get('name')!r}")
        mesh_hits = concepts.mesh_hits(record) if concepts.mesh.available() else []
        mesh_match = {hit["descriptor_ui"] for hit in mesh_hits}
        mesh_ui = record.get("mesh_ui")
        if mesh_ui:
            report.expect(record.get("status") in ("mesh", "human_confirmed"), "concepts",
                          f"MeSH-linked status for {record.get('name')!r}")
            report.expect(mesh_match == {mesh_ui}, "concepts",
                          f"MeSH match must be exact and unique for {record.get('name')!r}")
            descriptor = concepts.mesh.descriptor(mesh_ui)
            report.expect(descriptor is not None, "concepts", f"unknown MeSH descriptor {mesh_ui!r}")
            if descriptor:
                report.expect(record.get("mesh_tree_numbers") == descriptor["tree_numbers"], "concepts",
                              f"tree numbers disagree with MeSH for {record.get('name')!r}")
                report.expect(record.get("parents") == [
                    (f"mesh:{parent}" if len([x for x in records if x.get("mesh_ui") == parent]) != 1 else
                     next(x["concept_id"] for x in records if x.get("mesh_ui") == parent))
                    for parent in concepts.mesh.parent_descriptors(mesh_ui)], "concepts",
                    f"parent descriptors disagree with MeSH for {record.get('name')!r}")
        else:
            report.expect(record.get("status") in ("ai_proposed", "human_confirmed"), "concepts",
                          f"status for {record.get('name')!r}")
            report.expect(record.get("mesh_tree_numbers") == [] and record.get("parents") == [], "concepts",
                          f"unmapped MeSH fields must be empty for {record.get('name')!r}")
            report.expect(len(mesh_match) != 1, "concepts",
                          f"unambiguous exact MeSH match was not recorded for {record.get('name')!r}")
    known_ids = set(ids)
    for record in records:
        for parent in record.get("parents", []):
            if parent.startswith("mesh:"):
                report.expect(concepts.mesh.descriptor(parent[5:]) is not None, "concepts",
                              f"unknown parent MeSH descriptor {parent!r}")
            else:
                report.expect(parent in known_ids, "concepts", f"unknown parent concept id {parent!r}")

    registry = concepts.Registry(records)

    def registry_record_for_note(folder, stem):
        record = registry.lookup(stem)
        if record:
            return record
        # Note filenames are sanitized for Windows/Obsidian, while the
        # canonical display name may retain a slash (or another invalid
        # filename character).  Resolve that reversible filename mapping
        # before checking displayed machine stubs.
        for candidate in registry.records:
            expected_folder = notes.TERM_FOLDERS[concepts.TYPE_KEYS[candidate["type"]]]
            if expected_folder == folder and notes.safe_filename(candidate["name"]).casefold() == stem.casefold():
                return candidate
        return None
    paper_ids = [r["paper_id"] for r in con.execute("SELECT paper_id FROM papers")]
    paper_sentences = {
        paper_id: [s for s in db.read_jsonl(db.paper_dir(paper_id) / "sentences.jsonl")
                   if s.get("section") != "references" and (s.get("kind") == "table_row" or not s.get("in_table"))]
        for paper_id in paper_ids
    }
    _pattern_matches = concepts._pattern_matches
    expected_links = {}
    # Same matcher and eligible sentences as concepts._link_stream (the linker under test is the
    # authority on WHAT a match is; this check re-derives every link independently of the FTS pruning).
    patterns_by_id = concepts._compiled_patterns(registry)
    link_rows = {(r["concept_id"], r["paper_id"]): r for r in con.execute("SELECT * FROM concept_links")}
    blobs = {pid: "\n".join(s.get("text") or "" for s in rows) for pid, rows in paper_sentences.items()}
    for record in records:
        patterns = patterns_by_id[record["concept_id"]]
        for paper_id in paper_ids:
            # Pre-filter with the linker's own patterns on the whole text (a superset of the per-sentence matches).
            matched = [] if not any(pattern.search(blobs[paper_id]) for pattern in patterns) else [
                s for s in paper_sentences[paper_id]
                if any(_pattern_matches(s.get("text") or "", pattern, record) for pattern in patterns)]
            row = link_rows.get((record["concept_id"], paper_id))
            key = (record["concept_id"], paper_id)
            if matched:
                matched.sort(key=lambda s: s.get("seq", 0))
                expected_links[key] = {
                    "n_sentences": len(matched),
                    "sections": sorted({s.get("section") for s in matched}),
                    "first_sentence_id": matched[0].get("sentence_id"),
                }
            report.expect(bool(matched) == bool(row), "concept_links",
                          f"relink_paper completeness for {record['name']} / {paper_id}")
            if row:
                expected = expected_links.get(key)
                report.expect(expected is not None and row["n_sentences"] == expected["n_sentences"],
                              "concept_links", f"sentence count after relink_paper for {record['name']} / {paper_id}")
                try:
                    sections = json.loads(row["sections"] or "[]")
                except (TypeError, ValueError):
                    sections = None
                report.expect(expected is not None and sections == expected["sections"], "concept_links",
                              f"sections after relink_paper for {record['name']} / {paper_id}")
                report.expect(expected is not None and row["first_sentence_id"] == expected["first_sentence_id"],
                              "concept_links", f"first sentence after relink_paper for {record['name']} / {paper_id}")

    actual_links = {(row["concept_id"], row["paper_id"])
                    for row in con.execute("SELECT concept_id, paper_id FROM concept_links")}
    for key in sorted(actual_links - set(expected_links)):
        report.error("concept_links", f"stale row after relink_paper: {key[0]} / {key[1]}")

    for vault in sorted({r[0] for r in con.execute("SELECT DISTINCT vault FROM paper_vaults")}):
        vdir = config.vault_dir(vault)
        graph_data = graph.build(con, vault)
        graph_nodes = {n["id"] for n in graph_data["nodes"]} if graph_data else set()
        report.expect(graph_data is not None and len(graph_nodes) == len(graph_data["nodes"]),
                      f"{vault}/graph", "graph nodes exist and IDs are unique")
        report.expect(bool(graph_data) and all(e["source"] in graph_nodes and e["target"] in graph_nodes
                                                for e in graph_data["edges"]),
                      f"{vault}/graph", "graph edges resolve to returned nodes")
        report.expect(bool(graph_data) and all(e["kind"] in ("paper_concept", "citation", "concept_parent",
                                                              "relation", "discovered_citation")
                                                for e in graph_data["edges"]),
                      f"{vault}/graph", "graph edge kinds follow the contract")
        note_paths = list(vdir.rglob("*.md"))
        names_on_disk = {p.stem.lower() for p in note_paths}
        relevant_ids = {row[0] for row in con.execute(
            "SELECT DISTINCT concept_id FROM concept_links WHERE paper_id IN "
            "(SELECT paper_id FROM paper_vaults WHERE vault = ?)", (vault,))}
        # A1: registry terms must have a canonical machine note in the registry's
        # type folder.  Also catch pure stubs that were left in the wrong folder or
        # with a lower-/upper-case filename.  Human-edited notes are preserved and
        # reported as warnings rather than treated as disposable machine data.
        for record in records:
            if record["concept_id"] not in relevant_ids:
                continue
            _, links, units = concept_notes._collect(con, record, registry, vault)
            if not concept_notes.shown_in_vault(links, units):
                continue  # below the vault threshold (concept_notes.shown_in_vault): no note is expected
            folder = notes.TERM_FOLDERS[concepts.TYPE_KEYS[record["type"]]]
            expected_name = notes.safe_filename(record["name"])
            expected_path = vdir / folder / f"{expected_name}.md"
            if not expected_path.is_file():
                human_candidates = []
                for path in note_paths:
                    if path.stem.casefold() != expected_name.casefold() or path.parent == vdir / folder:
                        continue
                    text = path.read_text(encoding="utf-8", errors="replace")
                    if not (notes.STUB_MARK in text and notes._is_pure_stub(path.parent.relative_to(vdir).as_posix(), path.stem, text)):
                        human_candidates.append(path)
                if human_candidates:
                    report.warn(f"{vault}/concepts", f"human note kept outside canonical location for {record['name']!r}")
                else:
                    report.error(f"{vault}/concepts", f"missing canonical note {folder}/{expected_name}.md")
            elif con.execute("SELECT 1 FROM concept_links WHERE concept_id = ? AND paper_id IN "
                             "(SELECT paper_id FROM paper_vaults WHERE vault = ?) LIMIT 1",
                             (record["concept_id"], vault)).fetchone():
                note_text = expected_path.read_text(encoding="utf-8", errors="replace")
                if "<!-- kb:concept-fingerprint:" not in note_text:
                    if notes.STUB_MARK in note_text and notes._is_pure_stub(folder, expected_name, note_text):
                        report.error(f"{vault}/concept_note", f"linked concept has no generated note block: {record['name']}")
                    else:
                        report.warn(f"{vault}/concept_note", f"human-managed note protected without auto block: {record['name']}")

        for folder in notes.NODE_FOLDERS:
            for path in (vdir / folder).glob("*.md") if (vdir / folder).exists() else []:
                if path.stem == notes.LIST_NOTES.get(folder):
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                if notes.STUB_MARK not in text or not notes._is_pure_stub(folder, path.stem, text):
                    continue
                record = registry_record_for_note(folder, path.stem)
                if record:
                    expected_folder = notes.TERM_FOLDERS[concepts.TYPE_KEYS[record["type"]]]
                    expected_name = notes.safe_filename(record["name"])
                    report.expect(folder == expected_folder and path.stem == expected_name,
                                  f"{vault}/concepts", f"pure stub is not canonical: {path.relative_to(vdir)}")

        displayed_ids = set()
        for paper in (vdir / "Papers").glob("*.md") if (vdir / "Papers").exists() else []:
            paper_text = paper.read_text(encoding="utf-8", errors="replace")
            if _is_kb_index(paper):
                continue
            for target in notes.WIKI_LINK.findall(paper_text):
                target = target.strip()
                if Path(target).suffix.lower() in ATTACHMENT_SUFFIXES:
                    # Obsidian resolves a link/embed of a non-note file by vault-relative path.
                    report.expect((vdir / target).is_file(), f"{vault}/{paper.name}",
                                  f"paper link [[{target}]] must resolve to an existing attachment file")
                    continue
                target_name = target.rsplit("/", 1)[-1].removesuffix(".md")
                record = registry.lookup(target_name)
                if not record:
                    record = next((candidate for candidate in registry.records
                                   if notes.safe_filename(candidate["name"]).casefold() == target_name.casefold()),
                                  None)
                if record:
                    displayed_ids.add(record["concept_id"])
                report.expect(target_name.casefold() in names_on_disk, f"{vault}/{paper.name}",
                              f"paper link [[{target}]] must resolve to an existing note")

        # A2: every pure machine concept stub is displayed by at least one paper
        # note.  This guards against creating stubs for below-threshold links.
        for folder in notes.NODE_FOLDERS:
            for path in (vdir / folder).glob("*.md") if (vdir / folder).exists() else []:
                if path.stem == notes.LIST_NOTES.get(folder):
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                if notes.STUB_MARK not in text or not notes._is_pure_stub(folder, path.stem, text):
                    continue
                record = registry_record_for_note(folder, path.stem)
                report.expect(record is not None and record["concept_id"] in displayed_ids,
                              f"{vault}/concepts", f"machine stub is not displayed by a paper: {path.relative_to(vdir)}")

        known_units = {u["unit_id"] for pid in paper_ids
                       if con.execute("SELECT 1 FROM paper_vaults WHERE paper_id = ? AND vault = ?", (pid, vault)).fetchone()
                       for u in db.read_jsonl(db.paper_dir(pid) / "evidence.jsonl")}
        for folder in notes.NODE_FOLDERS:
            directory = vdir / folder
            for path in directory.glob("*.md") if directory.exists() else []:
                text = path.read_text(encoding="utf-8", errors="replace")
                if "<!-- kb:concept-fingerprint:" not in text:
                    continue
                starts = text.count(notes.AUTO_START)
                ends = text.count(notes.AUTO_END)
                fingerprint = re.findall(r"(?m)^<!-- kb:concept-fingerprint:([0-9a-f]{40}) -->$", text)
                report.expect(starts == 1 and ends == 1 and len(fingerprint) == 1,
                              f"{vault}/concept_note", f"invalid auto block/fingerprint in {path.name}")
                block = text[text.find(notes.AUTO_START):text.find(notes.AUTO_END)]
                # Node sections follow concept_notes.NODE_SECTIONS (a section without data is hidden, so only
                # unapproved or misordered headings are errors); topic hubs follow _render_moc.
                if directory.name == notes.MAPS or "## Overview" in block:
                    report.expect("## Overview" in block, f"{vault}/concept_note",
                                  f"missing section ## Overview in {path.name}")
                    for problem in concept_notes.heading_problems(text, "moc"):
                        report.error(f"{vault}/concept_note", f"{problem} in {path.name}")
                else:
                    for problem in concept_notes.heading_problems(text):
                        report.error(f"{vault}/concept_note", f"{problem} in {path.name}")
                for unit_id in re.findall(r"[a-zA-Z0-9_-]+:E\d{4,}", block):
                    report.expect(unit_id in known_units, f"{vault}/concept_note",
                                  f"citation points to missing evidence unit {unit_id} in {path.name}")

        # N3b: sentence / evidence / object IDs belong only in the collapsed "Source IDs" callout, never in a
        # heading or in the reading text of a generated note.
        stems_by_fold = {}
        for path in note_paths:
            stems_by_fold.setdefault(path.stem.casefold(), set()).add(path.stem)
        for folder in (*notes.NODE_FOLDERS, "Papers", "Results", "Claims"):
            directory = vdir / folder
            for path in directory.glob("*.md") if directory.exists() else []:
                text = path.read_text(encoding="utf-8", errors="replace")
                if notes.AUTO_START not in text:
                    continue
                for problem in note_text_problems(text, stems_by_fold, check_placeholders=folder != "Claims"):
                    report.error(f"{vault}/note_text", f"{problem} in {folder}/{path.name}")
                visible = presentation.machine_ids(text)
                report.expect(not visible, f"{vault}/machine_ids",
                              f"machine IDs visible outside the Source IDs callout in {folder}/{path.name}: {visible[:3]}")
                if folder in ("Results", "Claims"):  # N3c: approved EVD / CLM / conflict section sets
                    from . import result_notes
                    for problem in result_notes.heading_problems(text):
                        report.error(f"{vault}/result_note", f"{problem} in {folder}/{path.name}")

MOJIBAKE = ("�", "Ã", "â€", "Î²", "Î±", "Î¼", "Â±", "Âµ")
PLACEHOLDERS = re.compile(r"chưa tìm thấy|not found in (?:the )?(?:current )?(?:vault|knowledge base)|synthesis pending", re.I)


def note_text_problems(text, stems_by_fold=None, check_placeholders=True):
    """N3e: reading-quality problems in a generated note (the auto block); [] when clean.

    ``stems_by_fold`` maps casefolded note title -> set of exact titles in the vault; a link whose target is only
    a case variant of an existing title is reported (Obsidian resolves it, exports and other tools do not).
    """
    start, end = text.find(notes.AUTO_START), text.find(notes.AUTO_END)
    block = text[start:end] if 0 <= start < end else text
    problems = [f"broken characters {m!r}" for m in MOJIBAKE if m in block]
    lines = [line.strip() for line in block.splitlines() if line.strip() and not line.strip().startswith("<!--")]
    for i, line in enumerate(lines):
        level = len(line) - len(line.lstrip("#"))
        following = re.match(r"(#{1,6}) ", lines[i + 1]) if i + 1 < len(lines) else None
        if re.match(r"#{2,6} ", line) and (i + 1 == len(lines) or following and len(following.group(1)) <= level):
            problems.append(f"empty section {line!r}")
    if check_placeholders:
        problems += [f"placeholder text {m.group(0)!r}" for m in PLACEHOLDERS.finditer(block)][:3]
    for target in notes.WIKI_LINK.findall(block):
        name = target.strip().rsplit("/", 1)[-1].removesuffix(".md")
        exact = (stems_by_fold or {}).get(name.casefold())
        if exact and name not in exact:
            problems.append(f"link [[{name}]] differs from the note title {sorted(exact)[0]!r} only by case")
    return problems


def _is_kb_index(path):
    """Return true only for the generated Papers/Papers.md index note."""
    if path.parent.name != "Papers" or path.stem.casefold() != "papers":
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return bool(re.search(r"(?im)^tags:\s*\[[^\]]*\bkb-index\b", text))


def run(con, paper_id=None, unit=True, strict_conform=False):
    report = Report()
    if unit:
        unit_checks(report)
    papers = [paper_id] if paper_id else sorted(p.name for p in config.PAPERS_DIR.iterdir()
                                                 if (p / "metadata.json").exists()) if config.PAPERS_DIR.exists() else []
    for pid in papers:
        check_paper(report, con, pid)
    check_concepts(report, con)
    check_ghosts(report, con)
    check_relations(report)
    from . import conform
    conform.selftest_section(report, con, paper_id, strict_conform)
    return report, len(papers)


def check_ghosts(report, con):
    """Check file, database and aggregate registry parity for unresolved references."""
    papers = {r["paper_id"]: list(db.read_jsonl(db.paper_dir(r["paper_id"]) / "references.jsonl"))
              for r in con.execute("SELECT paper_id FROM papers ORDER BY paper_id")}
    path = config.STORE / "ghosts.jsonl"
    records = list(db.read_jsonl(path)) if path.exists() else []
    if not path.exists():
        report.warn("ghosts", "registry missing (run `kb.py rebuild-ghosts`)")
        return
    ghosts.validate_registry(report, papers, records)
    for paper_id, refs in papers.items():
        for ref in refs:
            row = con.execute("SELECT ghost_id FROM references_list WHERE ref_id = ?", (ref["ref_id"],)).fetchone()
            report.expect(row is not None and row["ghost_id"] == ref.get("ghost_id"), "ghosts",
                          f"SQLite ghost_id differs for {ref['ref_id']}")
