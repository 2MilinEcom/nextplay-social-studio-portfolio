import io,json,os,tempfile,threading,unittest,zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import requests
from pipeline_core.timeline_render import run_ffmpeg
from social_platform.server import Platform,Handler

class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.platform=Platform(cls.temp.name,'http://127.0.0.1:0')
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),type('TestHandler',(Handler,),{'platform':cls.platform,'context':cls.platform.context}))
        cls.base='http://127.0.0.1:'+str(cls.server.server_port);cls.platform.origin=cls.base
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.path=Path(cls.temp.name)/'sample.mp4'
        run_ffmpeg(['-f','lavfi','-i','testsrc2=s=180x320:r=30:d=4','-f','lavfi','-i','sine=frequency=220:duration=4','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest','-y',str(cls.path)])
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.temp.cleanup()
    def post(self,action,data):return requests.post(self.base+'/api/marketing/'+action,headers={'X-NextPlay':'marketing'},json=data,timeout=60)
    def test_complete_upload_edit_render_approve_queue_export(self):
        r=requests.post(self.base+'/api/marketing/upload',params={'filename':'sample.mp4','rights':'owned'},headers={'X-NextPlay':'marketing'},data=self.path.read_bytes());self.assertEqual(r.status_code,200,r.text)
        p=r.json()['post'];self.assertEqual(len(p['media']['sha256']),64)
        r=self.post('edit',{'id':p['id'],'version':p['version'],'changes':{'variants':{'instagram':{'caption':'Your next small step.','hashtags':['Momentum']}}}});self.assertEqual(r.status_code,200,r.text);p=r.json()['post']
        ts=requests.post(self.base+'/api/timeline/load',headers={'X-NextPlay':'timeline'},json={'name':p['timeline']['name']}).json()['project'];self.assertEqual(len(ts['clips']),1)
        r=self.post('render',{'id':p['id'],'version':p['version'],'preset':'square'});self.assertEqual(r.status_code,200,r.text);p=r.json()['post'];self.assertTrue(p['media']['rights_cleared']);self.assertEqual(p['media']['width'],1080)
        self.assertEqual(requests.get(self.base+p['media']['url'],headers={'Range':'bytes=0-63'}).status_code,206)
        r=self.post('approve',{'id':p['id'],'version':p['version'],'channels':['instagram']});self.assertEqual(r.status_code,200,r.text);p=r.json()['post']
        from social_platform.store import now
        r=self.post('schedule',{'id':p['id'],'version':p['version'],'channels':['instagram'],'due':now()});self.assertEqual(r.status_code,200,r.text)
        self.platform.worker.tick();self.assertEqual(self.platform.store.jobs()[0]['status'],'blocked')
        r=requests.get(self.base+'/api/marketing/export',params={'id':p['id']});self.assertEqual(r.status_code,200)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:self.assertIn('video.mp4',z.namelist());self.assertIn('instagram-caption.txt',z.namelist());self.assertIn('timeline-revision.json',z.namelist())
    def test_done_renders_prepares_and_preserves_manual_copy(self):
        r=requests.post(self.base+'/api/marketing/upload',params={'filename':'done-test.mp4','rights':'owned'},headers={'X-NextPlay':'marketing'},data=self.path.read_bytes())
        p=r.json()['post'];p=self.platform.store.edit(p['id'],p['version'],{'variants':{'instagram':{'caption':'My hand-written caption.'}}})
        variants={c:{'caption':'Generated '+c,'hook':'Hook','cta':'Follow','hashtags':['QA']} for c in ['instagram','tiktok','facebook']}
        with patch('social_platform.server.generate',return_value=variants):
            r=self.post('finish',{'name':p['timeline']['name'],'revision':p['timeline']['revision']})
        self.assertEqual(r.status_code,200,r.text);result=r.json();finished=result['post']
        self.assertTrue(finished['media']['rendered']);self.assertEqual(finished['variants']['instagram']['caption'],'My hand-written caption.')
        self.assertEqual(finished['variants']['tiktok']['caption'],'Generated tiktok');self.assertEqual(result['url'],'/#studio/'+p['id'])
        self.assertTrue(all(v['approval'] is None for v in finished['variants'].values()))
        r=self.post('finish',{'name':p['timeline']['name'],'revision':p['timeline']['revision']})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['post']['version'],finished['version'])
    def test_done_refuses_stale_editor_revision(self):
        r=requests.post(self.base+'/api/marketing/upload',params={'filename':'stale.mp4','rights':'owned'},headers={'X-NextPlay':'marketing'},data=self.path.read_bytes());p=r.json()['post']
        r=self.post('finish',{'name':p['timeline']['name'],'revision':0});self.assertEqual(r.status_code,409)
    def test_done_button_keeps_original_editor(self):
        html=requests.get(self.base+'/timeline?name=QA-button').text
        self.assertIn('Done → Prepare posts',html);self.assertIn('timeline-static/timeline.js',html);self.assertIn('finish-editor.js',html)

    def test_cross_origin_rejected(self):self.assertEqual(requests.get(self.base+'/api/marketing/state',headers={'Origin':'https://evil.example'}).status_code,403)
    def test_missing_header_rejected(self):self.assertEqual(requests.post(self.base+'/api/marketing/create',json={'title':'Bad'}).status_code,400)
    def test_authentication_boundary(self):
        with patch.dict(os.environ,{'NEXTPLAY_PASSWORD':'test-only-private-password'}):
            self.assertEqual(requests.get(self.base+'/api/marketing/state').status_code,401)
            sess=requests.Session();r=sess.post(self.base+'/auth/login',headers={'X-NextPlay':'marketing'},json={'password':'test-only-private-password'});self.assertEqual(r.status_code,200)
            self.assertIn('HttpOnly',r.headers['Set-Cookie']);self.assertEqual(sess.get(self.base+'/api/marketing/state').status_code,200)
            self.assertEqual(requests.get(self.base+'/timeline').status_code,200);self.assertIn('Sign in',requests.get(self.base+'/timeline').text)
    def test_clients_cannot_manufacture_media(self):
        p=self.platform.store.create('QA')
        r=self.post('edit',{'id':p['id'],'version':p['version'],'changes':{'media':{'path':'../../secret.env','rights_cleared':True}}});self.assertEqual(r.status_code,400)
    def test_path_traversal_rejected(self):self.assertEqual(requests.get(self.base+'/file/%2e%2e/private/credentials.key').status_code,403)
    def test_no_fake_analytics(self):self.assertEqual(self.platform.store.metrics(),[])

if __name__=='__main__':unittest.main()
