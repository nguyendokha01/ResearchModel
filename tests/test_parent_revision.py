"""Source/provenance integration checks. All library writes use isolated fixtures."""
import json
import sqlite3
import unittest
from unittest.mock import patch
from pathlib import Path
from kbsys import config, db, node_profile, concept_notes, concepts, neural, notes, media, structure, result_notes
from . import test_node_profile as npfixtures
from . import test_media as mfixtures
from . import test_graph_research as gfixtures

class ParentNodeTests(unittest.TestCase):
    setUp=npfixtures.NodeProfileTests.setUp
    tearDown=npfixtures.NodeProfileTests.tearDown
    write_paper=npfixtures.NodeProfileTests.write_paper
    build=npfixtures.NodeProfileTests.build

    def test_continues_in_markdown_keeps_complete_result_and_status(self):
        self.rows[-1]['continues']='p:S6'
        self.rows.append({**self.rows[-1],'sentence_id':'p:S6','seq':6,'text':'and fertility was not measured.', 'continues':None})
        self.write_paper('p',self.rows)
        original=(db.paper_dir('p')/'evidence.jsonl').read_bytes()
        profile=self.build(persist=False)
        r=profile['papers'][0]['results'][0]
        self.assertEqual(r['source_status'],'resolved')
        self.assertEqual([s['sentence_id'] for s in r['sources']],['p:S5','p:S6'])
        self.assertIn('fertility was not measured','\n'.join(node_profile.render(profile)))
        self.assertEqual((db.paper_dir('p')/'evidence.jsonl').read_bytes(),original)

    def test_invalid_protocol_never_exposes_structured_assignment(self):
        npfixtures.NodeProfileTests.protocol(self,"5 mg")
        profile=self.build(persist=False)['papers'][0]
        self.assertEqual(profile['protocol_profile_status'],'missing_or_stale')
        self.assertIn('inventory',profile['protocol_variants'][0]['basis'])
        self.assertEqual(profile['protocol_variants'][0]['details']['equipment']['items'],[])

    def test_review_derivative_primary_false_stored_annotation_preserved(self):
        meta=db.read_json(db.paper_dir('p')/'metadata.json');meta['publication_types']=['Review']
        db.write_json(db.paper_dir('p')/'metadata.json',meta)
        self.unit['quality']={'is_primary':True}
        result=self.build(persist=False)['papers'][0]['results'][0]
        self.assertFalse(result['quality']['is_primary'])
        self.assertTrue(result['stored_quality']['is_primary'])

    def test_one_endpoint_conflict_both_sources_and_scope(self):
        qrows=[{**self.rows[-1],'sentence_id':'q:S5','text':'A different finding with no matching node term.'}]
        self.write_paper('q',qrows)
        db.write_jsonl(db.paper_dir('q')/'evidence.jsonl',[{**self.unit,'paper_id':'q','unit_id':'q:E0002','sentence_ids':['q:S5']}])
        relation={'relation_id':'r1','from_unit':'p:E1','to_unit':'q:E0002','type':'contradicts','status':'ai_proposed','vault':'one'}
        db.write_jsonl(config.STORE/'relations.jsonl',[relation])
        con=sqlite3.connect(':memory:');con.execute('CREATE TABLE paper_vaults(paper_id,vault)')
        con.executemany('INSERT INTO paper_vaults VALUES (?,?)',[('p','one'),('q','one')])
        p=node_profile.build(self.record,'one',['p'],[self.unit],persist=False,con=con)
        with patch.object(result_notes.media, 'bundles', return_value=[]), \
                patch.object(result_notes.concepts, 'load_registry', return_value=[]):  # this fake db has no paper_terms
            result_notes.refresh(con, 'one')
        conflict = next((config.vault_dir('one') / 'Claims').glob('CONFLICT - *.md'))
        source_note = conflict.read_text(encoding='utf-8')
        out='\n'.join(node_profile.conflict_callout(p))
        self.assertIn(f'[[{conflict.stem}]]', out)
        self.assertNotIn('different finding', out)
        self.assertIn('p:S5', source_note);self.assertIn('q:S5', source_note)
        self.assertIn(self.rows[-1]['text'], source_note)
        self.assertIn(qrows[0]['text'], source_note)
        con.execute('DELETE FROM paper_vaults WHERE paper_id="q"')
        self.assertEqual(node_profile.build(self.record,'one',['p'],[self.unit],persist=False,con=con)['conflicts'],[])
        con.close()

    def test_source_tag_filter_preserves_raw_analysis(self):
        registry=concepts.Registry([self.record])
        analysis={'molecules':['EG']}
        self.write_paper('p',[{**self.rows[0],'text':'Epigenetics is measured.'}])
        self.assertEqual(notes._canonical_analysis_terms(analysis,registry,'p')['molecules'],[])
        self.assertEqual(analysis['molecules'],['EG'])

    def test_cpa_plural_and_wrong_substrings(self):
        from kbsys import presentation
        self.assertTrue(presentation.matches_term('Cryoprotective agents (CPAs) are used.','CPA'))
        self.assertFalse(presentation.matches_term('epigenetics and EGFR','EG'))
        self.assertNotEqual(presentation.COLORS['chemical'],presentation.COLORS['chemical_class'])

class ParentMediaTests(unittest.TestCase):
    setUp=mfixtures.MediaTests.setUp
    tearDown=mfixtures.MediaTests.tearDown
    make_pdf=staticmethod(mfixtures.MediaTests.make_pdf)

    def test_bad_legacy_image_with_missing_pdf_is_diagnostic_not_error(self):
        self.pdf.unlink()
        image=self.folder/'crops'/'legacy.png';image.parent.mkdir();image.write_bytes(b'not an image')
        result=media._old_crop(self.folder,{},'crops/legacy.png')
        self.assertEqual(result['status'],'corrupt')
        self.assertEqual(image.read_bytes(),b'not an image')

    def test_manual_override_cannot_move_existing_object_to_other_source(self):
        media.build('paper',detect=False)
        prior=(self.folder/'media_manifest.json').read_bytes()
        meta=db.read_json(self.folder/'metadata.json')
        meta['source_files'].append({**meta['source_files'][0],'role':'supplementary','supp_id':'supp1'})
        db.write_json(self.folder/'metadata.json',meta)
        with self.assertRaisesRegex(ValueError,'existing object source'):
            media.build('paper',detect=False,regions=[{'kind':'figure','object_id':'paper:F1','source':'supp1','page':1,'bbox':[0,0,100,100]}])
        self.assertEqual((self.folder/'media_manifest.json').read_bytes(),prior)

class ParentReaderTests(unittest.TestCase):
    setUp=gfixtures.GraphResearchTests.setUp
    tearDown=gfixtures.GraphResearchTests.tearDown
    result=gfixtures.GraphResearchTests.result

    def test_non_ai_fast_deep_different_context_preserves_continues(self):
        unit=self.result()
        for i in range(7,20):
            self.rows.append({**self.rows[0],'sentence_id':self.pid+f':S{i:04d}','seq':i,'continues':None,'text':f'H3K9me3 context sentence {i}.'})
        db.write_jsonl(self.folder/'sentences.jsonl',self.rows);db.reload_paper(self.con,self.pid);self.con.commit()
        fast=neural.resolve_sources(self.con,'one',[unit['unit_id']],24,context_limit=4)
        deep=neural.resolve_sources(self.con,'one',[unit['unit_id']],45,context_limit=12)
        self.assertGreater(len(deep['items']),len(fast['items']))
        self.assertIn(self.rows[1]['sentence_id'],[s['id'] for s in fast['items']])
        self.assertEqual(fast['context_limit'],4);self.assertEqual(deep['context_limit'],12)

    def test_empty_legacy_tags_and_wrong_stored_quote_do_not_link_node(self):
        self.result()
        record={'concept_id':'eg','name':'EG','type':'molecule','synonyms':[],'parents':[]}
        self.con.execute('INSERT INTO paper_terms(paper_id,term,term_type) VALUES (?,?,?)',(self.pid,'EG','molecule'))
        _,links,units=concept_notes._collect(self.con,record,concepts.Registry([record]),'one')
        self.assertEqual(links,{});self.assertEqual(units,[])

if __name__=='__main__':unittest.main()
