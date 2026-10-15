"""Official publishing adapters. Never interpret an accepted upload as a published post."""
from __future__ import annotations
import json,os,threading,time
from pathlib import Path
from urllib.parse import urlparse
import requests
from .store import now

class Blocked(ValueError):pass
class ProviderRejected(ValueError):pass

class Publishers:
    def __init__(self,accounts,root,http=None):self.accounts=accounts;self.root=Path(root).resolve();self.http=http or requests.Session();self.bridge=None
    def req(self,method,url,**kw):
        r=self.http.request(method,url,timeout=60,**kw)
        if r.status_code>=500:raise RuntimeError('Provider response is uncertain.')
        if r.status_code>=400:
            # Provider bodies can echo credentials; expose only safe status metadata.
            raise ProviderRejected(f'Provider rejected the request (HTTP {r.status_code}). Check account permissions and media requirements.')
        data=r.json()
        err=data.get('error')
        if err and isinstance(err,dict) and err.get('code') not in (None,'ok',0):
            raise ProviderRejected('Provider rejected the request. Review account permissions and content eligibility.')
        return data
    def media(self,p):
        if not p.get('media'):raise Blocked('Add rendered media first.')
        path=(self.root/p['media']['path']).resolve()
        try:path.relative_to(self.root)
        except ValueError:raise Blocked('Media is outside the workspace.') from None
        if not path.is_file():raise Blocked('Media is missing. Reimport or render the video.')
        if not p['media'].get('rights_cleared'):raise Blocked('Media rights have not been confirmed.')
        import hashlib
        with path.open('rb') as f:current=hashlib.file_digest(f,'sha256').hexdigest()
        if current!=p['media'].get('sha256'):raise Blocked('Video changed after approval. Reimport and approve it again.')
        if path.suffix.lower()!='.mp4':raise Blocked('Render an MP4 in the editor before publishing.')
        return path
    def caption(self,v):return (v['caption']+'\n\n'+' '.join('#'+h.lstrip('#') for h in v['hashtags'])).strip()
    def creator(self):
        if self.bridge and self.bridge.configured():return self.bridge.creator()
        try:a=self.accounts.get('tiktok')
        except ValueError as e:raise Blocked(str(e)) from None
        return self.req('POST','https://open.tiktokapis.com/v2/post/publish/creator_info/query/',headers={'Authorization':'Bearer '+a['token']},json={})['data']
    def start(self,p,c,persist):
        if self.bridge and self.bridge.configured():
            path=self.media(p)
            if p.get('music') and p['music'].get('mode')=='native':raise Blocked('Native music requires an in-app finish.')
            return self.bridge.start(p,c,path,self.caption(p['variants'][c]),persist)
        try:a=self.accounts.get(c)
        except ValueError as e:raise Blocked(str(e)) from None
        if '_publish_destination' in p and p['_publish_destination']!=a['id']:raise Blocked('Confirm the connected destination account before publishing.')
        path=self.media(p);v=p['variants'][c];caption=self.caption(v)
        if p.get('music') and p['music'].get('mode')=='native':raise Blocked('Native music requires finishing in the platform app.')
        if c=='tiktok':
            creator=self.creator()
            if v['privacy'] not in creator['privacy_level_options']:raise Blocked('Refresh TikTok options and choose an available privacy level.')
            if p['media'].get('duration',0)>creator['max_video_post_duration_sec']:raise Blocked('Video exceeds this creator’s duration limit.')
            if not v['music_ack']:raise Blocked('Confirm music usage before publishing.')
            size=path.stat().st_size
            chunk=min(size,10*1024*1024);count=max(1,size//chunk)
            data=self.req('POST','https://open.tiktokapis.com/v2/post/publish/video/init/',headers={'Authorization':'Bearer '+a['token']},json={'post_info':{'title':caption,'privacy_level':v['privacy'],'disable_comment':not v['allow_comment'] or creator['comment_disabled'],'disable_duet':not v['allow_duet'] or creator['duet_disabled'],'disable_stitch':not v['allow_stitch'] or creator['stitch_disabled'],'brand_content_toggle':v['branded'],'brand_organic_toggle':v['commercial']},'source_info':{'source':'FILE_UPLOAD','video_size':size,'chunk_size':chunk,'total_chunk_count':count}})['data']
            provider={'publish_id':data['publish_id'],'phase':'upload'};persist(provider)
            u=urlparse(data['upload_url'])
            if u.scheme!='https' or not (u.hostname or '').endswith('.tiktokapis.com'):raise Blocked('Unexpected TikTok upload host. Review the provider response before uploading.')
            offset=0
            with path.open('rb') as f:
                for i in range(count):
                    data_chunk=f.read(size-offset if i==count-1 else chunk)
                    r=self.http.put(data['upload_url'],headers={'Content-Type':'video/mp4','Content-Length':str(len(data_chunk)),'Content-Range':f'bytes {offset}-{offset+len(data_chunk)-1}/{size}'},data=data_chunk,timeout=120)
                    if r.status_code not in (200,201,206):raise ProviderRejected(f'TikTok upload failed (HTTP {r.status_code}).')
                    offset+=len(data_chunk)
            provider['phase']='processing';return provider
        if c=='instagram':
            public=p['media'].get('public_url')
            if not public or urlparse(public).scheme!='https':raise Blocked('Instagram needs an HTTPS video URL reachable by Meta. Set it in the post’s publishing settings.')
            base='https://graph.instagram.com/'+os.getenv('META_API_VERSION','v25.0')
            data=self.req('POST',base+'/'+a['id']+'/media',headers={'Authorization':'Bearer '+a['token']},data={'media_type':'REELS','video_url':public,'caption':caption,'share_to_feed':'true'})
            provider={'container_id':data['id'],'phase':'processing'};persist(provider);return provider
        if c=='facebook':
            m=p['media']
            if not 4<=m.get('duration',0)<=60 or m.get('width',0)<540 or m.get('height',0)<960 or abs(m.get('width',0)/max(1,m.get('height',0))-9/16)>.01:raise Blocked('Facebook Reels needs a portrait 9:16 video, at least 540 × 960, lasting 4–60 seconds. Render a compatible version in the editor.')
            base='https://graph.facebook.com/'+os.getenv('META_API_VERSION','v25.0')
            data=self.req('POST',base+'/'+a['id']+'/video_reels',headers={'Authorization':'Bearer '+a['token']},data={'upload_phase':'start'})
            provider={'video_id':data['video_id'],'phase':'upload'};persist(provider)
            u=urlparse(data['upload_url'])
            if u.scheme!='https' or u.hostname!='rupload.facebook.com':raise Blocked('Unexpected Facebook upload host.')
            with path.open('rb') as f:
                r=self.http.post(data['upload_url'],headers={'Authorization':'OAuth '+a['token'],'offset':'0','file_size':str(path.stat().st_size)},data=f,timeout=180)
            if r.status_code>=400:raise ProviderRejected('Facebook video upload was rejected.')
            self.req('POST',base+'/'+a['id']+'/video_reels',headers={'Authorization':'Bearer '+a['token']},data={'upload_phase':'finish','video_id':data['video_id'],'video_state':'PUBLISHED','description':caption})
            provider['phase']='processing';return provider
        raise Blocked('Unknown social channel.')
    def poll(self,c,provider):
        if provider.get('service')=='upload-post':return self.bridge.poll(c,provider)
        a=self.accounts.get(c)
        if c=='tiktok':
            data=self.req('POST','https://open.tiktokapis.com/v2/post/publish/status/fetch/',headers={'Authorization':'Bearer '+a['token']},json={'publish_id':provider['publish_id']})['data']
            if data['status']=='PUBLISH_COMPLETE':return 'published',{**provider,'post_ids':data.get('publicaly_available_post_id',[])}
            if data['status']=='FAILED':return 'failed',{**provider,'reason':str(data.get('fail_reason','Provider failed'))[:200]}
            return 'processing',provider
        if c=='instagram':
            base='https://graph.instagram.com/'+os.getenv('META_API_VERSION','v25.0')
            if provider.get('phase')=='publishing':return 'attention',provider
            data=self.req('GET',base+'/'+provider['container_id'],headers={'Authorization':'Bearer '+a['token']},params={'fields':'status_code'})
            status=data.get('status_code')
            if status in ('ERROR','EXPIRED'):return 'failed',provider
            if status=='FINISHED':
                # Caller must persist this intent before the irreversible publish request.
                return 'ready',provider
            if status=='PUBLISHED':return 'published',provider
            return 'processing',provider
        base='https://graph.facebook.com/'+os.getenv('META_API_VERSION','v25.0')
        data=self.req('GET',base+'/'+provider['video_id'],headers={'Authorization':'Bearer '+a['token']},params={'fields':'status'})
        status=data.get('status',{})
        if status.get('publishing_phase',{}).get('status')=='complete':return 'published',provider
        if status.get('video_status')=='error':return 'failed',provider
        return 'processing',provider
    def finish_instagram(self,provider):
        a=self.accounts.get('instagram');base='https://graph.instagram.com/'+os.getenv('META_API_VERSION','v25.0')
        data=self.req('POST',base+'/'+a['id']+'/media_publish',headers={'Authorization':'Bearer '+a['token']},data={'creation_id':provider['container_id']})
        return {**provider,'post_id':data['id'],'phase':'published'}
    def analytics(self,c,ids):
        a=self.accounts.get(c)
        if c=='tiktok':
            data=self.req('POST','https://open.tiktokapis.com/v2/video/query/',headers={'Authorization':'Bearer '+a['token']},params={'fields':'id,title,view_count,like_count,comment_count,share_count'},json={'filters':{'video_ids':[str(i) for i in ids]}})
            return data.get('data',{}).get('videos',[])
        base=('https://graph.instagram.com/' if c=='instagram' else 'https://graph.facebook.com/')+os.getenv('META_API_VERSION','v25.0')
        out=[]
        for pid in ids:
            metrics='views,likes,comments,shares,saved' if c=='instagram' else 'total_video_views'
            data=self.req('GET',base+'/'+str(pid)+'/insights',headers={'Authorization':'Bearer '+a['token']},params={'metric':metrics})
            out.append({'id':str(pid),'metrics':data.get('data',[])})
        return out

class Worker:
    def __init__(self,store,publishers):self.store=store;self.publishers=publishers;self.stop=threading.Event();self.thread=None
    def start(self):
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def tick(self):
        job=self.store.claim()
        if job:
            payload=job['payload'];payload['attempts']+=1
            def persist(provider):
                payload['provider']=provider
                self.store.job_update(job['id'],'sending',payload)
            try:
                provider=self.publishers.start({**payload['post'],'_publish_destination':payload.get('destination')},job['channel'],persist)
                payload['provider']=provider;payload['error']=''
                self.store.job_update(job['id'],'processing',payload)
            except Blocked as e:
                payload['error']=str(e);self.store.job_update(job['id'],'blocked' if not payload['provider'] else 'attention',payload)
            except ProviderRejected as e:
                payload['error']=str(e);self.store.job_update(job['id'],'failed',payload)
            except Exception:
                payload['error']='Publishing outcome is uncertain. Check the platform before retrying to avoid a duplicate.'
                self.store.job_update(job['id'],'attention',payload)
        for j in self.store.jobs():
            if j['status']!='processing':continue
            payload=j['payload']
            try:
                status,provider=self.publishers.poll(j['channel'],payload['provider'])
                payload['provider']=provider
                if status=='ready':
                    provider['phase']='publishing';self.store.job_update(j['id'],'sending',payload)
                    payload['provider']=self.publishers.finish_instagram(provider);status='published'
                if status=='failed':payload['error']='Provider processing failed. Review the media before retrying.'
                self.store.job_update(j['id'],status,payload)
            except Exception:
                if payload['provider'].get('phase')=='publishing':
                    payload['error']='Instagram publishing outcome is uncertain. Reconcile before retrying.'
                    self.store.job_update(j['id'],'attention',payload)
                # A transient read failure preserves processing; no duplicate writes.
    def run(self):
        while not self.stop.is_set():
            try:self.tick()
            except Exception:pass
            self.stop.wait(20)
