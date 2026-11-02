"""D3.14 source-first retrieval, scoped media and graph regressions; isolated stores only."""
import json
import threading
import urllib.request
import urllib.error
import unittest
from unittest.mock import patch

from . import test_core as fixtures
from kbsys import config,db,neural,graph,media,workflow


class GraphResearchTests(unittest.TestCase):
    setUp=fixtures.Demo3Tests.setUp
    tearDown=fixtures.Demo3Tests.tearDown

    def result(self):
        self.rows[0].update(section='results',text='H3K9me3 was measured and',continues=self.rows[1]['sentence_id'])
        self.rows[1].update(section='results',text='was not significantly different (P > 0.05).')
        unit={'unit_id':self.pid+':E0001','paper_id':self.pid,'seq':1,'unit_type':'result','status':'flagged','statement':'Invented 999 percent causal claim.','quote':'Untrusted stored quote 999.','sentence_ids':[self.rows[0]['sentence_id']],'pages':[1],'sections':['results'],'model':{},'intervention':{},'outcome':{},'terms':{'molecules':['EG','H3K9me3']}}
        db.write_jsonl(self.folder/'sentences.jsonl',self.rows)
        db.write_jsonl(self.folder/'evidence.jsonl',[unit])
        db.write_json(self.folder/'evidence_aliases.json',{self.pid+':E0002':unit['unit_id']})
        db.reload_paper(self.con,self.pid);self.con.commit()
        return unit

    def test_alias_continues_and_original_numeric_context_not_model_quote(self):
        self.result()
        selected=neural.resolve_sources(self.con,'one',[self.pid+':E0002'])
        self.assertEqual([s['id'] for s in selected['items']], [r['sentence_id'] for r in self.rows[:2]])
        self.assertIn('0.05',selected['items'][1]['text'])
        self.assertTrue(selected['items'][1]['uncertainty'])
        self.assertNotIn('999',' '.join(s['text'] for s in selected['items']))
        self.assertEqual(selected['items'][0]['continues'],self.rows[1]['sentence_id'])

    def test_sources_are_current_vault_only_and_abstract_reference_only(self):
        with self.assertRaisesRegex(ValueError,'outside'):
            neural.resolve_sources(self.con,'two',[self.rows[0]['sentence_id']])
        self.rows[0]['section']='abstract'
        db.write_jsonl(self.folder/'sentences.jsonl',self.rows);db.reload_paper(self.con,self.pid)
        self.assertEqual(neural.resolve_sources(self.con,'one',[self.rows[0]['sentence_id']])['items'],[])

    def test_resolved_source_uses_publication_review_type_over_generated_primary(self):
        meta=db.read_json(self.folder/'metadata.json');meta['publication_types']=['Review']
        db.write_json(self.folder/'metadata.json',meta);db.reload_paper(self.con,self.pid);self.con.commit()
        out=neural.resolve_sources(self.con,'one',[self.rows[0]['sentence_id']])
        self.assertTrue(out['items'][0]['review_paper'])

    def test_note_context_merges_google_and_raw_annotation_is_untrusted(self):
        self.result()
        (config.vault_dir('one')/'H3K9me3.md').write_text('H3K9me3 '+self.pid+':E0002')
        google = {'hits': [{'paper_id': self.pid, 'sentence_ids': [self.rows[0]['sentence_id']]}]}
        with patch('kbsys.find.search', return_value=google) as route:
            prepared=neural.prepare(self.con,config.load(),'one','H3K9me3')
        route.assert_called_once()
        self.assertEqual(prepared['retrieval_basis'],'graph_context_plus_google_sentence_search')
        self.assertTrue(all(i['kind']=='sentence' for i in prepared['items']))
        self.assertNotIn('999',prepared['prompt'])
        self.assertIn(self.rows[0]['sentence_id'], [i['id'] for i in prepared['items']])
        self.assertTrue(prepared['papers'][prepared['items'][0]['paper_id']]['graph_context']['notes'])

    def test_note_short_acronyms_and_sql_pagination_not_all_bodies(self):
        vault=config.vault_dir('one')
        for name,text in [('EG','EG source'),('eg','eg example'),('Epigenetics','epigenetics')]:
            (vault/(name+'.md')).write_text(text)
        self.assertEqual([n['title'] for n in neural.search('one','EG')['nodes']],['EG'])
        for i in range(60):
            (vault/f'Node{i}.md').write_text('ROS [[Node'+str((i+1)%60)+']]')
        selected=neural.search('one','ROS',hops=2)
        self.assertLessEqual(len(selected['nodes']),50)
        self.assertLessEqual(len(selected['edges']),80)
        self.assertTrue(selected['truncated'])
        self.assertTrue(selected['has_more'])
        self.assertEqual(neural.search('one','ROS',offset=12)['offset'],12)

    def test_body_results_rank_ahead_of_introduction_and_method_intent(self):
        self.result()
        ids=[r['sentence_id'] for r in self.rows]
        selected=neural.resolve_sources(self.con,'one',ids,2,'H3K9me3')
        self.assertEqual([i['id'] for i in selected['items']],[r['sentence_id'] for r in self.rows[:2]])
        selected=neural.resolve_sources(self.con,'one',ids,2,'protocol dose')
        self.assertTrue(all(i['sections']==['methods'] for i in selected['items']))
        self.assertTrue(selected['truncated'])

    def test_stale_ticket_rejects_profile_region_and_note_changes(self):
        self.result()
        note=config.vault_dir('one')/'H3K9me3.md';note.write_text('H3K9me3 '+self.pid+':E0002')
        prepared=neural.prepare(self.con,config.load(),'one','H3K9me3')
        db.write_json(self.folder/'media_regions.json',[])
        with self.assertRaisesRegex(ValueError,'media/crop'):
            neural.check(self.con,prepared['ticket'],{'answer':[]})
        prepared=neural.prepare(self.con,config.load(),'one','H3K9me3')
        note.write_text(note.read_text()+' changed')
        with self.assertRaisesRegex(ValueError,'Note changed'):
            neural.check(self.con,prepared['ticket'],{'answer':[]})

    def test_changed_original_relative_path_and_crop_reject_ticket_outside_root_cwd(self):
        self.result()
        note=config.vault_dir('one')/'H3K9me3.md';note.write_text('H3K9me3 '+self.pid+':E0002')
        original=self.root/'original.pdf';original.write_bytes(b'original fixture bytes')
        crop=self.folder/'crops'/'test.png';crop.parent.mkdir();crop.write_bytes(b'old crop')
        meta=db.read_json(self.folder/'metadata.json');meta['source_files']=[{'role':'main','path':'original.pdf'}]
        db.write_json(self.folder/'metadata.json',meta)
        # Media construction is tested separately; only dependency bytes are under test here.
        with patch.object(media,'bundles',return_value=[]):
            prepared=neural.prepare(self.con,config.load(),'one','H3K9me3')
        original.write_bytes(b'changed original bytes')
        with self.assertRaisesRegex(ValueError,'media/crop'):
            neural.check(self.con,prepared['ticket'],{'answer':[]})
        db.write_json(self.folder/'media_manifest.json',{'sources':{'main':{'path':'original.pdf'}},'bundles':[{'crop':{'path':'crops/test.png'}}]})
        before=neural.dependency_fingerprint(self.pid);crop.write_bytes(b'changed crop')
        self.assertNotEqual(before,neural.dependency_fingerprint(self.pid))

    def test_object_sentence_resolution_and_unlocated_missing_objects(self):
        unit=self.result()
        self.rows[0]['mentions']=[self.pid+':F1',self.pid+':T3']
        db.write_jsonl(self.folder/'sentences.jsonl',self.rows)
        db.write_jsonl(self.folder/'figures.jsonl',[{'figure_id':self.pid+':F1','label':'Figure 1','source':'main','page':1,'bbox':None,'crop':None,'caption_sentence_ids':[self.rows[0]['sentence_id']],'mentioned_in':[self.rows[0]['sentence_id']]}])
        resolved=neural.resolve_sources(self.con,'one',[self.pid+':F1'])
        self.assertIn(self.rows[1]['sentence_id'],[i['id'] for i in resolved['items']])
        data=neural.media_reader(self.con,'one',self.pid,[unit['unit_id']])
        missing=next(b for b in data['bundles'] if b['object_id'].endswith(':T3'))
        self.assertEqual(missing['source_locator']['status'],'missing_object')
        self.assertIsNone(missing['source_locator']['page'])
        self.assertEqual(missing['mentions'][0]['page'],1)
        self.assertIsNone(missing['crop_url'])

    def test_supplementary_and_formula_ids_share_one_bounded_bundle_scan(self):
        self.result()
        objects=[self.pid+':FS1',self.pid+':M0123456789abcdef']
        bundles=[{'object_id':oid,'caption':[self.rows[0]],'data':[],'mentions':[],'anchors':[]} for oid in objects]
        with patch.object(media,'bundles',return_value=bundles) as scan:
            resolved=neural.resolve_sources(self.con,'one',objects,limit=1,query='H3K9me3')
        scan.assert_called_once_with(self.pid)
        self.assertEqual(resolved['items'][0]['id'],self.rows[0]['sentence_id'])
        self.assertTrue(resolved['truncated'])
        self.assertEqual(resolved['unresolved_ids'],[])

    def test_removed_typed_profile_invalidates_saved_ticket_safely(self):
        self.result()
        record={'concept_id':'local:h3','name':'H3K9me3','type':'molecule','synonyms':[],'parents':[]}
        db.write_jsonl(config.STORE/'concepts.jsonl',[record])
        self.con.execute('INSERT INTO concept_links VALUES(?,?,?,?,?,?)',('local:h3',self.pid,'mentioned',1,'["results"]',self.rows[0]['sentence_id']))
        self.con.commit()
        (config.vault_dir('one')/'H3K9me3.md').write_text('H3K9me3 '+self.pid+':E0001')
        ticket=neural.prepare(self.con,config.load(),'one','H3K9me3')['ticket']
        db.write_jsonl(config.STORE/'concepts.jsonl',[])
        with self.assertRaisesRegex(ValueError,'Node profile changed'):
            neural.check(self.con,ticket,{'answer':[]})

    def test_profile_reader_and_graph_reject_fake_legacy_term_match(self):
        self.result()
        record={'concept_id':'local:h3','name':'H3K9me3','type':'molecule','synonyms':[],'parents':[]}
        db.write_jsonl(config.STORE/'concepts.jsonl',[record,{'concept_id':'local:eg','name':'EG','type':'molecule','synonyms':[],'parents':[]}])
        (config.vault_dir('one')/'H3K9me3.md').write_text('H3K9me3 '+self.pid+':E0001')
        self.con.execute('INSERT INTO concept_links VALUES(?,?,?,?,?,?)',('local:h3',self.pid,'mentioned',1,'["results"]',self.rows[0]['sentence_id']))
        self.con.commit()
        reader=neural.node_reader(self.con,'one','H3K9me3.md')
        self.assertEqual(reader['profile']['name'],'H3K9me3')
        self.assertIn('not annotated',reader['profile_markdown'])
        self.assertFalse((config.CACHE_DIR/'node_profiles').exists())
        graphdata=graph.build(self.con,'one')
        self.assertFalse(any(e['kind']=='relation' for e in graphdata['edges']))

    def test_conflict_touching_one_node_side_keeps_both_scoped_sources_and_review_history(self):
        first=self.result()
        other='other2026_example_abcd';folder=db.paper_dir(other)
        meta={**db.read_json(self.folder/'metadata.json'),'paper_id':other,'title':'Other artificial source'}
        db.write_json(folder/'metadata.json',meta)
        row={**self.rows[1],'sentence_id':other+':S0001','seq':1,'passage_id':other+':0001','text':'The outcome increased in the comparison group.'}
        second={**first,'paper_id':other,'unit_id':other+':E0001','sentence_ids':[row['sentence_id']],'quote':row['text'],'terms':{'molecules':['EG']}}
        db.write_jsonl(folder/'sentences.jsonl',[row]);db.write_jsonl(folder/'evidence.jsonl',[second])
        db.load_paper(self.con,other)
        record={'concept_id':'local:h3','name':'H3K9me3','type':'molecule','synonyms':[],'parents':[]}
        db.write_jsonl(config.STORE/'concepts.jsonl',[record])
        self.con.execute('INSERT INTO concept_links VALUES(?,?,?,?,?,?)',('local:h3',self.pid,'mentioned',1,'["results"]',self.rows[0]['sentence_id']))
        self.con.commit()
        (config.vault_dir('one')/'H3K9me3.md').write_text('H3K9me3 '+first['unit_id'])
        relation={'relation_id':'r1','from_unit':first['unit_id'],'to_unit':second['unit_id'],'vault':'one','type':'contradicts'}
        for status in ('ai_proposed','human_confirmed','rejected','stale'):
            db.write_jsonl(config.STORE/'relations.jsonl',[{**relation,'status':status}])
            reader=neural.node_reader(self.con,'one','H3K9me3.md')
            conflict=reader['profile']['conflicts'][0]
            self.assertEqual(conflict['status'],status)
            self.assertIn(row['text'],[s['text'] for s in conflict['source_sides'][1]['items']])
            edges=[e for e in graph.build(self.con,'one')['edges'] if e['kind']=='relation']
            self.assertEqual(bool(edges),status in ('ai_proposed','human_confirmed'))
            self.assertEqual('[!conflict]' in reader['profile_markdown'],status in ('ai_proposed','human_confirmed'))
            if edges:self.assertEqual(len(edges[0]['source_sides']),2)
        self.con.execute('DELETE FROM paper_vaults WHERE paper_id=?',(other,));self.con.commit()
        self.assertEqual(neural.node_reader(self.con,'one','H3K9me3.md')['profile']['conflicts'],[])

    def test_http_routes_keep_host_token_and_current_scope_guards(self):
        import web
        server=web.Server(('127.0.0.1',0),web.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        def request(path,headers=None):
            return urllib.request.urlopen(urllib.request.Request(base+path,headers=headers or {}))
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught: request('/api/research/media?paper='+self.pid)
            self.assertEqual(caught.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError) as caught: request('/api/status',{'X-KB-Token':web.TOKEN,'Host':'evil.test'})
            self.assertEqual(caught.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError) as caught: request('/api/research/sources?vault=two&id='+self.rows[0]['sentence_id'],{'X-KB-Token':web.TOKEN})
            self.assertEqual(caught.exception.code,400)
            with request('/api/status',{'X-KB-Token':web.TOKEN}) as response:
                self.assertEqual(json.load(response)['scope_policy'],'current_vault_by_default')
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main(verbosity=2)


class RelinkAffectedConceptsTests(unittest.TestCase):
    setUp=fixtures.Demo3Tests.setUp
    tearDown=fixtures.Demo3Tests.tearDown

    def test_concept_that_lost_its_last_link_to_the_paper_is_still_reported_affected(self):
        other='other2026_x_abcd'   # keeps the ROS concept in the registry
        db.write_json(db.paper_dir(other)/'metadata.json',{'paper_id':other,'title':'Other','year':2026,'vaults':['one'],'authors':[],'source_files':[]})
        db.write_json(db.paper_dir(other)/'analysis.json',{'version':2,'molecules':['ROS']})
        db.load_paper(self.con,other)
        self.assertIn('local:ros',db.reload_paper(self.con,self.pid)['affected_concepts'])
        for row in self.rows: row['text']='Nothing relevant here at all.'
        db.write_jsonl(self.folder/'sentences.jsonl',self.rows)
        again=db.reload_paper(self.con,self.pid)   # load_paper wipes the old links first
        self.assertEqual(again['linked'],0)
        self.assertIn('local:ros',again['affected_concepts'])

    def test_paper_read_stamp_is_memoised_only_inside_a_scope_and_a_changed_file_invalidates(self):
        from kbsys import structure
        before=structure.paper_read_stamp(self.pid)
        with structure.stamp_scope():
            first=structure.paper_read_stamp(self.pid)
            self.rows[0]['text']='A changed sentence.'
            db.write_jsonl(self.folder/'sentences.jsonl',self.rows)
            self.assertEqual(structure.paper_read_stamp(self.pid),first)    # memo: the file is not re-read
        self.assertEqual(first,before)
        self.assertNotEqual(structure.paper_read_stamp(self.pid),before)    # scope over: the change is seen
