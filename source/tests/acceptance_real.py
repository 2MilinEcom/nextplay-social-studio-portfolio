"""Opt-in acceptance run against a private copy of an existing product workspace.
No credential or media is included in release packages. Uses local copies only.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import threading
from http.server import ThreadingHTTPServer
import urllib.request
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pipeline_core.storage import load_product_workspace,atomic_write_json
from pipeline_core.timeline import apply,clip,make_clip,uid,duration
from pipeline_core.timeline_store import TimelineStore,digest
from pipeline_core.timeline_render import run_ffmpeg
from pipeline_core.dashboard import DashboardContext,_DashboardHandler


def inventory(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}

def copy_workspace(source,destination):
    if destination.exists():raise ValueError('Choose a new acceptance directory')
    destination.mkdir(parents=True)
    def rewrite(v):
        if isinstance(v,str) and v.startswith(str(source)+'/'):return str(destination)+v[len(str(source)):]
        if isinstance(v,list):return [rewrite(x) for x in v]
        if isinstance(v,dict):return {k:rewrite(x) for k,x in v.items()}
        return v
    for p in source.rglob('*'):
        rel=p.relative_to(source)
        if p.is_symlink() or not p.is_file():continue
        # Explicit product records/media only; no env, sessions or browser files.
        allowed=(len(rel.parts)==1 and p.name in {'product.json','videos.json','search_plan.json','product.webp','product.jpg','product.png','rights.json','review_state.json','studio_settings.json','source_quality.json'}) or rel.parts[0] in {'raw','analysis'} and p.suffix in {'.json','.mp4','.mov','.webm'} or rel.as_posix()=='ads/ad_plans.json'
        if not allowed:continue
        target=destination/rel;target.parent.mkdir(parents=True,exist_ok=True)
        if p.suffix=='.json':atomic_write_json(target,rewrite(json.loads(p.read_text())))
        else:shutil.copyfile(p,target)
    return load_product_workspace(destination)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    source=args.source.resolve();root=args.output.resolve();original=inventory(source);results=[]
    def passed(label,**details):
        item=dict(check=label,status='passed',**details);results.append(item);print(json.dumps(item),flush=True)
    product,workspace=copy_workspace(source,root);passed('Original workspace copied',media=len(list(workspace.raw_dir.glob('*.mp4'))))
    store=TimelineStore(workspace,'ad_001_direct_response');p=store.load();assert not p.get('warnings'),p.get('warnings');assert len(p['clips'])>=4
    initial=p['revision'];passed('Real legacy ad migrated',clips=len(p['clips']),frames=duration(p))
    p=store.edit([dict(op='canvas',values=dict(width=360,height=640,background='#101010')),dict(op='mode',magnetic=False,snapping=True)],p['revision'],'Acceptance canvas')
    videos=[c for c in p['clips'] if c['kind']=='video'];selected=videos[0]['id']
    def edit(command):
        nonlocal p
        p=store.edit([command],p['revision'],'Acceptance '+command['op']);return p
    edit(dict(op='split',id=selected,frame=clip(p,selected)['start']+30));other=next(c for c in p['clips'] if c['kind']=='video' and c['start']==clip(p,selected)['start']+clip(p,selected)['duration'])
    edit(dict(op='join',id=selected,other=other['id']));edit(dict(op='trim',id=selected,edge='right',delta=-1));edit(dict(op='trim',id=selected,edge='right',delta=1))
    if clip(p,selected)['source_in']>0:edit(dict(op='slip',id=selected,delta=-1));edit(dict(op='slip',id=selected,delta=1))
    edit(dict(op='detach',id=selected,track='A1'));edit(dict(op='properties',id=selected,values=dict(scale=1.1,x=8,y=-12,brightness=.03,contrast=1.05,saturation=.9,opacity=.95,crop=[.02,.02,.02,.02])))
    edit(dict(op='keyframe',id=selected,field='x',frame=0,value=0));edit(dict(op='keyframe',id=selected,field='x',frame=clip(p,selected)['duration'],value=15))
    for c in p['clips']:
        if c['kind']=='audio':edit(dict(op='properties',id=c['id'],values=dict(volume=.8,pan=-.1,fade_in=min(8,c['duration']),fade_out=min(8,c['duration']))));break
    edit(dict(op='insert',track='T1',start=0,source_out=90,text='ELROEL • Editor acceptance',ripple=False));title=p['clips'][-1]['id']
    edit(dict(op='properties',id=title,values=dict(font_size=26,y=-210,text_box=True)))
    # Import real image as an editable logo/image layer.
    from PIL import Image
    overlay=root/'acceptance-logo.png';Image.new('RGBA',(80,40),(20,160,210,160)).save(overlay)
    p,a=store.import_media(overlay.read_bytes(),'acceptance-logo.png',p['revision'],dict(rights_status='owned',source_url='test:generated'))
    edit(dict(op='add_track',kind='image',name='Acceptance logo'));track=p['tracks'][-1]['id'];edit(dict(op='insert',track=track,asset=a['id'],start=0,source_out=90,ripple=False));logo=p['clips'][-1]['id']
    edit(dict(op='properties',id=logo,values=dict(scale=.18,x=115,y=210,opacity=.7)))
    passed('Real footage multitrack editing',revision=p['revision'],clips=len(p['clips']))
    handler=type('AcceptanceHandler',(_DashboardHandler,),{'context':DashboardContext(product,workspace)})
    server=ThreadingHTTPServer(('127.0.0.1',0),handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def http(action,**kw):
        request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/timeline/{action}',data=json.dumps(dict(name=store.name,**kw)).encode(),headers={'Content-Type':'application/json','X-NextPlay':'timeline'})
        return json.loads(urllib.request.urlopen(request,timeout=600).read())
    try:
        saved=p['revision'];draft=http('render',revision=saved,preset='custom',draft=True);assert http('load')['project']['revision']==saved
        output=http('render',revision=saved,preset='portrait',draft=False)
        manifest=json.loads((root/output['manifest'].removeprefix('/file/')).read_text());assert manifest['revision']==saved;assert manifest['checksum']==digest(p)
        media=root/output['url'].removeprefix('/file/');assert media.exists();passed('Real draft and portrait export',revision=saved,bytes=media.stat().st_size,rights_cleared=manifest['rights_cleared'])
        restored=http('restore',revision=saved,restore_revision=initial)['project'];assert restored['revision']>saved;assert store.revision(saved)==p;assert media.exists()
        recovered=http('restore',revision=restored['revision'],restore_revision=saved)['project'];assert len(recovered['clips'])==len(p['clips'])
        reopened=TimelineStore(workspace,store.name).load();assert reopened==recovered;passed('Real version recovery and reopening',revision=reopened['revision'])
    finally:server.shutdown();server.server_close();thread.join()
    # Planning and QA operate on a separate folder, retaining the original copied plans.
    from pipeline_core.ad_builder import build_ad_plans,render_ad_plans
    from pipeline_core.creative_quality import heuristic_quality
    plans=build_ad_plans(workspace.analysis_dir,root/'acceptance-planner',count=1,max_duration=12,target_duration=10)
    for plan in plans:plan['render_settings']={'captions':True,'width':360,'height':640}
    paths=render_ad_plans(plans);assert paths and all(Path(x).is_file() for x in paths)
    qa=heuristic_quality(plans[0]);assert 0<=qa['overall']<=100
    passed('Real analyzed sources to planned and rendered ad',segments=len(plans[0]['segments']),quality=qa['overall'])
    assert inventory(source)==original,'Original project changed during acceptance'
    passed('Original project inventory unchanged',files=len(original))
    atomic_write_json(root/'acceptance-results.json',dict(results=results,export=str(media),workspace=str(root)))
    print('ACCEPTANCE PASS',flush=True)
if __name__=='__main__':main()
