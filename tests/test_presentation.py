"""Presentation regressions, entirely isolated from the live library."""
import json
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch
import yaml
from kbsys import config, db, notes, concept_notes, concepts, presentation, graph, answer, neural, node_profile

class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = patch.multiple(config, ROOT=self.root, STORE=self.root / 'store',
                                    PAPERS_DIR=self.root / 'store/papers', VAULTS_DIR=self.root / 'vaults')
        self.patch.start()
        self.unit = {'paper_id':'p', 'unit_id':'p:E1', 'seq':1, 'unit_type':'result', 'status':'ok',
                     'quote':'EG was added and EG concentration was measured.', 'statement':'EG concentration was measured.',
                     'intervention':{'agent':'EG'}, 'outcome':{'measure':'EG concentration'},
                     'pages':[2], 'sections':['results'], 'sentence_ids':['p:S1'],
                     'paper':{'paper_id':'p','title':'Fixture','note_name':'Fixture','evidence_type':'primary_research'}}
        self.record = {'concept_id':'eg', 'name':'EG','type':'molecule','synonyms':[], 'parents':[]}

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_short_acronym_source_roles(self):
        m = presentation.metadata_for_term('EG','molecule',[self.unit])
        self.assertEqual(m['entity_kind'],'chemical')
        self.assertEqual(m['roles'],['intervention','measured_marker'])
        for text in ('epigenetics integrates results', 'negative eggs', 'VEGF'):
            u = {**self.unit,'quote':text}
            self.assertEqual(presentation.roles_for_unit('EG',u),[])
        self.assertFalse(presentation.matches_term('ongoing','GO'))
        self.assertEqual(presentation.roles_for_unit('Vitrification', self.unit), [])

    def test_classes_and_roles_independent(self):
        c = presentation.metadata_for_term('CPA','molecule')
        h = presentation.metadata_for_term('Histones','molecule')
        self.assertEqual(c['entity_kind'],'chemical_class')
        self.assertEqual(h['entity_kind'],'biological_entity')
        self.assertNotEqual(c['color'], h['color'])
        self.assertEqual(c['roles'],[])
        self.assertIn('not a scientific definition', c['category_basis'])
        self.assertEqual(presentation.metadata_for_term('Assay','method')['entity_kind'],'method')

    def test_honest_cards_and_exact_summary(self):
        c = presentation.evidence_card(self.unit)
        self.assertEqual(c['model'],'not annotated')
        self.assertEqual(c['quote'],self.unit['quote'])
        self.assertEqual(presentation.note_summary([self.unit])[0]['statement'], self.unit['statement'])
        self.assertEqual(presentation.note_summary([{**self.unit,'status':'flagged'}]),[])
        figure = {**self.unit,'outcome':{'values':[42],'value_source':'figure_only'}}
        self.assertNotIn('42', presentation.evidence_card(figure)['outcome'])

    def test_human_content_and_frontmatter_preserved(self):
        path = config.vault_dir('one') / 'Substances/EG.md'
        path.parent.mkdir(parents=True)
        path.write_text('---\ntags: [mine]\nidea: keep\n---\n# EG\nMy hypothesis.\n'+notes.AUTO_START+'\nold\n'+notes.AUTO_END+'\nMy observations.\n',encoding='utf-8')
        block = notes.AUTO_START+'\nnew\n'+notes.AUTO_END+'\n'
        self.assertEqual(concept_notes._write_note('one',self.record,block,'abc',[self.unit]),'updated')
        text=path.read_text(encoding='utf-8')
        fm,rest=notes._split_frontmatter(text)
        self.assertEqual(fm['tags'],['mine'])
        self.assertEqual(fm['idea'],'keep')
        self.assertEqual(fm['kb_kind'],'chemical')
        self.assertEqual(fm['kb_roles'],['intervention','measured_marker'])
        self.assertEqual(fm['source_papers'],['p'])
        self.assertIn('My hypothesis.', rest)
        self.assertIn('My observations.', rest)
        self.assertEqual(concept_notes._write_note('one',self.record,block,'abc',[self.unit]),'unchanged')
        self.assertTrue(list((config.STORE/'concept_history').rglob('EG.md')))

    def test_unmarked_human_note_protected(self):
        path=config.vault_dir('one')/'Substances/EG.md'
        path.parent.mkdir(parents=True)
        path.write_text('Human research note.',encoding='utf-8')
        self.assertEqual(concept_notes._write_note('one',self.record,'replacement','abc'),'protected')
        self.assertEqual(path.read_text(encoding='utf-8'),'Human research note.')

    def test_graph_groups_and_user_filter_preserved(self):
        path=config.vault_dir('one')/'.obsidian/graph.json'
        path.parent.mkdir(parents=True)
        group={'query':'tag:#my-research','color':{'a':1,'rgb':123}}
        path.write_text(json.dumps({'colorGroups':[group],'search':'tag:#my-research','zoom':3}),encoding='utf-8')
        notes.write_graph_settings('one')
        settings=json.loads(path.read_text(encoding='utf-8'))
        self.assertIn(group,settings['colorGroups'])
        self.assertEqual(settings['search'],'tag:#my-research')
        self.assertEqual(settings['zoom'],3)
        self.assertTrue(any(g['query']=='path:"Substances/"' for g in settings['colorGroups']))

    def test_paper_results_and_object_navigation(self):
        folder=db.paper_dir('p')
        folder.mkdir(parents=True)
        db.write_json(folder/'metadata.json',self.unit['paper'])
        db.write_jsonl(folder/'evidence.jsonl',[self.unit])
        db.write_jsonl(folder/'sentences.jsonl',[{'sentence_id':'p:S2','text':'An exact figure caption.'}])
        db.write_jsonl(folder/'figures.jsonl',[{'figure_id':'p:F1','label':'Figure 1','source':'main','page':3,'caption_sentence_ids':['p:S2']}])
        registry=concepts.Registry([])
        class Con:
            def execute(self,*args): return []
        out='\n'.join(notes._render_v2(self.unit['paper'], {'classification':'CORE','version':2},Con(),registry,lambda t: True))
        self.assertIn('## Kết quả (Results)',out)
        self.assertIn('## Hình và bảng (Figures and tables)',out)
        self.assertIn('An exact figure caption.',out)
        self.assertIn('### Figure 1 — An exact figure caption',out)
        self.assertNotIn('p:F1',out)
        self.assertEqual(presentation.machine_ids(out),[])
        self.assertIn('EG concentration was measured.',out)

    def test_concept_text_tags_do_not_match_substrings(self):
        registry=concepts.Registry([self.record])
        ids={'eg'}
        self.assertEqual(concept_notes._text_concept_ids({'quote':'epigenetics integrates'},ids,registry),set())
        self.assertEqual(concept_notes._text_concept_ids({'quote':'EG was used'},ids,registry),ids)

    def test_tag_only_evidence_is_not_collected(self):
        con=sqlite3.connect(':memory:')
        con.row_factory=sqlite3.Row
        con.executescript('CREATE TABLE concept_links(concept_id,paper_id,level,n_sentences,sections,first_sentence_id); CREATE TABLE paper_vaults(paper_id,vault); CREATE TABLE paper_terms(paper_id,term);')
        con.execute("INSERT INTO concept_links VALUES('eg','p','studied',1,'results','p:S1')")
        con.execute("INSERT INTO paper_vaults VALUES('p','one')")
        con.execute("INSERT INTO paper_terms VALUES('p','EG')")
        folder=db.paper_dir('p')
        folder.mkdir(parents=True)
        db.write_jsonl(folder/'sentences.jsonl',[{'sentence_id':'p:S1','text':'Epigenetics integrates results.','section':'results','kind':'text'}])
        db.write_jsonl(folder/'evidence.jsonl',[{**self.unit,'quote':'Epigenetics integrates results.','terms':{'molecules':['EG']}}])
        collected=concept_notes._collect(con,self.record,concepts.Registry([self.record]),'one')
        self.assertEqual(collected[2],[])
        con.close()

    def test_stub_filter_and_reversible_machine_migration(self):
        path=config.vault_dir('one')/'Substances/EG.md'
        path.parent.mkdir(parents=True)
        (config.vault_dir('one')/'Home.md').write_text('# Home\n',encoding='utf-8')  # a research vault
        old=notes._stub_template('Substances','EG')
        path.write_text(old,encoding='utf-8')
        class Con:
            def execute(self,*args): return self
            def fetchall(self): return []
        notes.write_indexes(Con(),'one')
        text=path.read_text(encoding='utf-8')
        self.assertIn('kb_stub: true',text)
        self.assertTrue(notes._is_pure_stub('Substances','EG',text))
        self.assertNotIn('[[EG]]',(path.parent/'Substances.md').read_text(encoding='utf-8'))
        backups=list((config.STORE/'stub_history').rglob('EG.md'))
        self.assertEqual(backups[0].read_text(encoding='utf-8'),old)

    def test_short_acronym_does_not_match_lowercase_example_word(self):
        self.assertFalse(presentation.matches_term('eg a general example','EG'))
        self.assertTrue(presentation.matches_term('EG was used','EG'))
        self.assertTrue(presentation.matches_term('ethylene glycol was used','EG',['Ethylene glycol']))
        registry=concepts.Registry([self.record])
        self.assertEqual(concept_notes._text_concept_ids({'quote':'eg a general example'},{'eg'},registry),set())

    def test_unicode_prefix_is_not_a_separate_histone_token(self):
        pattern=concepts._regex('H2A',True)
        self.assertIsNone(pattern.search('γH2A.X increased'))
        self.assertIsNotNone(pattern.search('H2A was measured'))

    def test_graph_exposes_source_roles_and_category_color(self):
        con=sqlite3.connect(':memory:')
        con.row_factory=sqlite3.Row
        con.executescript('CREATE TABLE papers(paper_id,doi,title,first_author,year,evidence_type,note_name,publication_types); CREATE TABLE paper_vaults(paper_id,vault); CREATE TABLE concept_links(concept_id,paper_id,level,n_sentences,sections,first_sentence_id); CREATE TABLE references_list(ref_id,paper_id,resolved_paper_id);')
        con.execute("INSERT INTO papers VALUES('p',NULL,'Fixture','Test',2026,'primary_research','Fixture','[]')")
        con.execute("INSERT INTO paper_vaults VALUES('p','one')")
        con.execute("INSERT INTO concept_links VALUES('eg','p','studied',1,'results','p:S1')")
        db.write_jsonl(config.STORE/'concepts.jsonl',[self.record])
        db.write_jsonl(db.paper_dir('p')/'evidence.jsonl',[self.unit])
        with patch('kbsys.discovery.iter_cached',return_value=()):
            result=graph.build(con,'one')
        node=next(n for n in result['nodes'] if n['id']=='concept:eg')
        self.assertEqual(node['entity_kind'],'chemical')
        self.assertEqual(node['roles'],['intervention','measured_marker'])
        self.assertEqual(node['color'],presentation.COLORS['chemical'])
        self.assertNotIn('flags',node)
        con.close()


class PresentationFixTests(unittest.TestCase):
    def test_joined_lowercase_fragment_is_not_prefixed(self):
        rows = [
            {"sentence_id": "p:S0001", "seq": 1, "text": "among the three", "section": "results", "kind": "text"},
            {"sentence_id": "p:S0002", "seq": 2, "text": "groups were compared.", "section": "results", "kind": "text"},
        ]
        rows[0]["continues"] = "p:S0002"
        with patch.object(answer, "read_jsonl", return_value=rows):
            self.assertEqual(answer._whole_quote({"id": "p:S0001", "paper_id": "p", "text": rows[0]["text"]}, {}),
                             "among the three groups were compared.")

    def test_vault_filter_and_callout_snippet(self):
        self.assertTrue(config.is_research_vault_name("topic_a"))
        for name in ("Registry", "_Inbox", "_Maps", ".hidden"):
            self.assertFalse(config.is_research_vault_name(name))
        meta = neural._note_metadata(
            "---\nhas_conflict: true\n---\n> [!conflict] Open source conflict\n\nA source-backed summary.",
            "Concepts/H3K9me3.md")
        self.assertTrue(meta["has_conflict"])
        self.assertEqual(meta["summary"], "A source-backed summary.")

    def test_protocol_table_is_unfolded_and_marks_differences(self):
        ref = lambda sid, text: {"sentence_id": sid, "text": text}
        detail = lambda values: {"items": [{"value": v, "sources": [ref("p:S1", v)]} for v in values]}
        profile = {"papers": [
            {"paper_id": "p1", "cite": "Lee 2025", "protocol_variants": [{
                "name": "vitrification", "details": {"chemical": detail(["EG 10%"]), "temperature": detail(["-196 C"]), "equipment": detail(["straw"])},
                "steps": [ref("p:S1", "load and cool")], "sources": []}]},
            {"paper_id": "p2", "cite": "Smith 2026", "protocol_variants": [{
                "name": "vitrification", "details": {"chemical": detail(["EG 15%"]), "temperature": {"items": []}, "equipment": detail(["cryovial"])},
                "steps": [ref("p:S2", "equilibrate and cool")], "sources": []}]},
        ]}
        table = node_profile.protocol_variants_table(profile)
        text = "\n".join(table)
        self.assertIn("chemicals and concentrations", text)
        self.assertIn("[differs across papers]", text)
        self.assertNotIn("<details>", text)
        self.assertIn("New evidence from other papers (1)", "\n".join(node_profile.card_currency_notice({
            "card_currency": "current_with_new_evidence", "new_paper_ids": ["p2"]})))


if __name__=='__main__': unittest.main()
