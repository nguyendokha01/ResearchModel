"""Relevance, automatic dedup, and migration tests in disposable stores."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from kbsys import config, db, evidence, ingest, review_policy

class ReviewPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='review-policy-')
        self.root = Path(self.tmp.name)
        paths = {'ROOT': self.root, 'CONFIG_DIR': self.root/'config', 'STORE': self.root/'knowledge_store',
                 'DB_PATH': self.root/'knowledge_store/library.db', 'PAPERS_DIR': self.root/'knowledge_store/papers',
                 'LOGS_DIR': self.root/'knowledge_store/logs', 'CACHE_DIR': self.root/'knowledge_store/cache',
                 'VAULTS_DIR': self.root/'vaults', 'REVIEW_PENDING': self.root/'review/pending'}
        self.saved = {k: getattr(config,k) for k in paths}
        for k,v in paths.items(): setattr(config,k,v)
        config.vault_dir('vitrification').mkdir(parents=True)
        (config.vault_dir('vitrification')/'Home.md').write_text('# Vitrification\n',encoding='utf-8')
        self.con = db.connect()
        self.paper('anchor', 'Vitrification study', 'Resveratrol was used during vitrification.')
        self.paper('other', 'Unrelated study', 'Resveratrol was used in the cells.')
        self.con.commit()

    def tearDown(self):
        self.con.close()
        for k,v in self.saved.items(): setattr(config,k,v)
        self.tmp.cleanup()

    def paper(self,pid,title,text,section='methods'):
        row = {'sentence_id':pid+':S0001','paper_id':pid,'seq':1,'passage_id':pid+':0001','page':1,
               'section':section,'kind':'text','text':text,'source':'main'}
        folder=db.paper_dir(pid)
        db.write_json(folder/'metadata.json',{'paper_id':pid,'title':title,'vaults':['vitrification'],
                 'authors':[],'source_files':[],'classification':'CONNECTED','analysis_status':'done'})
        db.write_jsonl(folder/'sentences.jsonl',[row])
        db.load_paper(self.con,pid)

    def unit(self,pid='other',seq=1,**changes):
        row=next(db.read_jsonl(db.paper_dir(pid)/'sentences.jsonl'))
        return {'unit_id':f'{pid}:E{seq:04d}','seq':seq,'unit_type':'method_detail','status':'ok',
                'statement':row['text'],'quote':row['text'],'sentence_ids':[row['sentence_id']],
                'pages':[1],'sections':['methods'],'intervention':{'agent':'Resveratrol'},'model':{},
                'outcome':{},'terms':{'molecules':['Resveratrol']},'flags':[], **changes}

    def test_direct_topic_source_and_not_ai_fallback_tags(self):
        self.assertTrue(review_policy.relevance(self.con,'anchor','vitrification')['connected'])
        db.write_json(db.paper_dir('other')/'analysis.json',{'topics':['Vitrification'],'classification':'CORE'})
        r=review_policy.relevance(self.con,'other','vitrification')
        self.assertFalse(r['connected']); self.assertEqual(r['classification'],'POTENTIAL_BRIDGE')

    def test_index_hub_and_incidental_body_or_reference_edge_do_not_connect(self):
        self.paper('stray','An unrelated paper','Vitrification was mentioned once.')
        self.paper('refs','Another paper','Vitrification was mentioned twice. Vitrification.',section='references')
        (config.vault_dir('vitrification')/'Home.md').write_text('# Vitrification\n[[other]] [[stray]] [[refs]]',encoding='utf-8')
        self.assertTrue(review_policy.relevance(self.con,'stray','vitrification')['connected'])
        self.assertFalse(review_policy.relevance(self.con,'other','vitrification')['connected'])
        self.assertFalse(review_policy.relevance(self.con,'refs','vitrification')['connected'])

    def test_shared_source_structured_entity_connects_but_forged_and_flagged_do_not(self):
        for pid in ('anchor','other'): db.write_jsonl(db.paper_dir(pid)/'evidence.jsonl',[self.unit(pid)])
        r=review_policy.relevance(self.con,'other','vitrification')
        self.assertTrue(r['connected']); self.assertEqual(r['connection']['paper_id'],'anchor')
        db.write_jsonl(db.paper_dir('other')/'evidence.jsonl',[self.unit(quote='Forged Resveratrol')])
        self.assertFalse(review_policy.relevance(self.con,'other','vitrification')['connected'])
        db.write_jsonl(db.paper_dir('other')/'evidence.jsonl',[self.unit(status='flagged',flags=[{'code':'V5'}])])
        self.assertFalse(review_policy.relevance(self.con,'other','vitrification')['connected'])

    def test_exact_dedup_aliases_and_distinct_models_outcomes_hedging_preserved(self):
        one=self.unit(); two=self.unit(seq=2,statement='  '+one['statement'].upper()+'  ')
        units,aliases=review_policy.deduplicate([one,two]); self.assertEqual(len(units),1)
        self.assertEqual(aliases,{'other:E0002':'other:E0001'})
        for change in ({'model':{'species':'Bovine'}},{'outcome':{'measure':'ATP'}},
                       {'hedging':'may'},{'status':'human_confirmed'},{'sentence_ids':['other:S0002']}):
            self.assertEqual(len(review_policy.deduplicate([one,self.unit(seq=2,**change)])[0]),2)

    def test_exact_dedup_merges_only_tag_variants_and_preserves_quality_status(self):
        one=self.unit(status='flagged',flags=[{'unit_index':1,'code':'V5','message':'hedging'}])
        two=self.unit(seq=2,status='flagged',terms={'concepts':['Oxidative stress']},
                      flags=[{'unit_index':2,'code':'V5','message':'hedging'}])
        units,aliases=review_policy.deduplicate([one,two])
        self.assertEqual(len(units),1);self.assertEqual(units[0]['status'],'flagged')
        self.assertEqual(units[0]['terms']['concepts'],['Oxidative stress'])
        self.assertEqual(units[0]['flags'][0]['code'],'V5')

    def test_dedup_keeps_old_ids_but_renumbers_summary_positions(self):
        units, aliases = review_policy.deduplicate([self.unit(), self.unit(seq=2),
                                                   self.unit(seq=3, model={'cell_type':'Oocyte'})])
        self.assertEqual([u['unit_id'] for u in units], ['other:E0001','other:E0003'])
        self.assertEqual([u['seq'] for u in units], [1,2])
        self.assertEqual(aliases['other:E0002'], 'other:E0001')

    def test_legacy_pending_superseded_without_losing_decisions_or_quality_details(self):
        ids=[db.add_review_item(self.con,k,'original detail','other') for k in sorted(review_policy.LEGACY_KINDS)]
        resolved=db.add_review_item(self.con,'classification','human kept','other')
        self.con.execute("UPDATE review_items SET status='resolved' WHERE item_id=?",(resolved,))
        review_policy.refresh_paper(self.con,'other')
        self.assertTrue(all(self.con.execute('SELECT status FROM review_items WHERE item_id=?',(i,)).fetchone()[0]=='superseded' for i in ids))
        self.assertEqual(len(review_policy.pending(self.con,'vitrification')),0) # resolved researcher choice respected
        self.assertEqual(self.con.execute('SELECT detail FROM review_items WHERE item_id=?',(resolved,)).fetchone()[0],'human kept')

    def test_relevance_queue_idempotent_and_scoped(self):
        review_policy.refresh_paper(self.con,'other'); review_policy.refresh_paper(self.con,'other')
        self.assertEqual(len(review_policy.pending(self.con,'vitrification')),1)
        self.assertEqual(review_policy.pending(self.con,'another'),[])
        review_policy.refresh_paper(self.con,'anchor')
        self.assertEqual(len(review_policy.pending(self.con,'vitrification')),1)

    def test_resolved_topic_decision_scoped_per_vault_and_null_not_human(self):
        meta=db.read_json(db.paper_dir('other')/'metadata.json');meta['vaults'].append('second')
        config.vault_dir('second').mkdir();(config.vault_dir('second')/'Home.md').write_text('# Histones')
        db.write_json(db.paper_dir('other')/'metadata.json',meta);db.load_paper(self.con,'other')
        item=db.add_review_item(self.con,'topic_relevance','[second] old reason','other')
        self.con.execute("UPDATE review_items SET status='resolved' WHERE item_id=?",(item,))
        legacy=db.add_review_item(self.con,'analysis_flags','quality','other')
        self.con.execute('UPDATE review_items SET note_name=? WHERE item_id=?',('null',legacy))
        path=config.vault_dir('vitrification')/'Review Queue/null.md';path.parent.mkdir()
        path.write_text('---\ndecision: null\n---\nHuman observation stays.\n')
        review_policy.refresh_paper(self.con,'other')
        self.assertEqual(len(review_policy.pending(self.con,'vitrification')),1)
        self.assertEqual(review_policy.pending(self.con,'second'),[])
        self.assertEqual(self.con.execute('SELECT status FROM review_items WHERE item_id=?',(legacy,)).fetchone()[0],'superseded')
        self.assertIn('Human observation stays.',path.read_text())

    def test_review_note_creation_preserves_existing_human_writing(self):
        from kbsys import notes
        item=db.add_review_item(self.con,'topic_relevance','[vitrification] reason','other')
        path=config.vault_dir('vitrification')/'Review Queue'/f'Review {item:04d} - topic_relevance.md'
        path.parent.mkdir();path.write_text('Human note at matching filename.')
        notes.write_review_note(self.con,item,'vitrification')
        self.assertEqual(path.read_text(),'Human note at matching filename.')
        name=self.con.execute('SELECT note_name FROM review_items WHERE item_id=?',(item,)).fetchone()[0]
        other=path.parent/(name+'.md');other.write_text(other.read_text(encoding='utf-8')+'My added decision.\n',encoding='utf-8')
        before=other.read_bytes();notes.write_review_note(self.con,item,'vitrification')
        self.assertEqual(other.read_bytes(),before)

    def test_inline_human_decision_and_note_bytes_preserved(self):
        item=db.add_review_item(self.con,'analysis_flags','old quality warning','other')
        self.con.execute('UPDATE review_items SET note_name=? WHERE item_id=?',('human',item))
        folder=config.vault_dir('vitrification')/'Review Queue';folder.mkdir()
        path=folder/'human.md'; text='---\ndecision: keep\n---\nMy personal note.\n';path.write_text(text)
        review_policy.refresh_paper(self.con,'other')
        self.assertEqual(path.read_text(),text)
        self.assertEqual(self.con.execute('SELECT status FROM review_items WHERE item_id=?',(item,)).fetchone()[0],'pending')

    def test_dry_run_no_writes_and_apply_backup_remaps_summary(self):
        folder=db.paper_dir('other'); original=[self.unit(),self.unit(seq=2)]
        db.write_jsonl(folder/'evidence.jsonl',original)
        db.write_json(folder/'analysis.json',{'version':2,'summary':[{'text':'example','units':[2]}]})
        before=(folder/'evidence.jsonl').read_bytes(); report=review_policy.refresh(self.con,'vitrification')
        self.assertFalse(report['applied']);self.assertEqual((folder/'evidence.jsonl').read_bytes(),before)
        self.assertFalse((config.STORE/'review_policy_history').exists())
        with patch('kbsys.notes.write_paper_note'),patch('kbsys.notes.write_indexes'):
            report=review_policy.refresh(self.con,'vitrification',True)
        archive=Path(report['archive']);self.assertEqual((archive/'other/evidence.jsonl').read_bytes(),before)
        self.assertTrue((archive/'library.db').is_file())
        self.assertEqual(db.read_json(folder/'analysis.json')['summary'][0]['units'],[1])
        self.assertEqual(db.read_json(folder/'evidence_aliases.json'),{'other:E0002':'other:E0001'})
        self.assertEqual(len(list(db.read_jsonl(folder/'evidence.jsonl'))),1)
        self.assertEqual(evidence.resolve_unit(self.con,'other:E0002')['unit_id'],'other:E0001')

    def test_migration_archives_only_exact_machine_templates_and_reports_edits(self):
        from kbsys import notes
        pure=db.add_review_item(self.con,'analysis_flags','pure quality warning','other')
        edited=db.add_review_item(self.con,'analysis_incomplete','protected quality warning','other')
        for item in (pure,edited): notes.write_review_note(self.con,item,'vitrification')
        paths={i:config.vault_dir('vitrification')/'Review Queue'/(self.con.execute(
            'SELECT note_name FROM review_items WHERE item_id=?',(i,)).fetchone()[0]+'.md') for i in (pure,edited)}
        pure_bytes=paths[pure].read_bytes()
        paths[edited].write_text(paths[edited].read_text(encoding='utf-8')+'Human comment.\n',encoding='utf-8')
        protected=paths[edited].read_bytes();self.con.commit()
        preview=review_policy.refresh(self.con,'vitrification')
        actions={a['item_id']:a['action'] for a in preview['note_actions']}
        self.assertEqual(actions[pure],'archive_machine_note');self.assertEqual(actions[edited],'protected_human_note')
        self.assertTrue(paths[pure].exists())
        with patch('kbsys.notes.write_paper_note'),patch('kbsys.notes.write_indexes'):
            result=review_policy.refresh(self.con,'vitrification',True)
        self.assertFalse(paths[pure].exists());self.assertEqual(paths[edited].read_bytes(),protected)
        moved=next(a for a in result['note_actions'] if a['item_id']==pure)
        self.assertEqual(Path(moved['archive_path']).read_bytes(),pure_bytes)
        self.assertEqual(self.con.execute('SELECT status FROM review_items WHERE item_id=?',(pure,)).fetchone()[0],'superseded')

    def test_ingest_exact_copy_auto_associates_and_preserves_original_without_flag(self):
        src=self.root/'canonical.pdf';src.write_bytes(b'%PDF fixture bytes')
        meta=db.read_json(db.paper_dir('other')/'metadata.json')
        sha=ingest.extract.sha256_file(src)
        meta['source_files']=[{'sha256':sha,'path':str(src),'status':'active','role':'main'}]
        db.write_json(db.paper_dir('other')/'metadata.json',meta);db.load_paper(self.con,'other')
        incoming=self.root/'duplicate.pdf';incoming.write_bytes(src.read_bytes())
        with patch('kbsys.notes.write_paper_note'),patch('kbsys.notes.write_indexes'):
            outcome,pid=ingest.ingest_one({},self.con,incoming,'batch','vitrification')
        self.assertEqual(outcome,'duplicate');self.assertEqual(pid,'other');self.assertTrue(src.exists())
        copies=db.read_json(db.paper_dir('other')/'metadata.json')['duplicate_sources']
        self.assertEqual((config.ROOT/copies[0]['path']).read_bytes(),src.read_bytes())
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM review_items WHERE kind='duplicate_file'").fetchone()[0],0)

if __name__=='__main__': unittest.main()
