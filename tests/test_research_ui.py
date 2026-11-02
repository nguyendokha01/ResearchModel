"""Research workspace behavior in an isolated store, with providers mocked."""
import json
import shutil
import subprocess
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from . import test_core as fixtures
from kbsys import answer,config,db,find,mcp_server,neural,overlap,results


class ResearchTests(unittest.TestCase):
    setUp=fixtures.Demo3Tests.setUp
    tearDown=fixtures.Demo3Tests.tearDown

    def add_review(self):
        pid='review2026_example_cdef'
        folder=db.paper_dir(pid)
        db.write_json(folder/'metadata.json',{'paper_id':pid,'title':'Artificial review example','year':2026,'authors':[],'vaults':['one'],'source_files':[],'evidence_type':'primary_research','publication_types':['Review']})
        sentence={**self.rows[0],'sentence_id':pid+':S0001','passage_id':pid+':0001','text':'ROS is discussed in this review.'}
        db.write_jsonl(folder/'sentences.jsonl',[sentence])
        db.load_paper(self.con,pid)
        self.con.commit()
        return pid

    def add_results(self):
        sentence={**self.rows[0],'section':'results','text':'EG improved survival in the model.'}
        self.rows[0]=sentence
        db.write_jsonl(self.folder/'sentences.jsonl',self.rows)
        db.write_jsonl(self.folder/'evidence.jsonl',[{'unit_id':self.pid+':E0001','paper_id':self.pid,'seq':1,'unit_type':'result','sentence_ids':[sentence['sentence_id']],'pages':[1],'sections':['results'],'statement':sentence['text'],'quote':sentence['text'],'status':'ok','model':{},'intervention':{},'outcome':{},'terms':{}}])
        db.reload_paper(self.con,self.pid)
        self.con.commit()

    def test_article_metadata_separates_review_from_generated_primary_label(self):
        pid=self.add_review()
        cards=find.paper_cards(self.con,{pid})
        self.assertEqual(cards[pid]['evidence_type'],'review')
        primary=find.search(self.con,config.load(),'ROS',vault='one',semantic=False,filters={'evidence':'primary'})
        self.assertTrue(all(h['paper_id']!=pid for h in primary['hits']))
        review=find.search(self.con,config.load(),'ROS',vault='one',semantic=False,filters={'evidence':'review'})
        self.assertTrue(review['hits'])
        self.assertTrue(all(h['review_paper'] for h in review['hits']))

    def test_deep_search_rereads_source_even_without_ai(self):
        fast=find.search(self.con,config.load(),'ROS',vault='one',semantic=False,depth='fast')
        deep=find.search(self.con,config.load(),'ROS',vault='one',semantic=False,depth='deep')
        self.assertNotIn('context',fast['hits'][0])
        self.assertGreater(len(deep['hits'][0]['context']),1)
        self.assertIn(self.rows[0]['text'],[s['text'] for s in deep['hits'][0]['context']])

    def test_note_catalog_paginates_filters_and_skips_machine_stubs_hubs(self):
        vault=config.vault_dir('one')
        (vault/'Home.md').write_text('---\ntags: [kb-index]\n---\n[[EG]]')
        (vault/'Empty.md').write_text('---\nkb_stub: true\n---\nEmpty')
        for i in range(14):
            (vault/f'Chemical{i}.md').write_text('---\nkb_kind: chemical\nkb_roles: [intervention]\nkb_summary: An explicit saved description\n---\nEG source context')
        (vault/'Knowledge.md').write_text('---\nkb_kind: concept\n---\nKnowledge context')
        first=neural.search('one','',kind='chemical')
        second=neural.search('one','',kind='chemical',offset=12)
        self.assertEqual((first['total'],len(first['nodes']),len(second['nodes'])),(14,12,2))
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])
        self.assertEqual(first['edges'],[])
        self.assertTrue(all(n['entity_kind']=='chemical' for n in first['nodes']))
        self.assertEqual(neural.search('one','',kind='intervention')['total'],14)

    def test_results_are_scoped_and_eg_is_not_a_substring(self):
        self.add_results()
        found=results.browse(self.con,'one','EG')
        self.assertEqual(found['total'],1)
        self.assertEqual(found['records'][0]['text'],self.rows[0]['text'])
        self.assertEqual(results.browse(self.con,'two','EG')['total'],0)
        self.assertEqual(results.browse(self.con,'one','epi')['total'],0)
        self.assertEqual(results.overview(self.con,'one')['counts']['results'],1)

    def test_table_exposes_cells_caption_and_actual_crop_without_inventing_chart_values(self):
        (self.folder/'crops').mkdir()
        import pymupdf
        pix=pymupdf.Pixmap(pymupdf.csRGB,(0,0,10,10),False)
        pix.clear_with(255)
        pix.save(str(self.folder/'crops/T1.png'))
        db.write_jsonl(self.folder/'tables.jsonl',[{'table_id':self.pid+':T1','label':'Table 1','page':2,'source':'main','caption_sentence_ids':[self.rows[0]['sentence_id']],'crop':'crops/T1.png','header':['Dose','Result'],'rows':[['5 mg','40%']],'quality':'uncertain'}])
        db.reload_paper(self.con,self.pid)
        rows=results.browse(self.con,'one','40%',kind='table')['records']
        self.assertEqual(rows[0]['rows'],[['5 mg','40%']])
        self.assertEqual(rows[0]['quality'],'uncertain')
        self.assertIsNone(rows[0]['crop_url'])
        self.assertEqual(rows[0]['bundle']['crop']['status'],'retained_unverified')
        self.assertEqual(rows[0]['text'],self.rows[0]['text'])

    def test_ai_action_calls_provider_and_checks_exact_snapshot(self):
        ready={'ready':True,'provider':'fixture','message':'fixture'}
        with patch.object(answer,'provider_status',return_value=ready),patch.object(answer,'_chat',return_value=(json.dumps({'answer':[{'text':self.rows[0]['text'],'evidence':[1]}]}),{'tokens':3})) as chat:
            reply=neural.answer_question(self.con,config.load(),'one','ROS')
        chat.assert_called_once()
        self.assertEqual(reply['status'],'answered')
        self.assertEqual(reply['answer'][0]['check'],'ok')
        with patch.object(answer,'provider_status',return_value=ready),patch.object(answer,'_chat',return_value=(json.dumps({'answer':[{'text':'There were 900 subjects.','evidence':[1]}]}),{})):
            reply=neural.answer_question(self.con,config.load(),'one','ROS')
        self.assertNotEqual(reply['answer'][0]['check'],'ok')

    def test_note_sentence_ids_do_not_enter_evidence_alias_resolver(self):
        (config.vault_dir('one')/'ROS.md').write_text('ROS source '+self.rows[0]['sentence_id'])
        prepared=neural.prepare(self.con,config.load(),'one','ROS')
        self.assertTrue(prepared['items'])
        self.assertEqual(prepared['vault'],'one')

    def test_generated_summary_cannot_supply_numbers_or_causal_support(self):
        hit={'kind':'unit','id':'test:E0001','paper_id':self.pid,'unit_type':'result','text':'There were 5 subjects; the observation may be associated with survival.','statement':'There were 900 subjects and treatment causes survival.','pages':[1],'sections':['results'],'hedging':'plain','review_paper':False}
        checked=answer.check_sentence({'text':'There were 900 subjects.','evidence':[1]},[hit],{})
        self.assertIn('A2',checked['problems'])
        checked=answer.check_sentence({'text':'Treatment causes survival.','evidence':[1]},[hit],{})
        self.assertIn('A3',checked['problems'])
        self.assertNotIn('900',answer._item_block(1,hit,{}))

    def test_quality_units_are_answered_from_copied_sentences(self):
        hit={'kind':'unit','id':self.pid+':E0001','paper_id':self.pid,'status':'flagged','sentence_ids':[self.rows[0]['sentence_id']],'statement':'There were 900 subjects.','text':self.rows[0]['text'],'score':1}
        selected=answer.source_items(self.con,[hit])
        self.assertEqual(selected[0]['kind'],'sentence')
        self.assertIsNone(selected[0]['statement'])
        self.assertEqual(selected[0]['text'],self.rows[0]['text'])

    def test_unconfigured_or_bad_ai_reply_has_actionable_status(self):
        reply=neural.answer_question(self.con,config.load(),'one','ROS')
        self.assertEqual(reply['status'],'provider_required')
        self.assertTrue(reply['message'])
        with patch.object(answer,'provider_status',return_value={'ready':True,'provider':'fixture'}),patch.object(answer,'_chat',return_value=('{"answer":["bad"]}',{})):
            self.assertEqual(neural.answer_question(self.con,config.load(),'one','ROS')['status'],'invalid_reply')
        with self.assertRaises(ValueError):neural.answer_question(self.con,config.load(),'one','')


    def test_overlap_route_and_mcp_tool_share_one_lexical_overlap_result(self):
        import web
        text='The cells were then washed and incubated for 3 hours.'
        server=web.Server(('127.0.0.1',0),web.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def post(body,token=web.TOKEN):
            req=urllib.request.Request(f'http://127.0.0.1:{server.server_address[1]}/api/overlap',json.dumps(body).encode(),{'Content-Type':'application/json','X-KB-Token':token})
            return urllib.request.urlopen(req)
        with post({'text':'Notes: '+text,'vault':'one'}) as r:
            res=json.load(r)
        self.assertEqual(res['label'],'lexical overlap')
        hit=res['hits'][0]
        self.assertEqual(hit['sentence_id'],self.rows[3]['sentence_id'])
        span=hit['matched_spans'][0]
        self.assertEqual(hit['text'][span['start']:span['end']],text.rstrip('.'))
        self.assertEqual(res['papers'][0]['title'],'Artificial test example')
        self.assertAlmostEqual(res['papers'][0]['coverage'],6/7,places=5)  # 'Notes:' prefix adds one query shingle
        with post({'text':text,'vault':'two'}) as r:
            self.assertEqual(json.load(r)['hits'],[])
        with self.assertRaises(urllib.error.HTTPError) as err:
            post({'text':text},token='wrong')
        self.assertEqual(err.exception.code,403)
        self.assertEqual(mcp_server.call_tool(self.con,config.load(),'find_overlaps',{'text':text,'vault':'one'}),overlap.find_overlaps(self.con,text,'one'))
        self.assertIn('find_overlaps',{t['name'] for t in mcp_server.TOOLS})

    def test_mcp_ask_tools_default_to_four_part_view_and_instructions_describe_it(self):
        for name in ('ask_prepare','ask_check'):
            tool=next(t for t in mcp_server.TOOLS if t['name']==name)
            self.assertEqual(tool['inputSchema']['properties']['view']['enum'][0],'evidence4')
        self.assertIn('evidence4',mcp_server.INSTRUCTIONS)
        self.assertIn('## Bằng chứng từ tài liệu',mcp_server.INSTRUCTIONS)
        self.assertIn('## Không tìm thấy / không chắc chắn',mcp_server.INSTRUCTIONS)

    def test_neural_js_syntax_after_card_renderer_changes(self):
        node = shutil.which('node') or r'C:\Program Files\nodejs\node.exe'
        if not Path(node).is_file():
            self.skipTest('Node runtime absent; no dependency installation')
        path = Path(__file__).parents[1] / 'scripts/kbsys/web/neural.js'
        subprocess.run([node, '--check', str(path)], capture_output=True, text=True,
                       encoding='utf-8', check=True)


if __name__=='__main__':unittest.main(verbosity=2)
