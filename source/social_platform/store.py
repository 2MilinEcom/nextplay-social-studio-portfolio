from __future__ import annotations
import hashlib, json, sqlite3, uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

CHANNELS = ('tiktok', 'instagram', 'facebook')
LIMITS = {'tiktok': 2200, 'instagram': 2200, 'facebook': 5000}

class Conflict(ValueError): pass

def now(): return datetime.now(timezone.utc).isoformat()
def checksum(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def ident(): return uuid.uuid4().hex

def parse_time(value):
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if dt.tzinfo is None: raise ValueError()
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError): raise ValueError('Choose a valid date and time with a timezone.') from None

class Store:
    def __init__(self, root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'marketing.sqlite3'
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY, version INTEGER, payload TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, post_id TEXT, channel TEXT, fingerprint TEXT UNIQUE, due TEXT, status TEXT, payload TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, post_id TEXT, action TEXT, detail TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS settings(id TEXT PRIMARY KEY, payload TEXT);
            CREATE TABLE IF NOT EXISTS music(id TEXT PRIMARY KEY, payload TEXT);
            CREATE TABLE IF NOT EXISTS metrics(id TEXT PRIMARY KEY, payload TEXT);
            ''')
            # Never blindly retry an upload interrupted between provider acceptance and local persistence.
            db.execute("UPDATE jobs SET status='attention',updated=? WHERE status='sending'",(now(),))

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA foreign_keys=ON')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception: db.rollback();raise
        finally: db.close()

    def settings(self):
        with self.db() as db: r=db.execute("SELECT payload FROM settings WHERE id='brand'").fetchone()
        return json.loads(r[0]) if r else {'name':'Your brand','audience':'','voice':'Warm, clear, confident','cta':'','timezone':'America/Los_Angeles'}

    def save_settings(self, body):
        from zoneinfo import ZoneInfo
        fields={k:str(body.get(k,''))[:1000] for k in ['name','audience','voice','cta','timezone']}
        if not fields['name'].strip():raise ValueError('Give your brand a name.')
        try:ZoneInfo(fields['timezone'])
        except Exception:raise ValueError('Choose a valid timezone.') from None
        with self.db() as db:db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('brand',json.dumps(fields)))
        return fields

    def posts(self):
        with self.db() as db: rows=db.execute('SELECT payload FROM posts ORDER BY created DESC').fetchall()
        return [json.loads(r[0]) for r in rows]

    def get(self, post_id, db=None):
        if db is None:
            with self.db() as conn:return self.get(post_id,conn)
        row=db.execute('SELECT payload FROM posts WHERE id=?',(post_id,)).fetchone()
        if not row:raise ValueError('Post not found.')
        return json.loads(row[0])

    def create(self, title, brief='', media=None, timeline=None):
        title=str(title).strip()[:200]
        if not title:raise ValueError('Give your post a title.')
        p={'id':ident(),'version':1,'title':title,'brief':str(brief)[:5000], 'created':now(), 'media':media,'timeline':timeline,'variants':{},'music':None,'copy_source':'manual'}
        for c in CHANNELS:p['variants'][c]={'caption':'','hashtags':[],'hook':'','cta':'','status':'draft','approval':None,'scheduled':None,'privacy':'','allow_comment':False,'allow_duet':False,'allow_stitch':False,'commercial':False,'branded':False,'music_ack':False}
        with self.db() as db:
            db.execute('INSERT INTO posts VALUES(?,?,?,?)',(p['id'],p['version'],json.dumps(p),p['created']))
            self.event(db,p['id'],'created','Draft created')
        return p

    def event(self,db,pid,action,detail):db.execute('INSERT INTO events(post_id,action,detail,created) VALUES(?,?,?,?)',(pid,action,str(detail)[:1000],now()))
    def save(self, db, p, expected):
        p['version']=expected+1
        changed=db.execute('UPDATE posts SET version=?,payload=? WHERE id=? AND version=?',(p['version'],json.dumps(p),p['id'],expected)).rowcount
        if not changed:raise Conflict('This draft changed in another window. Reload before saving.')
        return p

    def check_editable(self,db,pid):
        if db.execute("SELECT 1 FROM jobs WHERE post_id=? AND status IN ('sending','processing','attention')",(pid,)).fetchone():
            raise Conflict('Publishing is in progress or needs reconciliation. Resolve it before changing this post.')

    def fingerprint(self,p,c):
        v=p['variants'][c]
        return checksum({'post':p['id'],'channel':c,'media':p['media'],'music':p['music'],'caption':v['caption'],'hashtags':v['hashtags'],'hook':v['hook'],'cta':v['cta'],'privacy':v['privacy'],'interaction':[v['allow_comment'],v['allow_duet'],v['allow_stitch']],'disclosure':[v['commercial'],v['branded'],v['music_ack']]})

    def edit(self,pid,expected,changes):
        with self.db() as db:
            p=self.get(pid,db)
            if p['version']!=expected:raise Conflict('This draft changed. Reload before saving.')
            self.check_editable(db,pid)
            affected=[]
            for key in ['title','brief']:
                if key in changes:
                    p[key]=str(changes[key])[:5000 if key=='brief' else 200]
                    if key=='title' and not p[key].strip():raise ValueError('Give your post a title.')
            for key in ['media','music','timeline']:
                if key in changes:p[key]=changes[key];affected=list(CHANNELS)
            if 'copy_source' in changes:p['copy_source']=str(changes['copy_source'])[:100]
            for c,values in changes.get('variants',{}).items():
                if c not in CHANNELS:raise ValueError('Unknown channel.')
                v=p['variants'][c]
                before=self.fingerprint(p,c)
                for k in ['caption','hook','cta','privacy']:
                    if k in values:v[k]=str(values[k])[:10000]
                if 'hashtags' in values:
                    if not isinstance(values['hashtags'],list):raise ValueError('Hashtags must be a list.')
                    v['hashtags']=[str(x).strip().lstrip('#')[:100] for x in values['hashtags'][:30] if str(x).strip()]
                for k in ['allow_comment','allow_duet','allow_stitch','commercial','branded','music_ack']:
                    if k in values:v[k]=bool(values[k])
                if before!=self.fingerprint(p,c):affected.append(c)
            for c in set(affected):
                p['variants'][c].update(status='draft',approval=None,scheduled=None)
                db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE post_id=? AND channel=? AND status IN ('queued','blocked')",(now(),pid,c))
            self.event(db,pid,'edited','Changes saved; affected approvals cleared')
            return self.save(db,p,expected)

    def approve(self,pid,expected,channels,reject=False):
        if not channels or any(c not in CHANNELS for c in channels):raise ValueError('Select valid channels.')
        with self.db() as db:
            p=self.get(pid,db)
            if p['version']!=expected:raise Conflict('This draft changed. Reload before approving.')
            self.check_editable(db,pid)
            for c in channels:
                v=p['variants'][c]
                if v['status']=='published':raise ValueError('Published posts cannot be approved again.')
                if not reject:
                    if not p.get('media'):raise ValueError('Add a video before approving.')
                    if not v['caption'].strip():raise ValueError('Add a caption before approving.')
                    if len(v['caption']+' '+' '.join('#'+h for h in v['hashtags']))>LIMITS[c]:raise ValueError(f'{c.title()} caption is too long.')
                    if not p['media'].get('rights_cleared'):raise ValueError('Confirm video rights before approving.')
                    if p.get('music') and p['music'].get('mode')=='native':raise ValueError('Native platform music requires an in-app finish. Use an export package or render licensed audio in the editor.')
                    if c=='tiktok' and not v['privacy']:raise ValueError('Choose TikTok privacy before approving.')
                    if c=='tiktok' and not v['music_ack']:raise ValueError('Confirm TikTok music usage before approving.')
                v['approval']=None if reject else {'fingerprint':self.fingerprint(p,c),'at':now()}
                v['status']='rejected' if reject else 'approved';v['scheduled']=None
                db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE post_id=? AND channel=? AND status IN ('queued','blocked')",(now(),pid,c))
            self.event(db,pid,'rejected' if reject else 'approved',', '.join(channels))
            return self.save(db,p,expected)

    def enqueue(self,pid,expected,channels,due,destinations=None):
        dt=parse_time(due)
        if dt.timestamp()<datetime.now(timezone.utc).timestamp()-30:raise ValueError('Choose a future publishing time.')
        if not channels or any(c not in CHANNELS for c in channels):raise ValueError('Select valid channels.')
        with self.db() as db:
            p=self.get(pid,db)
            if p['version']!=expected:raise Conflict('This draft changed. Reload before scheduling.')
            for c in channels:
                v=p['variants'][c];fp=self.fingerprint(p,c)
                if not v['approval'] or v['approval']['fingerprint']!=fp:raise ValueError('Approve the current version before publishing.')
                existing=db.execute('SELECT id,status FROM jobs WHERE fingerprint=?',(fp,)).fetchone()
                if existing and existing['status'] in ('published','sending','processing','attention'):raise ValueError('This approved version already has a publishing job.')
                jid=existing['id'] if existing else ident()
                payload={'post':p,'channel':c,'provider':{},'error':'','attempts':0,'destination':(destinations or {}).get(c)}
                if existing:db.execute('UPDATE jobs SET due=?,status=?,payload=?,updated=? WHERE id=?',(dt.isoformat(),'queued',json.dumps(payload),now(),jid))
                else:db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',(jid,pid,c,fp,dt.isoformat(),'queued',json.dumps(payload),now()))
                v['status']='scheduled';v['scheduled']=dt.isoformat()
            self.event(db,pid,'scheduled',dt.isoformat())
            return self.save(db,p,expected)

    def jobs(self):
        with self.db() as db:rows=db.execute('SELECT * FROM jobs ORDER BY due').fetchall()
        return [{**dict(r),'payload':json.loads(r['payload'])} for r in rows]

    def claim(self):
        with self.db() as db:
            r=db.execute("SELECT * FROM jobs WHERE status='queued' AND due<=? ORDER BY due LIMIT 1",(now(),)).fetchone()
            if not r:return None
            p=self.get(r['post_id'],db);v=p['variants'][r['channel']]
            if not v['approval'] or v['approval']['fingerprint']!=r['fingerprint'] or self.fingerprint(p,r['channel'])!=r['fingerprint']:
                db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=?",(now(),r['id']));return None
            db.execute("UPDATE jobs SET status='sending',updated=? WHERE id=?",(now(),r['id']))
            return {**dict(r),'payload':json.loads(r['payload'])}

    def job_update(self,jid,status,payload):
        with self.db() as db:
            r=db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
            if not r:raise ValueError('Job not found.')
            db.execute('UPDATE jobs SET status=?,payload=?,updated=? WHERE id=?',(status,json.dumps(payload),now(),jid))
            p=self.get(r['post_id'],db)
            p['variants'][r['channel']]['status']={'published':'published','blocked':'blocked','failed':'failed','attention':'attention','processing':'publishing','sending':'publishing','cancelled':'approved'}.get(status,'scheduled')
            self.save(db,p,p['version'])
            self.event(db,r['post_id'],status,payload.get('error') or f"{r['channel']} publishing")

    def cancel(self,jid):
        with self.db() as db:
            r=db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
            if not r or r['status'] not in ('queued','blocked'):raise ValueError('This job has started and cannot be cancelled.')
            db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=?",(now(),jid))
            p=self.get(r['post_id'],db);v=p['variants'][r['channel']]
            v['status']='approved';v['scheduled']=None
            self.event(db,p['id'],'cancelled',r['channel'])
            return self.save(db,p,p['version'])

    def retry(self,jid,destination=None):
        with self.db() as db:
            r=db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
            if not r or r['status'] not in ('blocked','failed'):raise ValueError('Only blocked or confirmed failed jobs can be retried.')
            p=self.get(r['post_id'],db);v=p['variants'][r['channel']]
            if not v['approval'] or self.fingerprint(p,r['channel'])!=r['fingerprint']:raise ValueError('Reapprove this version first.')
            payload=json.loads(r['payload']);payload['error']='';payload['provider']={};payload['destination']=destination
            db.execute("UPDATE jobs SET status='queued',due=?,payload=?,updated=? WHERE id=?",(now(),json.dumps(payload),now(),jid))
            v['status']='scheduled';return self.save(db,p,p['version'])

    def events(self):
        with self.db() as db:return [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')]
    def music(self):
        with self.db() as db:return [json.loads(r[0]) for r in db.execute('SELECT payload FROM music')]
    def add_music(self,item):
        item={**item,'id':ident(),'added':now()}
        with self.db() as db:db.execute('INSERT INTO music VALUES(?,?)',(item['id'],json.dumps(item)))
        return item
    def metrics(self):
        with self.db() as db:return [json.loads(r[0]) for r in db.execute('SELECT payload FROM metrics')]
    def add_metric(self,item):
        with self.db() as db:db.execute('INSERT OR REPLACE INTO metrics VALUES(?,?)',(item['channel']+':'+item['post_id'],json.dumps(item)))
