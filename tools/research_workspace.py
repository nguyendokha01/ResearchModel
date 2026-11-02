"""Snapshot and audit one research vault without providers or source mutations."""
import argparse,hashlib,json,shutil,sqlite3,sys,uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT/'scripts'))
from kbsys import config,db,find,neural,results,review_policy

def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()

def manifest(con,vault):
    ids=[r[0] for r in con.execute('SELECT paper_id FROM paper_vaults WHERE vault=? ORDER BY paper_id',(vault,))]
    sources={}
    for pid in ids:
        for row in con.execute('SELECT path FROM source_files WHERE paper_id=?',(pid,)):
            if not row[0]:continue
            path=Path(row[0])
            if not path.is_absolute():path=ROOT/path
            if path.is_file():sources[str(path)]=sha(path)
    notes={str(p.relative_to(config.vault_dir(vault))):sha(p) for p in config.vault_dir(vault).rglob('*.md')}
    return {'root':str(ROOT),'vault':vault,'paper_ids':ids,'sources':sources,'notes':notes}

def snapshot(con,vault):
    """Full machine + vault backup under <config.ROOT>/runtime/research-workspace/<uuid>/ (no PDFs)."""
    out=manifest(con,vault)
    target=config.ROOT/'runtime/research-workspace'/uuid.uuid4().hex
    target.mkdir(parents=True)
    backup=sqlite3.connect(target/'library.db')
    con.backup(backup);backup.close()
    shutil.copytree(config.vault_dir(vault),target/'vault')
    for pid in out['paper_ids']:
        if db.paper_dir(pid).is_dir():shutil.copytree(db.paper_dir(pid),target/'papers'/pid)
    for name in ('concepts.jsonl','relations.jsonl','gap_candidates.jsonl','ui_preferences.json'):
        if (config.STORE/name).is_file():shutil.copy2(config.STORE/name,target/name)
    if (config.CONFIG_DIR/'ai.yaml').is_file():shutil.copy2(config.CONFIG_DIR/'ai.yaml',target/'ai.yaml')
    (target/'manifest.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8', newline='\n')
    return {'snapshot':str(target),'papers':len(out['paper_ids']),'source_files':len(out['sources']),'notes':len(out['notes'])}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['snapshot','audit'])
    parser.add_argument('--vault',required=True)
    args=parser.parse_args()
    if not config.vault_dir(args.vault).is_dir():raise ValueError('Vault does not exist')
    con=sqlite3.connect(config.DB_PATH.as_uri()+'?mode=ro',uri=True)
    con.row_factory=sqlite3.Row
    try:
        if args.action=='snapshot':
            print(json.dumps(snapshot(con,args.vault),ensure_ascii=False))
        else:
            out=manifest(con,args.vault)
            prepared=review_policy.context(con,args.vault)
            out['relevance']={pid:review_policy.relevance(con,pid,args.vault,prepared) for pid in out['paper_ids']}
            out['overview']=results.overview(con,args.vault)
            out['pending_topic_reviews']=len(review_policy.pending(con,args.vault))
            print(json.dumps(out,ensure_ascii=False,indent=2))
    finally:con.close()

if __name__=='__main__':main()
