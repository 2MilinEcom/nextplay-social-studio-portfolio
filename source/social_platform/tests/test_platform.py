import hashlib,json,os,tempfile,unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock,patch
from social_platform.store import Store,Conflict,now
from social_platform.publishing import Worker,Blocked
from social_platform.accounts import Accounts
from social_platform.creative import generate

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=Store(self.tmp.name)
        self.media={'path':'video.mp4','sha256':'a','rights_cleared':True,'duration':5}
        self.p=self.store.create('Test','A simple brief',self.media)
        self.p=self.store.edit(self.p['id'],1,{'variants':{'instagram':{'caption':'One step at a time.','hashtags':['Progress']},'tiktok':{'caption':'Keep moving.','privacy':'SELF_ONLY','music_ack':True}}})
    def approve(self,cs=['instagram']):self.p=self.store.approve(self.p['id'],self.p['version'],cs);return self.p
    def enqueue(self):self.p=self.store.enqueue(self.p['id'],self.p['version'],['instagram'],now());return self.store.jobs()[0]
    def test_approval_changes_invalidate_and_cancel(self):
        self.approve();self.enqueue();self.p=self.store.edit(self.p['id'],self.p['version'],{'variants':{'instagram':{'caption':'A new caption'}}})
        self.assertIsNone(self.p['variants']['instagram']['approval']);self.assertEqual(self.store.jobs()[0]['status'],'cancelled')
    def test_stale_save_rejected(self):
        with self.assertRaises(Conflict):self.store.edit(self.p['id'],1,{'title':'Stale'})
    def test_schedule_requires_approval(self):
        with self.assertRaises(ValueError):self.enqueue()
    def test_unknown_rights_block(self):
        self.p=self.store.edit(self.p['id'],self.p['version'],{'media':{**self.media,'rights_cleared':False}})
        with self.assertRaises(ValueError):self.approve()
    def test_native_music_blocks(self):
        self.p=self.store.edit(self.p['id'],self.p['version'],{'music':{'mode':'native','title':'Sound'}})
        with self.assertRaises(ValueError):self.approve()
    def test_tiktok_requires_privacy(self):
        self.p=self.store.edit(self.p['id'],self.p['version'],{'variants':{'tiktok':{'privacy':''}}})
        with self.assertRaises(ValueError):self.approve(['tiktok'])
    def test_future_schedule_and_timezone(self):
        self.approve();d=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat();self.store.enqueue(self.p['id'],self.p['version'],['instagram'],d)
        self.assertIsNone(self.store.claim())
        with self.assertRaises(ValueError):self.store.enqueue(self.p['id'],self.p['version'],['instagram'],'2026-10-14T20:00')
    def test_claim_is_once_and_edit_locked(self):
        self.approve();self.enqueue();self.assertIsNotNone(self.store.claim());self.assertIsNone(self.store.claim())
        with self.assertRaises(Conflict):self.store.edit(self.p['id'],self.p['version'],{'title':'Changed'})
    def test_restart_marks_uncertain_upload(self):
        self.approve();self.enqueue();self.store.claim();s=Store(self.tmp.name)
        self.assertEqual(s.jobs()[0]['status'],'attention')
        with self.assertRaises(ValueError):s.retry(s.jobs()[0]['id'])
    def test_duplicate_publish_prevented(self):
        self.approve();j=self.enqueue();self.store.job_update(j['id'],'published',j['payload']);p=self.store.get(self.p['id'])
        with self.assertRaises(ValueError):self.store.enqueue(p['id'],p['version'],['instagram'],now())
    def test_missing_account_blocks(self):
        self.approve();self.enqueue();fake=Mock();fake.start.side_effect=Blocked('Connect Instagram')
        Worker(self.store,fake).tick();self.assertEqual(self.store.jobs()[0]['status'],'blocked')
    def test_uncertain_network_does_not_retry(self):
        self.approve();self.enqueue();fake=Mock();fake.start.side_effect=TimeoutError()
        w=Worker(self.store,fake);w.tick();w.tick();self.assertEqual(self.store.jobs()[0]['status'],'attention');self.assertEqual(fake.start.call_count,1)
    def test_processing_is_not_published(self):
        self.approve();self.enqueue();fake=Mock();fake.start.return_value={'publish_id':'abc'};fake.poll.return_value=('processing',{'publish_id':'abc'})
        w=Worker(self.store,fake);w.tick();self.assertEqual(self.store.jobs()[0]['status'],'processing')
        fake.poll.return_value=('published',{'publish_id':'abc'});w.tick();self.assertEqual(self.store.jobs()[0]['status'],'published')
    def test_cancel_is_atomic(self):
        self.approve();j=self.enqueue();self.store.claim()
        with self.assertRaises(ValueError):self.store.cancel(j['id'])
    def test_reject_clears_queue(self):
        self.approve();self.enqueue();p=self.store.approve(self.p['id'],self.p['version'],['instagram'],True)
        self.assertEqual(p['variants']['instagram']['status'],'rejected');self.assertEqual(self.store.jobs()[0]['status'],'cancelled')
    def test_other_channel_approval_survives(self):
        self.approve(['instagram','tiktok']);self.p=self.store.edit(self.p['id'],self.p['version'],{'variants':{'instagram':{'caption':'Changed'}}})
        self.assertIsNotNone(self.p['variants']['tiktok']['approval'])
    def test_persistence(self):self.assertEqual(Store(self.tmp.name).get(self.p['id'])['title'],'Test')
    def test_timezone_validation(self):
        with self.assertRaises(ValueError):self.store.save_settings({'name':'My brand','timezone':'made/up'})

class AccountTests(unittest.TestCase):
    def test_encrypted_tokens(self):
        with tempfile.TemporaryDirectory() as root:
            a=Accounts(root,'http://127.0.0.1:8770');a.save({'tiktok':{'token':'test-private-value','id':'x','name':'QA creator'}})
            self.assertNotIn(b'test-private-value',a.path.read_bytes());self.assertEqual(a.get('tiktok')['token'],'test-private-value')
            self.assertNotIn('test-private-value',json.dumps(a.status()))
    def test_oauth_state(self):
        with tempfile.TemporaryDirectory() as root:
            a=Accounts(root,'http://127.0.0.1:8770')
            with self.assertRaises(ValueError):a.callback('tiktok','code','unknown-state')
    def test_missing_app(self):
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{},clear=True):
            a=Accounts(root,'http://127.0.0.1:8770')
            with self.assertRaises(ValueError):a.start('instagram')
            self.assertFalse(a.status()[1]['connected'])

class CreativeTests(unittest.TestCase):
    def test_structured_copy(self):
        p={'title':'A test','brief':'Motivating','media':None};fake=Mock()
        data={c:{'caption':'Keep going.','hook':'One step.','cta':'Follow along.','hashtags':['Progress']} for c in ['tiktok','instagram','facebook']}
        fake.responses.create.return_value.output_text=json.dumps(data);self.assertEqual(generate(p,{},'.',api=fake),data)
        fake.responses.create.return_value.output_text='{"instagram":{}}'
        with self.assertRaises(ValueError):generate(p,{},'.',api=fake)
    def test_failure_preserves_draft(self):
        fake=Mock();fake.responses.create.side_effect=TimeoutError()
        with self.assertRaises(ValueError):generate({'title':'Test','brief':'','media':None},{},'.',api=fake)

if __name__=='__main__':unittest.main()
