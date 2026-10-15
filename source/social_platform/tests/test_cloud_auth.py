import os,tempfile,threading,time,unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import requests
from social_platform.auth import invite,valid_invite
from social_platform.server import Platform,Handler

class CloudAuthTests(unittest.TestCase):
    def test_expired_and_tampered_invites(self):
        token=invite('secret')
        self.assertTrue(valid_invite(token,'secret'))
        self.assertFalse(valid_invite(token,'wrong'))
        self.assertFalse(valid_invite(invite('secret',time.time()-1),'secret'))

    def test_cloud_owner_setup_and_health(self):
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'NEXTPLAY_PASSWORD':'bootstrap-test-only-password'}):
            platform=Platform(root,'http://127.0.0.1:0')
            server=ThreadingHTTPServer(('127.0.0.1',0),type('TestHandler',(Handler,),{'platform':platform,'context':platform.context}))
            base='http://127.0.0.1:'+str(server.server_port);platform.origin=base
            threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                self.assertEqual(requests.get(base+'/health',headers={'Host':'internal.railway'}).status_code,200)
                self.assertEqual(requests.get(base+'/api/marketing/state').status_code,401)
                token=invite(os.environ['NEXTPLAY_PASSWORD']);session=requests.Session()
                response=session.get(base+'/auth/link',params={'token':token},allow_redirects=False)
                self.assertEqual(response.status_code,303);self.assertIn('HttpOnly',response.headers['Set-Cookie'])
                self.assertEqual(requests.get(base+'/auth/link',params={'token':token}).status_code,401)
                headers={'X-NextPlay':'marketing'}
                self.assertEqual(session.post(base+'/api/marketing/workspace-password',headers=headers,json={'password':'short'}).status_code,400)
                self.assertEqual(session.post(base+'/api/marketing/workspace-password',headers=headers,json={'password':'permanent-test-only-password'}).status_code,200)
                self.assertFalse('permanent-test-only-password' in str(platform.accounts.load()))
                self.assertEqual(requests.get(base+'/auth/link',params={'token':invite(os.environ['NEXTPLAY_PASSWORD'])}).status_code,401)
                fresh=requests.Session()
                self.assertEqual(fresh.post(base+'/auth/login',headers=headers,json={'password':os.environ['NEXTPLAY_PASSWORD']}).status_code,401)
                self.assertEqual(fresh.post(base+'/auth/login',headers=headers,json={'password':'permanent-test-only-password'}).status_code,200)
                self.assertEqual(fresh.post(base+'/api/marketing/workspace-password',headers=headers,json={'password':'another-test-only-password','current':'wrong'}).status_code,400)
                self.assertEqual(fresh.get(base+'/api/marketing/state').status_code,200)
            finally:server.shutdown();server.server_close()
