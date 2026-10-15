from __future__ import annotations
import argparse,hashlib,hmac,io,json,mimetypes,os,secrets,threading,time,zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs,quote,urlparse
from pipeline_core.dashboard import DashboardContext,_DashboardHandler
from pipeline_core.models import Product
from pipeline_core.storage import ProductWorkspace,load_product_workspace
from pipeline_core.timeline import fps_value,make_clip,new_project
from pipeline_core.timeline_store import TimelineStore,digest
from pipeline_core.timeline_render import run_ffmpeg
from .store import Store,Conflict,CHANNELS,now
from .accounts import Accounts
from .publishing import Publishers,Worker
from .creative import generate,research_music
from .provider import UploadPost
from .auth import matches,password_record,valid_invite

STATIC=Path(__file__).parent/'static'
LOGIN='''<!doctype html><html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · NextPlay</title><link rel="stylesheet" href="/platform-static/app.css"><body><main class="login"><div class="brand"><span class="brandmark">▷</span> NextPlay</div><h1>Your creative space.</h1><p>Sign in to your private marketing dashboard.</p><form id="login"><label>Password<input id="password" type="password" required autocomplete="current-password"></label><button class="primary">Sign in →</button><p id="error" role="alert"></p></form></main><script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();let r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json','X-NextPlay':'marketing'},body:JSON.stringify({password:document.querySelector('#password').value})});if(r.ok)location.href='/';else document.querySelector('#error').textContent='Unable to sign in. Check your password or try again later.'}</script></body></html>'''

class Platform:
    def __init__(self,data,origin,workspace=None):
        self.data=Path(data).resolve();self.data.mkdir(parents=True,exist_ok=True)
        if workspace:self.product,self.workspace=load_product_workspace(Path(workspace).resolve())
        else:
            self.product=Product('local:nextplay','studio','NextPlay Studio')
            self.workspace=ProductWorkspace(self.data/'products',self.product).ensure()
            if not self.workspace.product_json.exists():self.workspace.save_product(self.product)
        self.context=DashboardContext(self.product,self.workspace)
        self.store=Store(self.data/'marketing');self.accounts=Accounts(self.data/'private',origin)
        saved_key=self.accounts.load().get('_openai',{}).get('key')
        if saved_key and not os.getenv('OPENAI_API_KEY'):os.environ['OPENAI_API_KEY']=saved_key
        self.bridge=UploadPost(self.accounts)
        self.publishers=Publishers(self.accounts,self.workspace.root);self.publishers.bridge=self.bridge;self.worker=Worker(self.store,self.publishers)
        self.origin=origin.rstrip('/');self.sessions={};self.login_failures={};self.auth_lock=threading.Lock();self.used_invites=set()
    def media_record(self,path,rights=False,revision=None):
        root=self.workspace.root.resolve();path=Path(path).resolve();path.relative_to(root)
        from ffmpeg_bin import FFMPEG
        import subprocess,re
        probe=subprocess.run([FFMPEG,'-hide_banner','-i',str(path)],capture_output=True,text=True,timeout=20).stderr
        m=re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)',probe)
        duration=int(m[1])*3600+int(m[2])*60+float(m[3]) if m else 0
        dim=re.search(r'Video:.*?\b(\d{2,5})x(\d{2,5})\b',probe)
        rel=path.relative_to(root).as_posix()
        poster=path.with_suffix('.poster.jpg')
        try:
            run_ffmpeg(['-i',str(path),'-vf','scale=480:-2','-frames:v','1','-y',str(poster)],30)
        except Exception:pass
        with path.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
        return {'path':rel,'url':'/file/'+quote(rel,safe='/'),'name':path.name,'sha256':sha,'rights_cleared':bool(rights),'duration':duration,'width':int(dim[1]) if dim else 0,'height':int(dim[2]) if dim else 0,'poster':'/file/'+quote(poster.relative_to(root).as_posix(),safe='/') if poster.exists() else None,'revision':revision,'public_url':''}
    def upload(self,data,filename,rights,post_id=None,expected=None):
        name='Content '+secrets.token_hex(6);store=TimelineStore(self.workspace,name)
        with self.context.lock:
            p=store.load();p,a=store.import_media(data,filename,p['revision'],{'rights_status':'owned' if rights else 'unknown','import_method':'marketing upload'})
            if a['kind']!='video':raise ValueError('Upload a video for a social post. Add audio and images in the editor.')
            p=store.edit([{'op':'insert','track':'V1','asset':a['id'],'start':0,'source_in':0,'source_out':min(a['duration_frames'],18000),'ripple':False}],p['revision'],'Imported video into timeline')
        media=self.media_record(self.workspace.root/a['path'],rights,p['revision']);media['name']=Path(filename).name
        timeline={'name':name,'revision':p['revision'],'checksum':digest(p)}
        if post_id:return self.store.edit(post_id,expected,{'media':media,'timeline':timeline,'music':None})
        return self.store.create(Path(filename).stem,media=media,timeline=timeline)
    def state(self):
        return {'posts':self.store.posts(),'jobs':[{'id':j['id'],'post_id':j['post_id'],'channel':j['channel'],'due':j['due'],'status':j['status'],'error':j['payload'].get('error',''),'provider':j['payload'].get('provider',{})} for j in self.store.jobs()], 'brand':self.store.settings(),'accounts':self.connections(), 'publishing_service':{'name':'Upload-Post','configured':self.bridge.configured(),'profile':self.bridge.config()['profile']},'music':self.store.music(),'metrics':self.store.metrics(),'events':self.store.events(),'ai':bool(os.getenv('OPENAI_API_KEY')),'origin':self.origin,'version':'5.0.0','password_set':bool(self.accounts.load().get('_workspace_auth')),'hosting':'local' if urlparse(self.origin).hostname in ('127.0.0.1','localhost') else 'hosted'}
    def connections(self):
        external=self.bridge.status()
        if external is not None:return external
        saved=self.accounts.load()
        return [{**a,'destination':saved.get(a['channel'],{}).get('id'),'provider':'direct'} for a in self.accounts.status()]
    def finish(self,name,revision,post_id=None):
        ts=TimelineStore(self.workspace,name)
        with self.context.lock:project=ts.load()
        if project['revision']!=revision:raise Conflict('The editor changed. Wait for Saved and try Done again.')
        if not project['clips']:raise ValueError('Add a clip before preparing social posts.')
        post=self.store.get(post_id) if post_id else next((p for p in self.store.posts() if (p.get('timeline') or {}).get('name')==name),None)
        if post and (post.get('timeline') or {}).get('name')!=name:raise ValueError('This post is linked to another editor project.')
        if not post:
            post=self.store.create(name,timeline={'name':name,'revision':revision,'checksum':digest(project)})
        if not post.get('media') or not post['media'].get('rendered') or (post.get('timeline') or {}).get('checksum')!=digest(project):post=self.render(post,'portrait')
        warnings=[]
        missing=[c for c,v in post['variants'].items() if not v['caption'].strip()]
        if missing:
            try:
                variants=generate(post,self.store.settings(),self.workspace.root)
                post=self.store.edit(post['id'],post['version'],{'variants':{c:variants[c] for c in missing},'copy_source':'OpenAI · '+os.getenv('NEXTPLAY_COPY_MODEL','gpt-4.1-mini')})
            except ValueError as e:warnings.append(str(e))
        return {'post':post,'url':'/#studio/'+post['id'],'warnings':warnings}

    def render(self,post,preset):
        if not post.get('timeline'):raise ValueError('Open or import an editor project first.')
        ts=TimelineStore(self.workspace,post['timeline']['name'])
        with self.context.lock:p=ts.load()
        if not p['clips']:raise ValueError('Add clips in the editor before rendering.')
        result=ts.export(p,preset,False)
        rel=result['url'][len('/file/'):]
        from urllib.parse import unquote
        path=self.workspace.root/unquote(rel);manifest=json.loads(path.with_suffix('.json').read_text())
        media=self.media_record(path,manifest['rights_cleared'],p['revision']);media['rendered']=True
        return self.store.edit(post['id'],post['version'],{'media':media,'timeline':{'name':ts.name,'revision':p['revision'],'checksum':digest(p)},'music':None})

class Handler(_DashboardHandler):
    platform:Platform
    def _session(self,status=200,location=None):
        session=secrets.token_urlsafe(32);self.platform.sessions[session]=time.time()+43200
        self.send_response(status)
        self.send_header('Set-Cookie','nextplay_session='+session+'; HttpOnly; SameSite=Lax; Path=/; Max-Age=43200'+('; Secure' if self.platform.origin.startswith('https') else ''))
        self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','no-referrer')
        if location:self.send_header('Location',location)
        self.send_header('Content-Length','2' if status==200 else '0');self.end_headers()
        if status==200:self.wfile.write(b'{}')
    def log_message(self,format,*args):
        # First-login tokens must never appear in application logs.
        if urlparse(self.path).path=='/auth/link':return
        super().log_message(format,*args)
    def _authenticated(self):
        if not os.getenv('NEXTPLAY_PASSWORD'):return True
        import http.cookies
        try:
            c=http.cookies.SimpleCookie(self.headers.get('Cookie',''));value=c['nextplay_session'].value
            return self.platform.sessions.get(value,0)>time.time()
        except Exception:return False
    def _local_request(self):
        path=urlparse(self.path).path
        if path=='/health' and self.command in ('GET','HEAD'):return True
        host=self.headers.get('Host','');origin=self.headers.get('Origin')
        allowed={urlparse(self.platform.origin).netloc}
        if host not in allowed or (origin and origin.rstrip('/')!=self.platform.origin):
            self.send_error(403,'Same-origin requests only');return False
        path=urlparse(self.path).path
        public=path in ('/auth/login','/auth/link','/health') or path.startswith('/platform-static/')
        if not public and not self._authenticated():
            if self.command in ('GET','HEAD') and not path.startswith(('/api/','/file/')):
                self._html(LOGIN,head_only=self.command=='HEAD')
            else:self._json({'error':'Sign in to continue.'},401)
            return False
        return True
    def do_HEAD(self):
        parsed=urlparse(self.path)
        if parsed.path=='/health':
            self.send_response(200);self.send_header('Content-Length','0');self.end_headers();return
        if parsed.path=='/':
            if self._local_request():self._html((STATIC/'index.html').read_text(),head_only=True)
            return
        super().do_HEAD()
    def do_GET(self):
        if not self._local_request():return
        parsed=urlparse(self.path)
        try:
            if parsed.path=='/auth/link':
                token=parse_qs(parsed.query).get('token',[''])[0]
                with self.platform.auth_lock:
                    if self.platform.accounts.load().get('_workspace_auth') or token in self.platform.used_invites or not valid_invite(token,os.getenv('NEXTPLAY_PASSWORD','')):
                        self._json({'error':'This invitation expired. Sign in with your workspace password.'},401);return
                    self.platform.used_invites.add(token);self._session(303,'/#settings');return
            if parsed.path=='/health':self._json({'ok':True,'app':'nextplay-social-studio'});return
            if parsed.path in ('/','/index.html'):
                self._html((STATIC/'index.html').read_text());return
            if parsed.path=='/legacy':
                from pipeline_core.dashboard import DASHBOARD_HTML
                self._html(DASHBOARD_HTML);return
            if parsed.path.startswith('/platform-static/'):
                name=parsed.path.rsplit('/',1)[-1]
                if name not in ('app.js','app.css','editor-shell.css','finish-editor.js','icon.svg','manifest.json'):self.send_error(404);return
                body=(STATIC/name).read_bytes();self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(name)[0] or 'application/octet-stream');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-cache');self.end_headers();self.wfile.write(body);return
            if parsed.path=='/api/marketing/state':self._json(self.platform.state());return
            if parsed.path=='/api/marketing/export':
                pid=parse_qs(parsed.query).get('id',[''])[0];p=self.platform.store.get(pid)
                buff=io.BytesIO()
                with zipfile.ZipFile(buff,'w',zipfile.ZIP_DEFLATED) as z:
                    z.writestr('post.json',json.dumps(p,indent=2))
                    for c,v in p['variants'].items():z.writestr(c+'-caption.txt',self.platform.publishers.caption(v))
                    if p.get('media'):
                        path=(self.platform.workspace.root/p['media']['path']).resolve();path.relative_to(self.platform.workspace.root.resolve());z.write(path,'video'+path.suffix)
                    if p.get('timeline'):
                        ts=TimelineStore(self.platform.workspace,p['timeline']['name']);revision=p['timeline']['revision'];z.writestr('timeline-revision.json',json.dumps(ts.revision(revision),indent=2))
                    z.writestr('README.txt','NextPlay post package. Native music must be added in its platform app. Confirm licensing before publishing.\n')
                body=buff.getvalue();self.send_response(200);self.send_header('Content-Type','application/zip');self.send_header('Content-Disposition','attachment; filename="nextplay-post.zip"');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
            if parsed.path.startswith('/oauth/callback/'):
                q=parse_qs(parsed.query);c=parsed.path.rsplit('/',1)[-1]
                self.platform.accounts.callback(c,q.get('code',[''])[0],q.get('state',[''])[0]);self.send_response(303);self.send_header('Location','/#settings');self.end_headers();return
            if parsed.path=='/timeline':
                from pipeline_core.timeline_api import STATIC as EDITOR_STATIC
                html=(EDITOR_STATIC/'timeline.html').read_text()
                html=html.replace('<header>', '<link rel="stylesheet" href="/platform-static/editor-shell.css"><header>',1)
                html=html.replace('<a href="/">← Workspace</a>', '<a href="/#studio">← Social Studio</a>',1)
                html=html.replace('<body>','<body><div style="padding:10px 18px;background:#14131c;color:#eee"><a href="/#studio" style="color:#cbbaff;text-decoration:none">← Back to NextPlay Studio</a></div>')
                name=parse_qs(parsed.query).get('name',['Untitled'])[0]
                linked=next((p for p in self.platform.store.posts() if (p.get('timeline') or {}).get('name')==name),None)
                html=html.replace('</header>', '<button id="finishSocial" type="button">Done → Prepare posts</button></header>',1)
                html += '<script src="/platform-static/finish-editor.js" defer></script>'
                self._html(html);return
            super().do_GET()
        except Exception as e:self._json({'error':str(e) if isinstance(e,ValueError) else 'Unable to complete this request.'},400)
    def do_POST(self):
        if not self._local_request():return
        parsed=urlparse(self.path)
        if parsed.path=='/auth/login':
            if self.headers.get('X-NextPlay')!='marketing':self._json({'error':'Invalid request.'},403);return
            body=self._body_json();ip=self.client_address[0]
            with self.platform.auth_lock:
                failures=[t for t in self.platform.login_failures.get(ip,[]) if t>time.time()-300]
                if len(failures)>=10:self._json({'error':'Try again later.'},429);return
                if not os.getenv('NEXTPLAY_PASSWORD') or matches(str(body.get('password','')),self.platform.accounts.load().get('_workspace_auth'),os.environ['NEXTPLAY_PASSWORD']):
                    self._session();return
                failures.append(time.time());self.platform.login_failures[ip]=failures
            self._json({'error':'Invalid password.'},401);return
        if parsed.path in ('/api/workspace/switch','/api/product/create'):
            self._json({'error':'Workspace switching is disabled in Social Studio to keep editor and publishing media in the same workspace.'},400);return
        if not parsed.path.startswith('/api/marketing/'):
            # Preserve the original editor and workflow routes, with the stronger auth boundary above.
            super().do_POST();return
        try:
            if self.headers.get('X-NextPlay')!='marketing':raise ValueError('Missing dashboard request header.')
            action=parsed.path.rsplit('/',1)[-1];s=self.platform.store
            if action=='upload':
                q=parse_qs(parsed.query);length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=512*1024*1024:raise ValueError('Upload a video up to 512 MB.')
                data=self.rfile.read(length)
                if len(data)!=length:raise ValueError('Upload was interrupted.')
                p=self.platform.upload(data,q.get('filename',['video.mp4'])[0],q.get('rights',['unknown'])[0]=='owned',q.get('id',[None])[0],int(q.get('version',['0'])[0]));self._json({'post':p});return
            body=self._body_json()
            if action=='workspace-password':
                with self.platform.auth_lock:
                    saved=self.platform.accounts.load();record=saved.get('_workspace_auth')
                    if record and not matches(str(body.get('current','')),record,''):raise ValueError('Enter your current password.')
                    saved['_workspace_auth']=password_record(str(body.get('password','')));self.platform.accounts.save(saved);self.platform.sessions.clear();self._session();return
            if action=='ai-config':
                key=str(body.get('key',''))
                if not key.startswith('sk-') or len(key)>512:raise ValueError('Invalid API key.')
                with self.platform.auth_lock:
                    saved=self.platform.accounts.load();saved['_openai']={'key':key};self.platform.accounts.save(saved);os.environ['OPENAI_API_KEY']=key
                self._json({'configured':True});return
            if action=='create':self._json({'post':s.create(body.get('title',''),body.get('brief',''))});return
            if action=='settings':self._json({'brand':s.save_settings(body)});return
            if action=='provider-config':self._json(self.platform.bridge.configure(str(body.get('key','')),str(body.get('profile',''))));return
            if action=='connections':
                self.platform.bridge.cached=None;self._json({'accounts':self.platform.connections()});return
            if action=='finish':self._json(self.platform.finish(str(body.get('name','Untitled'))[:200],body.get('revision'),body.get('id')));return
            if action=='connect':self._json({'url':self.platform.bridge.connect(body['channel']) if self.platform.bridge.configured() else self.platform.accounts.start(body['channel'])});return
            if action=='disconnect':
                if self.platform.bridge.configured():self.platform.bridge.disconnect(body['channel'])
                else:self.platform.accounts.disconnect(body['channel'])
                self._json({'ok':True});return
            if action=='creator':self._json({'creator':self.platform.publishers.creator()});return
            if action=='research':self._json(research_music(body.get('brief',''),body.get('platform','tiktok'),body.get('region','US')));return
            if action=='music':
                item={'title':str(body.get('title',''))[:200],'artist':str(body.get('artist',''))[:200],'source':str(body.get('source',''))[:2000],'mode':'native','license':'unverified','notes':str(body.get('notes',''))[:1000]}
                if not item['title'].strip():raise ValueError('Give the sound a title.')
                self._json({'music':s.add_music(item)});return
            if action=='retry':
                job=next((j for j in s.jobs() if j['id']==body['id']),None)
                if not job:raise ValueError('Job not found.')
                a=next(a for a in self.platform.connections() if a['channel']==job['channel'])
                if not a['connected'] or body.get('destination')!=a.get('destination'):raise ValueError('Refresh and confirm the connected account before retrying.')
                self._json({'post':s.retry(body['id'],a['destination'])});return
            if action=='cancel':
                s.cancel(body['id']);self._json({'ok':True});return
            if action=='reconcile':
                job=next((j for j in s.jobs() if j['id']==body['id']),None)
                if not job or job['status']!='attention':raise ValueError('This job does not need reconciliation.')
                status=body.get('status')
                if status not in ('published','failed'):raise ValueError('Confirm whether the post exists on the platform.')
                payload=job['payload'];payload['error']='';payload['provider']['reconciled']=now();payload['provider']['post_id']=str(body.get('provider_id',''))[:200]
                if status=='published' and not payload['provider']['post_id']:raise ValueError('Enter the platform post ID before marking it published.')
                s.job_update(job['id'],status,payload);self._json({'ok':True});return
            if action=='analytics':
                count=0
                for job in s.jobs():
                    if job['status']!='published':continue
                    p=job['payload'].get('provider',{});ids=p.get('post_ids') or [p.get('post_id') or p.get('video_id')]
                    ids=[i for i in ids if i]
                    if p.get('service')=='upload-post':items=self.platform.bridge.analytics(job['channel'],p)
                    else:
                        if not ids:continue
                        items=self.platform.publishers.analytics(job['channel'],ids)
                    for item in items:
                        s.add_metric({'channel':job['channel'],'post_id':str(item['id']),'collected':now(),'data':item});count+=1
                self._json({'collected':count});return
            p=s.get(body['id'])
            if p['version']!=body.get('version'):raise Conflict('This post changed. Reload before continuing.')
            if action=='edit':
                changes=body.get('changes',{})
                # Clients cannot inject file paths or manufacture media rights/provenance records.
                if any(k in changes for k in ('media','timeline','copy_source')):raise ValueError('Use the media or editor actions to update files.')
                if changes.get('music') is not None:
                    match=next((m for m in s.music() if m['id']==changes['music'].get('id')),None)
                    if not match:raise ValueError('Choose a saved sound.')
                    changes['music']=match
                p=s.edit(p['id'],p['version'],changes)
            elif action=='media-settings':
                if not p.get('media'):raise ValueError('Add a video first.')
                media=dict(p['media']);url=str(body.get('public_url',''))[:4000]
                if url and (urlparse(url).scheme!='https' or not urlparse(url).hostname):raise ValueError('Use an HTTPS video URL.')
                media['public_url']=url;media['rights_cleared']=bool(body.get('rights_cleared'))
                p=s.edit(p['id'],p['version'],{'media':media})
            elif action=='generate':
                variants=generate(p,s.settings(),self.platform.workspace.root,body.get('direction',''))
                p=s.edit(p['id'],p['version'],{'variants':variants,'copy_source':'OpenAI · '+os.getenv('NEXTPLAY_COPY_MODEL','gpt-4.1-mini')})
            elif action=='approve':
                if p.get('media'):
                    path=(self.platform.workspace.root/p['media']['path']).resolve();path.relative_to(self.platform.workspace.root.resolve())
                    with path.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
                    if sha!=p['media']['sha256']:raise ValueError('Video has changed on disk. Reimport it before approval.')
                p=s.approve(p['id'],p['version'],body.get('channels',[]),bool(body.get('reject')))
            elif action=='schedule':
                destinations={}
                for a in self.platform.connections():
                    if a['channel'] not in body.get('channels',[]):continue
                    target=a.get('destination') if a['connected'] else None
                    if target!=(body.get('destinations') or {}).get(a['channel']):raise ValueError('A destination account changed. Refresh and review before publishing.')
                    destinations[a['channel']]=target
                p=s.enqueue(p['id'],p['version'],body.get('channels',[]),body.get('due'),destinations)
            elif action=='editor':
                if not p.get('timeline'):
                    name='Content '+p['id'];ts=TimelineStore(self.platform.workspace,name)
                    with self.context.lock:project=ts.load()
                    p=s.edit(p['id'],p['version'],{'timeline':{'name':name,'revision':project['revision'],'checksum':digest(project)}})
                self._json({'post':p,'url':'/timeline?name='+quote(p['timeline']['name'])});return
            elif action=='render':p=self.platform.render(p,body.get('preset','portrait'))
            else:raise ValueError('Unknown dashboard action.')
            self._json({'post':p})
        except Exception as e:
            self._json({'error':str(e) if isinstance(e,ValueError) else 'Unable to complete this action. Your last saved draft is preserved.'},409 if isinstance(e,Conflict) else 400)

def main():
    parser=argparse.ArgumentParser(description='NextPlay Social Studio')
    parser.add_argument('--data',default=str(Path(__file__).resolve().parents[2]/'runtime'))
    parser.add_argument('--workspace');parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=int(os.getenv('PORT','8770')))
    parser.add_argument('--origin',default=os.getenv('NEXTPLAY_ORIGIN'));args=parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2]/'.env.local',override=False)
    args.origin=args.origin or os.getenv('NEXTPLAY_ORIGIN')
    if args.host not in ('127.0.0.1','localhost') and (not os.getenv('NEXTPLAY_PASSWORD') or len(os.getenv('NEXTPLAY_PASSWORD',''))<16 or not args.origin):parser.error('Network hosting requires NEXTPLAY_PASSWORD (16+ characters) and NEXTPLAY_ORIGIN.')
    origin=args.origin or os.getenv('NEXTPLAY_ORIGIN') or f'http://127.0.0.1:{args.port}'
    os.environ.setdefault('NEXTPLAY_AI_MODEL',os.getenv('NEXTPLAY_COPY_MODEL','gpt-4.1-mini'))
    platform=Platform(args.data,origin,args.workspace)
    handler=type('NextPlayHandler',(Handler,),{'platform':platform,'context':platform.context})
    server=ThreadingHTTPServer((args.host,args.port),handler);platform.worker.start()
    print('NextPlay Social Studio: '+origin,flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:platform.worker.stop.set();server.server_close()

if __name__=='__main__':main()
