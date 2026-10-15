import hashlib,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
from social_platform.publishing import Publishers,Blocked,Worker
from social_platform.store import Store,now

class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.file=Path(self.tmp.name)/'video.mp4';self.file.write_bytes(b'QA-video-bytes')
        self.accounts=Mock();self.accounts.get.return_value={'token':'test-token','id':'account'}
        self.http=Mock();self.pub=Publishers(self.accounts,self.tmp.name,self.http)
        self.p={'media':{'path':'video.mp4','rights_cleared':True,'sha256':hashlib.sha256(self.file.read_bytes()).hexdigest(),'duration':5},'music':None,'variants':{'tiktok':{'caption':'QA post','hashtags':[],'privacy':'SELF_ONLY','allow_comment':False,'allow_duet':False,'allow_stitch':False,'commercial':False,'branded':False,'music_ack':True},'instagram':{'caption':'QA post','hashtags':[]}}}
    def response(self,data,status=200):r=Mock();r.status_code=status;r.json.return_value=data;return r
    def test_tiktok_upload_and_status(self):
        self.http.request.side_effect=[self.response({'data':{'privacy_level_options':['SELF_ONLY'],'max_video_post_duration_sec':60,'comment_disabled':False,'duet_disabled':False,'stitch_disabled':False}}),self.response({'data':{'publish_id':'qa-1','upload_url':'https://open-upload.tiktokapis.com/upload/qa'}})]
        self.http.put.return_value=self.response({},201);persist=Mock();p=self.pub.start(self.p,'tiktok',persist)
        self.assertEqual(p['publish_id'],'qa-1');persist.assert_called_once();self.assertEqual(self.http.put.call_args.kwargs['data'],self.file.read_bytes())
        self.http.request.side_effect=None;self.http.request.return_value=self.response({'data':{'status':'PUBLISH_COMPLETE','publicaly_available_post_id':['42']}})
        self.assertEqual(self.pub.poll('tiktok',p)[0],'published')
    def test_tiktok_invalid_privacy_blocks_before_init(self):
        self.http.request.return_value=self.response({'data':{'privacy_level_options':['PUBLIC_TO_EVERYONE'],'max_video_post_duration_sec':60}})
        with self.assertRaises(Blocked):self.pub.start(self.p,'tiktok',Mock())
        self.assertEqual(self.http.request.call_count,1)
    def test_instagram_requires_hosted_media(self):
        with self.assertRaises(Blocked):self.pub.start(self.p,'instagram',Mock())
        self.http.request.assert_not_called()
    def test_instagram_two_phase_publish(self):
        self.p['media']['public_url']='https://cdn.example/video.mp4';self.http.request.return_value=self.response({'id':'container'})
        p=self.pub.start(self.p,'instagram',Mock());self.assertEqual(p['container_id'],'container')
        self.http.request.return_value=self.response({'status_code':'FINISHED'});self.assertEqual(self.pub.poll('instagram',p)[0],'ready')
        self.http.request.return_value=self.response({'id':'post-1'});self.assertEqual(self.pub.finish_instagram(p)['post_id'],'post-1')
        self.assertEqual(self.pub.poll('instagram',{'phase':'publishing','container_id':'container'})[0],'attention')
    def test_media_mutation_blocks(self):
        self.file.write_bytes(b'changed')
        with self.assertRaises(Blocked):self.pub.media(self.p)
    def test_server_error_is_uncertain(self):
        self.http.request.return_value=self.response({},503)
        with self.assertRaises(RuntimeError):self.pub.req('POST','https://example.com')

if __name__=='__main__':unittest.main()
