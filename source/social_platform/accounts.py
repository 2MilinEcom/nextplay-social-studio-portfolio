"""Encrypted local OAuth credentials. Plaintext never enters API responses or logs."""
import json,os,secrets,time,uuid
from pathlib import Path
from urllib.parse import urlencode
from cryptography.fernet import Fernet
import requests
from .store import CHANNELS

class Accounts:
    def __init__(self,root,origin):
        self.root=Path(root);self.origin=origin.rstrip('/')
        self.root.mkdir(parents=True,exist_ok=True)
        key_path=self.root/'credentials.key'
        if not key_path.exists():
            fd=os.open(key_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as f:f.write(Fernet.generate_key())
        self.cipher=Fernet(key_path.read_bytes());self.path=self.root/'accounts.enc';self.pending={}
    def load(self):
        return json.loads(self.cipher.decrypt(self.path.read_bytes())) if self.path.exists() else {}
    def save(self,data):
        tmp=self.path.with_name(self.path.name+'.'+uuid.uuid4().hex+'.tmp')
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'wb') as f:f.write(self.cipher.encrypt(json.dumps(data).encode()))
        os.replace(tmp,self.path)
    def get(self,c):
        a=self.load().get(c)
        if not a:raise ValueError(f'Connect {c.title()} in Settings first.')
        if a.get('expires',time.time()+1)<=time.time():raise ValueError(f'Reconnect {c.title()}; authorization has expired.')
        return a
    def configured(self,c):
        prefix='TIKTOK' if c=='tiktok' else 'INSTAGRAM' if c=='instagram' else 'META'
        return bool(os.getenv(prefix+'_CLIENT_ID') and os.getenv(prefix+'_CLIENT_SECRET'))
    def status(self):
        data=self.load()
        return [{'channel':c,'connected':c in data and data[c].get('expires',time.time()+1)>time.time(),'name':data.get(c,{}).get('name',''),'configured':self.configured(c),'expires':data.get(c,{}).get('expires')} for c in CHANNELS]
    def disconnect(self,c):
        if c not in CHANNELS:raise ValueError('Unknown channel.')
        data=self.load();data.pop(c,None);self.save(data)
    def start(self,c):
        if c not in CHANNELS:raise ValueError('Unknown channel.')
        if not self.configured(c):raise ValueError(f'{c.title()} needs a registered developer app before accounts can be connected. See the connection guide in Settings.')
        state=secrets.token_urlsafe(32);self.pending[state]={'channel':c,'expires':time.time()+600}
        redirect=self.origin+'/oauth/callback/'+c
        if c=='tiktok':
            return 'https://www.tiktok.com/v2/auth/authorize/?'+urlencode({'client_key':os.environ['TIKTOK_CLIENT_ID'],'scope':'user.info.basic,video.publish,video.list','response_type':'code','redirect_uri':redirect,'state':state})
        if c=='instagram':
            return 'https://www.instagram.com/oauth/authorize?'+urlencode({'client_id':os.environ['INSTAGRAM_CLIENT_ID'],'scope':'instagram_business_basic,instagram_business_content_publish,instagram_business_manage_insights','response_type':'code','redirect_uri':redirect,'state':state,'enable_fb_login':'0','force_authentication':'1'})
        return 'https://www.facebook.com/'+os.getenv('META_API_VERSION','v25.0')+'/dialog/oauth?'+urlencode({'client_id':os.environ['META_CLIENT_ID'],'scope':'pages_show_list,pages_read_engagement,pages_manage_posts,read_insights','response_type':'code','redirect_uri':redirect,'state':state})
    def callback(self,c,code,state):
        pending=self.pending.pop(state,None)
        if not pending or pending['channel']!=c or pending['expires']<time.time():raise ValueError('Connection session expired. Try connecting again.')
        redirect=self.origin+'/oauth/callback/'+c
        try:
            if c=='tiktok':
                r=requests.post('https://open.tiktokapis.com/v2/oauth/token/',data={'client_key':os.environ['TIKTOK_CLIENT_ID'],'client_secret':os.environ['TIKTOK_CLIENT_SECRET'],'code':code,'grant_type':'authorization_code','redirect_uri':redirect},timeout=30)
                r.raise_for_status();t=r.json()
                account={'token':t['access_token'],'refresh':t.get('refresh_token'),'id':t['open_id'],'name':'TikTok creator','expires':time.time()+t['expires_in']}
            elif c=='instagram':
                r=requests.post('https://api.instagram.com/oauth/access_token',data={'client_id':os.environ['INSTAGRAM_CLIENT_ID'],'client_secret':os.environ['INSTAGRAM_CLIENT_SECRET'],'code':code,'grant_type':'authorization_code','redirect_uri':redirect},timeout=30)
                r.raise_for_status();short=r.json()
                r=requests.get('https://graph.instagram.com/access_token',params={'grant_type':'ig_exchange_token','client_secret':os.environ['INSTAGRAM_CLIENT_SECRET'],'access_token':short['access_token']},timeout=30)
                r.raise_for_status();t=r.json()
                account={'token':t['access_token'],'id':str(short['user_id']),'name':'Instagram professional account','expires':time.time()+t.get('expires_in',3600)}
            else:
                base='https://graph.facebook.com/'+os.getenv('META_API_VERSION','v25.0')
                r=requests.get(base+'/oauth/access_token',params={'client_id':os.environ['META_CLIENT_ID'],'client_secret':os.environ['META_CLIENT_SECRET'],'code':code,'redirect_uri':redirect},timeout=30)
                r.raise_for_status();t=r.json()
                r=requests.get(base+'/me/accounts',params={'access_token':t['access_token'],'fields':'id,name,access_token'},timeout=30);r.raise_for_status();pages=r.json().get('data',[])
                if len(pages)!=1:raise ValueError('Authorize exactly one Facebook Page for this installation, then reconnect.')
                page=pages[0];account={'token':page['access_token'],'id':page['id'],'name':page['name'],'expires':time.time()+t.get('expires_in',3600)}
            data=self.load();data[c]=account;self.save(data)
            return account['name']
        except ValueError:raise
        except Exception:raise ValueError('Account connection failed. Check app permissions and registered redirect URLs.') from None
