"""Disposable Safari QA workspace. Never reads user media or credentials."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from http.server import ThreadingHTTPServer
from pipeline_core.models import Product
from pipeline_core.storage import ProductWorkspace
from pipeline_core.dashboard import DashboardContext, _DashboardHandler
from pipeline_core.timeline import new_project,make_clip,apply
from pipeline_core.timeline_store import TimelineStore
from pipeline_core.timeline_render import run_ffmpeg

def fixture(root):
    product=Product('test:synthetic','123','Synthetic editor QA')
    workspace=ProductWorkspace(Path(root),product).ensure()
    workspace.save_product(product)
    path=workspace.raw_dir/'synthetic.mp4'
    if not path.exists():
        run_ffmpeg(['-f','lavfi','-i','testsrc2=s=320x240:r=30:d=6','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=6','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest','-y',str(path)])
    store=TimelineStore(workspace,'Safari QA')
    if not store.history():
        p=new_project('Safari QA');p['canvas']={'width':320,'height':240,'background':'#000000'}
        a=store.asset(path,30,{'rights_status':'owned','source_url':'test:generated','creator':'Synthetic fixture'})
        p['assets'][a['id']]=a
        p['clips']=[make_clip(p,'V1',a['id'],0,0,60),make_clip(p,'V1',a['id'],60,60,120),make_clip(p,'V1',a['id'],120,120,180)]
        store.commit(p,0,'Synthetic fixture')
    return product,workspace

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--port',type=int,default=8874);args=parser.parse_args()
    product,workspace=fixture(args.root)
    handler=type('FixtureHandler',(_DashboardHandler,),{'context':DashboardContext(product,workspace)})
    server=ThreadingHTTPServer(('127.0.0.1',args.port),handler)
    print(f'http://127.0.0.1:{args.port}/timeline?name=Safari%20QA',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
