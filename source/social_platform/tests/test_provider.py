import tempfile,unittest,hashlib
from pathlib import Path
from unittest.mock import Mock
from social_platform.accounts import Accounts
from social_platform.provider import UploadPost
from social_platform.publishing import Blocked

class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.accounts=Accounts(self.temp.name,'http://127.0.0.1:8770');self.accounts.save({'_upload_post':{'key':'synthetic-provider-key','profile':'qa-profile'}})
        self.http=Mock();self.bridge=UploadPost(self.accounts,self.http)
        self.profile={'username':'qa-profile','social_accounts':{'instagram':{'username':'123','handle':'qa','display_name':'QA brand'},'tiktok':'','facebook':None}}
    def response(self,data,status=200):r=Mock();r.status_code=status;r.json.return_value=data;return r
    def test_profile_and_placeholders(self):
        self.http.request.return_value=self.response({'success':True,'profile':self.profile});status=self.bridge.status()
        self.assertTrue(status[1]['connected']);self.assertFalse(status[0]['connected']);self.assertFalse(status[2]['connected'])
        self.assertNotIn('synthetic-provider-key',str(status))
    def test_expired_connection(self):
        self.profile['social_accounts']['instagram']['reauth_required']=True
        self.http.request.return_value=self.response({'success':True,'profile':self.profile});self.assertFalse(self.bridge.status()[1]['connected'])
    def test_secure_connection_link(self):
        self.http.request.return_value=self.response({'success':True,'access_url':'https://app.upload-post.com/connect?token=test-only'})
        self.assertTrue(self.bridge.connect('instagram').startswith('https://app.upload-post.com'))
        self.http.request.return_value=self.response({'success':True,'access_url':'https://evil.example/connect'})
        with self.assertRaises(ValueError):self.bridge.connect('instagram')
    def test_failed_setup_preserves_old_credentials(self):
        self.http.request.return_value=self.response({},401)
        with self.assertRaises(ValueError):self.bridge.configure('invalid','qa-profile')
        self.assertEqual(self.accounts.load()['_upload_post']['key'],'synthetic-provider-key')
    def test_account_mismatch_blocks_before_upload(self):
        self.http.request.return_value=self.response({'success':True,'profile':self.profile})
        with self.assertRaises(Blocked):self.bridge.start({'_publish_destination':'other'},'instagram',Path('unused'),'caption',Mock())
        self.assertEqual(self.http.request.call_count,1)
    def test_async_upload_and_idempotency(self):
        self.http.request.side_effect=[self.response({'success':True,'profile':self.profile}),self.response({'success':True})]
        path=Path(self.temp.name)/'test.mp4';path.write_bytes(b'test-only')
        p={'_publish_destination':'123','variants':{'instagram':{}}};persist=Mock()
        result=self.bridge.start(p,'instagram',path,'Test caption',persist)
        self.assertEqual(result['phase'],'processing');persist.assert_called_once()
        kw=self.http.request.call_args.kwargs;self.assertEqual(kw['data']['title'],'Test caption')
        self.assertEqual(kw['headers']['Idempotency-Key'],result['request_id'])
    def test_publication_confirmation(self):
        self.http.request.return_value=self.response({'status':'completed','results':[{'platform':'instagram','success':True,'post_id':'42'}]})
        status,p=self.bridge.poll('instagram',{'request_id':'qa'});self.assertEqual(status,'published');self.assertEqual(p['post_id'],'42')
    def test_provider_metrics_without_invented_counts(self):
        self.http.request.return_value=self.response({'success':True,'platforms':{'instagram':{'platform_post_id':'42','post_metrics':{'views':17,'likes':3}}}})
        items=self.bridge.analytics('instagram',{'request_id':'qa'})
        self.assertEqual(items[0]['id'],'42');self.assertEqual(len(items[0]['metrics']),2)
        self.http.request.return_value=self.response({'success':True,'platforms':{'instagram':{'post_metrics':None}}})
        self.assertEqual(self.bridge.analytics('instagram',{'request_id':'qa'}),[])
    def test_tiktok_inbox_is_not_published(self):
        self.http.request.return_value=self.response({'status':'completed','results':[{'platform':'tiktok','success':True,'fallback_to_inbox':True}]})
        self.assertEqual(self.bridge.poll('tiktok',{'request_id':'qa'})[0],'attention')
    def test_skipped_channel_is_not_published(self):
        self.http.request.return_value=self.response({'status':'completed','results':[{'platform':'instagram','skipped':True}]})
        self.assertEqual(self.bridge.poll('instagram',{'request_id':'qa'})[0],'failed')
    def test_empty_completion_needs_attention(self):
        self.http.request.return_value=self.response({'status':'completed','results':[]})
        self.assertEqual(self.bridge.poll('instagram',{'request_id':'qa'})[0],'attention')

if __name__=='__main__':unittest.main()
