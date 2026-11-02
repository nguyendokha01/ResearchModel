"""Acceptance regressions for the independent review and a split-page negation."""
import json
import shutil
import subprocess
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from . import test_core as fixtures
from kbsys import config, db, concepts, neural, node_profile, results, selftest, workflow

class ContextRevisionTests(unittest.TestCase):
    setUp=fixtures.Demo3Tests.setUp
    tearDown=fixtures.Demo3Tests.tearDown

    def split_result(self):
        before={**self.rows[0], 'section':'results', 'page':8, 'text':'In group B, no significant change of'}
        caption={**self.rows[1], 'section':'results', 'page':9, 'kind':'figure_caption', 'text':'Fig. 3. A separate illustration.'}
        after={**self.rows[2], 'section':'results', 'page':9, 'text':'H3K9me3 between the groups was found.'}
        self.rows=[before, caption, after]
        unit={'paper_id':self.pid,'unit_id':self.pid+':E0001','seq':1,'unit_type':'result',
              'statement':'Stored legacy interpretation', 'quote':'Incomplete legacy quote',
              'sentence_ids':[after['sentence_id']], 'pages':[9], 'sections':['results'],
              'status':'ok','model':{},'intervention':{},'outcome':{},'terms':{}}
        db.write_jsonl(self.folder/'sentences.jsonl', self.rows)
        db.write_jsonl(self.folder/'evidence.jsonl', [unit])
        db.reload_paper(self.con,self.pid);self.con.commit()
        return unit

    def test_split_negation_is_labelled_context_in_reader_and_results(self):
        unit=self.split_result()
        original={n:(self.folder/n).read_bytes() for n in ('sentences.jsonl','evidence.jsonl')}
        r=neural.resolve_sources(self.con,'one',[unit['unit_id']],context_limit=0)
        self.assertEqual([s['id'] for s in r['items']], [self.rows[0]['sentence_id'],self.rows[2]['sentence_id']])
        self.assertEqual(r['items'][0]['context_basis'],'preceding_source_fragment')
        self.assertIn('no significant change',r['items'][0]['text'])
        self.assertTrue(r['items'][0]['uncertainty'])
        card=results.browse(self.con,'one','H3K9me3')['records'][0]
        self.assertIn('no significant change',card['text'])
        self.assertEqual(card['pages'],[8,9])
        self.assertEqual(card['sentence_ids'],unit['sentence_ids'])
        self.assertEqual(card['context_sentence_ids'],[self.rows[0]['sentence_id']])
        self.assertTrue(card['source_url'].endswith('#page=8'))
        for name,data in original.items(): self.assertEqual((self.folder/name).read_bytes(),data)

    def test_concurrent_search_serves_snapshot_without_stealing_index_lock(self):
        note = config.vault_dir('one') / 'EG.md'
        note.write_text('EG source fixture', encoding='utf-8')
        neural.refresh('one')
        self.assertEqual([n['title'] for n in neural.search('one', 'source fixture')['nodes']], ['EG'])
        note.write_text('EG fresh replacement', encoding='utf-8')
        held, release = threading.Event(), threading.Event()
        def writer():
            with workflow.writer_lock('vault-index:one'):
                held.set()
                if not release.wait(3): raise RuntimeError('Fixture writer not released')
        def competing_writer():
            try:
                with workflow.writer_lock('vault-index:one', timeout=0):
                    return False
            except workflow.WriterBusy:
                return True
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(writer)
            self.assertTrue(held.wait(2))
            reader = pool.submit(neural.search, 'one', 'source fixture')
            try:
                result = reader.result(timeout=1)
                self.assertEqual(result['index_status'], 'refreshing')
                self.assertEqual([n['title'] for n in result['nodes']], ['EG'])
                self.assertNotIn('fresh replacement', ' '.join(n.get('excerpt', '') for n in result['nodes']))
                self.assertTrue(pool.submit(competing_writer).result(timeout=1))
            finally:
                release.set()
            owner.result(timeout=3)

    def test_unknown_result_source_is_not_counted_as_primary(self):
        self.split_result()
        self.con.execute("UPDATE papers SET evidence_type='unknown' WHERE paper_id=?", (self.pid,))
        self.con.commit()
        self.assertEqual(results.browse(self.con, 'one')['records'][0]['evidence_type'], 'unknown')
        counts = results.overview(self.con, 'one')['counts']
        self.assertEqual(counts['primary_papers'], 0)
        self.assertEqual(counts['other_papers'], 1)

    def test_fallback_does_not_join_completed_or_other_source_section_page(self):
        self.split_result(); anchor=self.rows[2]['sentence_id']
        for changed in ({'text':'A finished sentence.'},{'source':'supp1'}, {'section':'discussion'}, {'page':7}, {'in_table':True}):
            with self.subTest(changed=changed):
                rows={s['sentence_id']:s for s in self.rows}
                rows[self.rows[0]['sentence_id']]={**self.rows[0],**changed}
                wanted,_=node_profile._source_ids([anchor],rows)
                self.assertEqual(wanted,{anchor})

    def test_explicit_continuation_is_resolved_backwards_without_mutation(self):
        self.split_result(); self.rows[0]['continues']=self.rows[2]['sentence_id']
        source={s['sentence_id']:s for s in self.rows}
        old=json.dumps(source,sort_keys=True)
        refs=node_profile._refs([self.rows[2]['sentence_id']], source,self.pid,{})
        self.assertEqual([r['sentence_id'] for r in refs],[self.rows[0]['sentence_id'],self.rows[2]['sentence_id']])
        self.assertEqual(refs[0]['context_basis'],'source_anchor_or_continues')
        self.assertEqual(json.dumps(source,sort_keys=True),old)

    def test_selftest_requires_source_linked_note_and_not_legacy_tag_only(self):
        base={'status':'ai_proposed','type':'concept','synonyms':[],'parents':[],'mesh_tree_numbers':[]}
        records=[{**base,'name':'Unmatched old tag','concept_id':'old'}, {**base,'name':'ROS','concept_id':'ros'}]
        db.write_jsonl(config.STORE/'concepts.jsonl',records)
        self.con.execute('INSERT INTO paper_terms(paper_id,term,term_type) VALUES (?,?,?)',(self.pid,'Unmatched old tag','concept'))
        self.con.execute("INSERT INTO concept_links VALUES (?,?,?,?,?,?)", ("ros",self.pid,"mentioned",3,json.dumps(["introduction"]),self.rows[0]["sentence_id"]));self.con.commit()
        with patch('kbsys.concepts.mesh.available',return_value=False):
            report=selftest.Report();selftest.check_concepts(report,self.con)
        missing=[e for e in report.errors if 'missing canonical note' in e]
        self.assertTrue(any('ROS.md' in e for e in missing),missing)
        self.assertFalse(any('Unmatched old tag' in e for e in missing),missing)

class ActualRendererTests(unittest.TestCase):
    def test_formula_card_uses_source_anchor_and_page_instead_of_opaque_id(self):
        node = shutil.which('node') or r'C:\Program Files\nodejs\node.exe'
        if not Path(node).is_file(): self.skipTest('Node runtime absent; no dependency installation')
        path = Path(__file__).parents[1]/'scripts/kbsys/web/neural.js'
        script = r'''const fs=require('fs'),vm=require('vm');
const code=fs.readFileSync(process.argv[1],'utf8'),ctx={document:{querySelector:()=>({content:'fixture'})}};
vm.createContext(ctx);vm.runInContext(code.slice(0,code.indexOf('async function openRecord')),ctx);
ctx.fixture={kind:'formula',id:'p:M1234567890abcdef',label:'p:M1234567890abcdef',pages:[10],text:'',title:'Fixture',bundle:{anchors:[{text:'P < 0.05 from the source.'}]}};
console.log(vm.runInContext('card(fixture,0)',ctx));'''
        run = subprocess.run([node,'-e',script,str(path)],capture_output=True,text=True,encoding='utf-8',check=True)
        self.assertIn('trang 10', run.stdout)
        self.assertIn('P &lt; 0.05 from the source.', run.stdout)
        self.assertNotIn('p:M1234567890abcdef', run.stdout)

    def test_uncertain_and_ragged_table_does_not_imply_column_alignment(self):
        node = shutil.which('node') or r'C:\Program Files\nodejs\node.exe'
        if not Path(node).is_file(): self.skipTest('Node runtime absent; no dependency installation')
        path = Path(__file__).parents[1]/'scripts/kbsys/web/neural.js'
        script = r'''const fs=require('fs'),vm=require('vm');
const code=fs.readFileSync(process.argv[1],'utf8'),ctx={document:{querySelector:()=>({content:'fixture'})}};
vm.createContext(ctx);vm.runInContext(code.slice(0,code.indexOf('async function openRecord')),ctx);
for(const fixture of [{quality:'uncertain',header:['n','mean'],rows:[['10','4']]},{quality:'ok',header:['n','mean'],rows:[['10']]},{quality:'ok',header:['n','mean'],rows:[['10','4']]}]){
ctx.fixture=fixture;console.log(JSON.stringify({html:vm.runInContext('table(fixture)',ctx)}));}'''
        run = subprocess.run([node,'-e',script,str(path)],capture_output=True,text=True,encoding='utf-8',check=True)
        rendered = [json.loads(s)['html'] for s in run.stdout.splitlines()]
        for html in rendered[:2]:
            self.assertNotIn('<table>', html)
            self.assertIn('chưa được ghép', html)
            self.assertIn('10', html.split('<details>',1)[1])
        self.assertIn('<table>', rendered[2])

    def test_result_reader_keeps_source_visible_and_legacy_direction_qualified(self):
        node = shutil.which('node') or r'C:\Program Files\nodejs\node.exe'
        if not Path(node).is_file(): self.skipTest('Node runtime absent; no dependency installation')
        path = Path(__file__).parents[1]/'scripts/kbsys/web/neural.js'
        script = r'''const fs=require('fs'),vm=require('vm');
const code=fs.readFileSync(process.argv[1],'utf8'), els={};
const ctx={document:{querySelector:s=>els[s]||(els[s]={content:'fixture',value:'one',innerHTML:''}),querySelectorAll:()=>[]},URL};
vm.createContext(ctx);vm.runInContext(code.slice(0,code.indexOf('const steps=')),ctx);
ctx.fixture={kind:'result',cite:'Fixture 2026',title:'Source',text:'No significant change of H3K9me3 was found.',statement:'Legacy mixed marker annotation',model:{},intervention:{},outcome:{direction:'increase'},quality_details:[],media:[],sources:{items:[]},evidence_type:'unknown'};
(async()=>{await vm.runInContext('openRecord(fixture)',ctx);console.log(JSON.stringify({html:els['#reader'].innerHTML,card:vm.runInContext('card(fixture,0)',ctx)}));})().catch(e=>{console.error(e);process.exitCode=1});'''
        run = subprocess.run([node,'-e',script,str(path)],capture_output=True,text=True,encoding='utf-8',check=True)
        rendered = json.loads(run.stdout)
        visible, folded = rendered['html'].split('<details>',1)
        self.assertIn('No significant change', visible)
        self.assertNotIn('increase', visible)
        self.assertIn('increase', folded)
        self.assertIn('chưa được nghiệm thu khoa học', folded)
        self.assertIn('Chưa xác định loại nguồn', rendered['card'])
        self.assertNotIn('Bài gốc', rendered['card'])

    def test_secondary_unknown_and_visible_source_identity(self):
        node=shutil.which('node') or r'C:\Program Files\nodejs\node.exe'
        if not Path(node).is_file():self.skipTest('Node runtime absent; no dependency installation')
        path=Path(__file__).parents[1]/'scripts/kbsys/web/neural.js'
        script=r'''const fs=require('fs'),vm=require('vm');
const code=fs.readFileSync(process.argv[1],'utf8');
const ctx={document:{querySelector:()=>({content:'fixture'})}};vm.createContext(ctx);
vm.runInContext(code.slice(0,code.indexOf('async function openRecord')),ctx);
for(const t of ['primary_research','review','systematic_review','meta_analysis','commentary','']){
const fields=Object.fromEntries(['definitions','full_names','source_classes','purpose','roles_context','significance','limitations'].map(f=>[f,{items:[]}])) ;
fields.full_names.items=[{value:'Ethylene glycol',abbreviation:'EG',sources:[{text:'Ethylene glycol (EG)',sentence_id:'p:S1',page:1}]}];
const p={papers:[{paper_id:'p',cite:'Fixture 2026',evidence_type:t,fields,protocol_variants:[],results:[]}],aliases:[]};
ctx.fixture=p; console.log(JSON.stringify({type:t,html:vm.runInContext('profileView(fixture)',ctx)}));
}'''
        run=subprocess.run([node,'-e',script,str(path)],capture_output=True,text=True,encoding='utf-8',check=True)
        rows=[json.loads(x) for x in run.stdout.splitlines()]
        for row in rows:
            with self.subTest(type=row['type']):
                html=row['html']
                self.assertEqual('Bài gốc' in html,row['type']=='primary_research')
                self.assertIn('Ethylene glycol',html.split('<details>',1)[0])
        self.assertIn('Chưa xác định loại nguồn',rows[-1]['html'])

if __name__=='__main__':unittest.main()
