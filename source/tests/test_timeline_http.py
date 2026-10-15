"""Real HTTP integration checks with synthetic media only. Requires localhost binding."""
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from http.server import ThreadingHTTPServer
from serve_fixture import fixture
from pipeline_core.dashboard import DashboardContext,_DashboardHandler

class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        product,cls.workspace=fixture(cls.temp.name)
        handler=type('QAHandler',(_DashboardHandler,),{'context':DashboardContext(product,cls.workspace)})
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()
    def setUp(self):
        p=self.api("load")[1]["project"]
        if p["revision"]>1:
            self.api("restore",revision=p["revision"],restore_revision=1)
    def req(self,path,body=None,headers=None,raw=None):
        data=raw if raw is not None else json.dumps(body).encode() if body is not None else None
        h={'Content-Type':'application/json','X-NextPlay':'timeline'}
        h.update(headers or {})
        r=urllib.request.Request(self.base+path,data=data,headers=h)
        try:
            response=urllib.request.urlopen(r,timeout=60)
        except urllib.error.HTTPError as e:
            response=e
        with response:return response.status,response.read(),dict(response.headers)
    def api(self,action,**kw):
        status,data,_=self.req('/api/timeline/'+action,dict(name='Safari QA',**kw))
        return status,json.loads(data)
    def test_static_and_entry(self):
        for path in ('/timeline?name=Safari%20QA','/timeline-static/timeline.js','/timeline-static/timeline.css'):
            status,data,_=self.req(path);self.assertEqual(status,200);self.assertGreater(len(data),100)
        self.assertEqual(self.req('/timeline-static/evil.py')[0],404)
    def test_edit_restore_draft_end_to_end(self):
        status,d=self.api('load');self.assertEqual(status,200);p=d['project'];original=p['revision'];c=p['clips'][0]
        status,d=self.api('edit',revision=original,commands=[dict(op='split',id=c['id'],frame=c['start']+10)])
        self.assertEqual(status,200);self.assertEqual(len(d['project']['clips']),4)
        status,d=self.api('restore',revision=d['project']['revision'],restore_revision=original)
        self.assertEqual(status,200);self.assertEqual(len(d['project']['clips']),3)
        revision=d['project']['revision']
        status,d=self.api('render',revision=revision,preset='custom',draft=True);self.assertEqual(status,200)
        self.assertEqual(self.req(d['url'])[0],200)
        self.assertEqual(self.api('load')[1]['project']['revision'],revision)
    def test_stale_tabs_conflict_and_concurrent_autosave(self):
        p=self.api('load')[1]['project'];revision=p['revision'];c=p['clips'][0]
        def change():return self.api('edit',revision=revision,commands=[dict(op='reverse',id=c['id'])])[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:change(),range(2)))
        self.assertEqual(sorted(results),[200,409])
    def test_invalid_batch_no_partial_edit(self):
        p=self.api('load')[1]['project'];c=p['clips'][0]
        status,d=self.api('edit',revision=p['revision'],commands=[dict(op='reverse',id=c['id']),dict(op='properties',id=c['id'],values={'opacity':5})])
        self.assertEqual(status,400);self.assertEqual(self.api('load')[1]['project'],p)
    def test_host_origin_and_header_protection(self):
        self.assertEqual(self.req('/timeline',headers={'Host':'attacker.example'})[0],403)
        self.assertEqual(self.req('/api/timeline/load',dict(name='Safari QA'),headers={'Origin':'https://attacker.example'})[0],403)
        self.assertEqual(self.req('/api/timeline/load',dict(name='Safari QA'),headers={'X-NextPlay':''})[0],400)
    def test_path_secrets_and_ranges(self):
        (self.workspace.root/'.env').write_text('not-a-real-secret')
        self.assertEqual(self.req('/file/.env')[0],403)
        self.assertEqual(self.req('/file/%2e%2e/%2e%2e/etc/passwd')[0],403)
        full=(self.workspace.raw_dir/'synthetic.mp4').read_bytes()
        status,data,h=self.req('/file/raw/synthetic.mp4',headers={'Range':'bytes=0-99'})
        self.assertEqual(status,206);self.assertEqual(data,full[:100]);self.assertEqual(h['Content-Length'],'100')
        status,data,h=self.req('/file/raw/synthetic.mp4',headers={'Range':'bytes=-20'})
        self.assertEqual(status,206);self.assertEqual(data,full[-20:])
        self.assertEqual(self.req('/file/raw/synthetic.mp4',headers={'Range':'bytes=999999999-'})[0],416)
    def test_real_import_and_invalid_format(self):
        revision=self.api('load')[1]['project']['revision'];data=(self.workspace.raw_dir/'synthetic.mp4').read_bytes()
        status,body,_=self.req(f'/api/timeline/import?name=Safari%20QA&revision={revision}&filename=test.mp4&rights=owned',raw=data)
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['asset']['provenance']['rights_status'],'owned')
        revision=json.loads(body)['project']['revision']
        self.assertEqual(self.req(f'/api/timeline/import?name=Safari%20QA&revision={revision}&filename=bad.env',raw=b'bad')[0],400)
    def test_caption_routes_and_direction(self):
        p=self.api('load')[1]['project'];status,d=self.api('captions',revision=p['revision'],text='1\n00:00:00,000 --> 00:00:01,000\nTest caption\n')
        self.assertEqual(status,200);self.assertIn('Test caption',self.api('export-captions')[1]['text'])
        p=d['project'];status,d=self.api('direction',revision=p['revision'],instruction='reverse',selection=p['clips'][0]['id'],playhead=0)
        self.assertEqual(status,200);self.assertEqual(d['provider'],'local')
    def test_oversize_json(self):
        from http.client import HTTPConnection
        connection=HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        connection.putrequest('POST','/api/timeline/edit')
        connection.putheader('X-NextPlay','timeline')
        connection.putheader('Content-Length','1048577')
        connection.endheaders()
        response=connection.getresponse()
        self.assertEqual(response.status,400);response.read();connection.close()

if __name__=='__main__':unittest.main()
