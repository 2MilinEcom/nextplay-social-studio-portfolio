"""Optional Upload-Post bridge: hosted authorization and asynchronous publishing.
No account is created or subscription purchased by importing this module.
"""
import os,time,secrets
from urllib.parse import quote,urlparse
import requests
from .store import CHANNELS
from .publishing import Blocked,ProviderRejected

class UploadPost:
    BASE='https://api.upload-post.com'
    def __init__(self,accounts,http=None):self.accounts=accounts;self.http=http or requests.Session();self.cached=None;self.cached_at=0
    def config(self):
        saved=self.accounts.load().get('_upload_post',{})
        return {'key':os.getenv('UPLOAD_POST_API_KEY') or saved.get('key',''),'profile':os.getenv('UPLOAD_POST_PROFILE') or saved.get('profile','')}
    def configured(self):return bool(self.config()['key'] and self.config()['profile'])
    def request(self,method,path,**kw):
        key=self.config()['key']
        if not key:raise Blocked('Sign in to Upload-Post and finish connecting the publishing service first.')
        r=self.http.request(method,self.BASE+path,headers={'Authorization':'Apikey '+key,**kw.pop('headers',{})},timeout=kw.pop('timeout',30),**kw)
        if r.status_code>=500:raise RuntimeError('Publishing service response is uncertain.')
        if r.status_code>=400:raise ProviderRejected(f'Publishing service rejected the request (HTTP {r.status_code}). Check account access and plan limits.')
        data=r.json()
        if data.get('success') is False:raise ProviderRejected('Publishing service could not complete the request.')
        return data
    def profile(self,force=False):
        if self.cached is not None and not force and time.time()-self.cached_at<30:return self.cached
        cfg=self.config()
        if not cfg['profile']:raise Blocked('Choose a publishing profile first.')
        data=self.request('GET','/api/uploadposts/users/'+quote(cfg['profile'],safe=''))
        profile=data.get('profile') or data.get('user') or data
        if profile.get('username')!=cfg['profile'] or not isinstance(profile.get('social_accounts'),dict):raise Blocked('Publishing service returned an unexpected profile. Check the selected profile.')
        self.cached=profile;self.cached_at=time.time();return profile
    def status(self):
        if not self.configured():return None
        try:
            p=self.profile();social=p['social_accounts']
            return [{'channel':c,'configured':True,'connected':isinstance(social.get(c),dict) and bool(social[c].get('username')) and not social[c].get('reauth_required',False),'name':(social.get(c) or {}).get('display_name') or (social.get(c) or {}).get('handle') or c.title(),'destination':(social.get(c) or {}).get('username'),'provider':'upload-post','capabilities':(social.get(c) or {}).get('capabilities',[])} for c in CHANNELS]
        except Exception:
            return [{'channel':c,'configured':True,'connected':False,'name':'','destination':None,'provider':'upload-post','error':'Unable to verify connection. Refresh or reconnect the publishing service.'} for c in CHANNELS]
    def configure(self,key,profile=''):
        # Explicit setup action. Validate before persisting, then pick or create a profile.
        saved=self.accounts.load();prior=saved.get('_upload_post')
        saved['_upload_post']={'key':key.strip(),'profile':profile.strip()};self.accounts.save(saved)
        try:
            self.request('GET','/api/uploadposts/me')
            profiles=self.request('GET','/api/uploadposts/users').get('profiles',[])
            if not profile:
                if len(profiles)==1:profile=profiles[0]['username']
                elif not profiles:
                    profile='nextplay-'+secrets.token_hex(6);self.request('POST','/api/uploadposts/users',json={'username':profile})
                else:raise ValueError('Your publishing service has multiple profiles. Enter the profile you want NextPlay to use.')
            elif not any(p['username']==profile for p in profiles):raise ValueError('Choose an existing publishing profile.')
            saved=self.accounts.load();saved['_upload_post']={'key':key.strip(),'profile':profile};self.accounts.save(saved);self.cached=None
            self.profile(True)
        except Exception:
            saved=self.accounts.load()
            if prior:saved['_upload_post']=prior
            else:saved.pop('_upload_post',None)
            self.accounts.save(saved);raise
        return {'configured':True,'profile':profile}
    def connect(self,channel):
        if channel not in CHANNELS:raise ValueError('Unknown channel.')
        data=self.request('POST','/api/uploadposts/users/generate-jwt',json={'username':self.config()['profile'],'redirect_url':self.accounts.origin+'/#settings','redirect_button_text':'Back to NextPlay','connect_title':'Connect your NextPlay channels','platforms':[channel],'show_calendar':False})
        url=data.get('access_url','');p=urlparse(url)
        if p.scheme!='https' or p.hostname!='app.upload-post.com':raise ValueError('Unexpected account connection destination.')
        return url
    def disconnect(self,channel):
        self.request('DELETE','/api/uploadposts/users/social',json={'profile_username':self.config()['profile'],'social_platform':channel});self.cached=None
    def creator(self):
        p=self.profile(True);account=p['social_accounts'].get('tiktok') or {}
        if not isinstance(account,dict) or not account.get('username'):raise Blocked('Connect TikTok first.')
        data=self.request('GET','/api/uploadposts/tiktok/settings',params={'profile':self.config()['profile']})
        if not isinstance(data.get('privacy_level_options'),list):raise Blocked('Unable to check TikTok publishing preferences.')
        return {**data,'creator_nickname':account.get('display_name') or account.get('handle') or 'TikTok creator'}

    def start(self,p,c,path,caption,persist):
        profile=self.profile(True);dest=profile['social_accounts'].get(c)
        if not isinstance(dest,dict) or not dest.get('username') or dest.get('reauth_required'):raise Blocked('Connect '+c.title()+' before publishing.')
        expected=p.get('_publish_destination')
        if expected!=dest['username']:raise Blocked('Confirm this connected account in the publishing queue before sending the post.')
        v=p['variants'][c];data={'user':self.config()['profile'],'platform[]':c,'title':caption,'async_upload':'true','request_id':secrets.token_hex(16)}
        if c=='tiktok':
            capabilities=dest.get('capabilities',[])
            if not v['music_ack']:raise Blocked('Confirm TikTok music usage before publishing.')
            settings=self.creator()
            if v['privacy'] not in settings['privacy_level_options']:raise Blocked('Refresh your TikTok privacy choices before publishing.')
            if p['media'].get('duration',0)>settings.get('max_video_post_duration_sec',0):raise Blocked('Video exceeds this TikTok account’s duration limit.')
            data.update({'privacy_level':v['privacy'],'disable_comment':str(not v['allow_comment']).lower(),'disable_duet':str(not v['allow_duet']).lower(),'disable_stitch':str(not v['allow_stitch']).lower(),'brand_content_toggle':str(v['branded']).lower(),'brand_organic_toggle':str(v['commercial']).lower()})
        provider={'service':'upload-post','request_id':data['request_id'],'channel':c,'destination':dest['username'],'phase':'upload'};persist(provider)
        with path.open('rb') as f:r=self.request('POST','/api/upload',data=data,files={'video':(path.name,f,'video/mp4')},headers={'Idempotency-Key':data['request_id']},timeout=180)
        if r.get('request_id') and r['request_id']!=provider['request_id']:provider['request_id']=r['request_id']
        provider['phase']='processing';return provider
    def analytics(self,c,provider):
        data=self.request('GET','/api/uploadposts/post-analytics/'+quote(provider['request_id'],safe=''),params={'platform':c})
        item=(data.get('platforms') or {}).get(c) or {}
        metrics=item.get('post_metrics')
        if not isinstance(metrics,dict) or not metrics:return []
        normalized=[]
        aliases={'view_count':'views','like_count':'likes','comment_count':'comments','share_count':'shares'}
        for k,v in metrics.items():
            if isinstance(v,(int,float)) and not isinstance(v,bool):normalized.append({'name':aliases.get(k,k),'values':[{'value':v}]})
        return [{'id':str(item.get('platform_post_id') or provider.get('post_id') or provider['request_id']),'metrics':normalized}]

    def poll(self,c,provider):
        data=self.request('GET','/api/uploadposts/status',params={'request_id':provider['request_id']})
        if data.get('status')=='failed':return 'failed',provider
        if data.get('status')=='not_found':return 'attention',provider
        if data.get('status')!='completed':return 'processing',provider
        results=data.get('results',[])
        result=next((r for r in results if r.get('platform')==c),None)
        if not result:return 'attention',provider
        if result.get('success') is not True:return 'failed',provider
        # A platform inbox fallback/draft is not a confirmed public publication.
        if result.get('post_mode')=='MEDIA_UPLOAD' or result.get('is_draft') or result.get('fallback_to_inbox'):return 'attention',{**provider,'phase':'native-finish'}
        return 'published',{**provider,'post_id':str(result.get('post_id') or result.get('id') or ''),'post_url':str(result.get('url') or result.get('post_url') or ''),'phase':'published'}
