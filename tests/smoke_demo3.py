"""Exercise local HTTP/MCP contracts against an isolated temporary paper and vault."""
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .test_core import Demo3Tests
from kbsys import config, mcp_server, notes
import web

def main():
    fixture = Demo3Tests()
    fixture.setUp()
    server = None
    checks = []
    def check(name, ok):
        checks.append({'name': name, 'ok': bool(ok)})
        if not ok:
            raise AssertionError(name)
    try:
        web.CFG = config.load()
        # hops follow knowledge edges only: a result note links the node note Methods/Protocol in its kb:auto block
        topic_body = 'ROS context\n' + notes.AUTO_START + '\n[[Protocol]]\n' + notes.AUTO_END + '\n'
        (config.vault_dir('one') / 'Topic.md').write_text('---\ntype: result\nid: p1:E0001\n---\n' + topic_body, encoding='utf-8', newline='\n')
        (config.vault_dir('one') / 'Methods').mkdir()
        (config.vault_dir('one') / 'Methods' / 'Protocol.md').write_text('Methods example', encoding='utf-8')
        server = web.Server(('127.0.0.1', 0), web.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_address[1]}'
        def request(path, body=None, token=True, host=None):
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['X-KB-Token'] = web.TOKEN
            if host:
                headers['Host'] = host
            req = Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
            try:
                with urlopen(req, timeout=10) as response:
                    raw = response.read()
                    return response.status, raw, response.headers
            except HTTPError as exc:
                return exc.code, exc.read(), exc.headers
        def api(path, body=None):
            status, raw, _ = request(path, body)
            check('HTTP ' + path, status == 200)
            return json.loads(raw)
        for path in ('/', '/library', '/graph', '/neural'):
            status, raw, headers = request(path)
            check('page ' + path, status == 200 and web.TOKEN.encode() in raw and headers.get('Content-Security-Policy'))
        check('missing token rejected', request('/api/library', token=False)[0] == 403)
        check('foreign Host rejected', request('/neural', host='outside.example')[0] == 403)
        check('static neural JS', request('/static/neural.js')[0] == 200)
        library = api('/api/library?vault=one')
        check('library profile state', library['papers'][0]['structured_profiles']['pending'] == ['introduction_wh', 'protocols'])
        found = api('/api/neural/search?vault=one&q=ROS&hops=1')
        check('real notes and links', {n['path'] for n in found['nodes']} == {'Topic.md', 'Methods/Protocol.md'})
        note = api('/api/neural/note?vault=one&path=Topic.md')
        check('untrusted note text intact', note['body'] == '\n' + topic_body)
        inventory = api('/api/structure?id=' + fixture.pid)
        check('structure source coverage', inventory['coverage']['required'] == 6)
        protocols = api('/api/protocols?vault=one')
        check('protocol source order', protocols['records'][0]['source'][0]['text'] == fixture.rows[2]['text'])
        snapshot = api('/api/neural/prepare', {'vault': 'one', 'question': 'ROS', 'hops': 1})
        checked = api('/api/neural/check', {'ticket': snapshot['ticket'], 'reply': {'answer': [{'text': snapshot['items'][0]['text'], 'evidence': [1]}]}})
        check('checked exact source reply', checked['answer'][0]['check'] == 'ok')
        checked = api('/api/neural/check', {'ticket': snapshot['ticket'], 'reply': {'answer': [{'text': 'There were 900 subjects.', 'evidence': [1]}]}})
        check('invented quantity rejected', checked['answer'][0]['check'] != 'ok')
        api('/api/workflow')
        summary=api('/api/research/overview?vault=one')
        check('scoped research overview',summary['counts']['papers']==1)
        catalog=api('/api/research/results?vault=two&kind=table')
        check('results stay inside vault',catalog['total']==0)
        fast=api('/api/search?vault=one&q=ROS&speed=fast')
        deep=api('/api/search?vault=one&q=ROS&speed=deep')
        check('deep search context without provider',not fast['hits'][0].get('context') and deep['hits'][0].get('context'))
        unavailable=api('/api/neural/answer',{'vault':'one','question':'ROS'})
        check('AI is explicit when provider unavailable',unavailable['status']=='provider_required' and unavailable['message'])
        initialized = mcp_server.handle(fixture.con, web.CFG, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': mcp_server.PROTOCOLS[0], 'clientInfo': {'name': 'demo3-smoke'}}})
        check('MCP initialize version', initialized['result']['serverInfo']['version'] == '3.0')
        listed = mcp_server.handle(fixture.con, web.CFG, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        names = {tool['name'] for tool in listed['result']['tools']}
        check('MCP additive tools', {'neural_search', 'read_vault_note', 'neural_prepare', 'neural_check', 'get_section_profile', 'submit_section_profile', 'get_protocol_profile', 'submit_protocol_profile', 'compare_protocols', 'workflow_status'} <= names)
        check('MCP live retrieval', len(mcp_server.call_tool(fixture.con, web.CFG, 'neural_search', {'vault': 'one', 'query': 'ROS'})['nodes']) == 2)
        print(json.dumps({'passed': len(checks), 'failed': 0, 'checks': checks}, ensure_ascii=False, indent=2))
        return 0
    finally:
        if server:
            server.shutdown()
            server.server_close()
        fixture.tearDown()

if __name__ == '__main__':
    raise SystemExit(main())
