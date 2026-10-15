import hashlib
import io
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from PIL import Image
from pipeline_core.dashboard_previews import make_poster, poster_url, local_video
from pipeline_core.dashboard import DashboardContext, _DashboardHandler, build_dashboard_state
from pipeline_core.models import Product, VideoCandidate
from pipeline_core.storage import ProductWorkspace
from pipeline_core.timeline_render import run_ffmpeg

class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source.mp4'
        run_ffmpeg(['-f','lavfi','-i','color=c=blue:s=160x120:r=30:d=1',
                    '-c:v','libx264','-pix_fmt','yuv420p','-y',str(self.source)])
    def tearDown(self):self.temp.cleanup()
    def test_local_poster_cached_without_source_changes(self):
        before=self.source.read_bytes();poster=make_poster(self.root,self.source)
        with Image.open(poster) as im:self.assertEqual(im.size,(480,360));self.assertEqual(im.format,'JPEG')
        stamp=poster.stat().st_mtime_ns
        self.assertEqual(make_poster(self.root,self.source),poster)
        self.assertEqual(poster.stat().st_mtime_ns,stamp)
        self.assertEqual(self.source.read_bytes(),before)
    def test_content_version_changes_when_source_changes(self):
        first=poster_url(self.root,'video',self.source)
        self.source.write_bytes(self.source.read_bytes()+b'\0')
        self.assertNotEqual(poster_url(self.root,'video',self.source),first)
        self.assertIsNone(poster_url(self.root,'missing','missing.mp4'))
    def test_unsafe_sources_and_cache_links_rejected(self):
        (self.root/'.env').write_text('fixture only')
        self.assertIsNone(poster_url(self.root,'secret',self.root/'.env'))
        with self.assertRaises(ValueError):local_video(self.root,'/etc/passwd')
        with tempfile.TemporaryDirectory() as other:
            (self.root/'nextplay').symlink_to(other,target_is_directory=True)
            with self.assertRaises(ValueError):make_poster(self.root,self.source)
            self.assertEqual(list(Path(other).iterdir()),[])
    def test_http_concurrent_posters_and_provenance(self):
        product=Product('test:generated','123','Preview QA')
        workspace=ProductWorkspace(self.root/'products',product).ensure();workspace.save_product(product)
        source=workspace.raw_dir/'video.mp4';source.write_bytes(self.source.read_bytes())
        candidates=[VideoCandidate('test:video','video',thumbnail_url='https://example.invalid/expired.jpg',
                    download_status='downloaded',local_path=str(source)),
                    VideoCandidate('test:pending','pending',thumbnail_url='https://example.invalid/discovery.jpg'),
                    VideoCandidate('test:unsafe','unsafe',local_path='/etc/passwd')]
        workspace.save_candidates(candidates)
        original=(workspace.root/'videos.json').read_bytes();source_hash=hashlib.sha256(source.read_bytes()).digest()
        state=build_dashboard_state(product,workspace)
        row=next(c for c in state['candidates'] if c['video_id']=='video')
        self.assertTrue(row['poster_url'].startswith('/api/source-poster?'))
        self.assertEqual(row['thumbnail_url'],candidates[0].thumbnail_url)
        self.assertEqual(row['rights']['status'],'unknown')
        self.assertIsNone(next(c for c in state['candidates'] if c['video_id']=='pending')['poster_url'])
        handler=type('PreviewHandler',(_DashboardHandler,),{'context':DashboardContext(product,workspace)})
        server=ThreadingHTTPServer(('127.0.0.1',0),handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            def read(_):
                with urllib.request.urlopen(base+row['poster_url']) as r:
                    self.assertEqual(r.headers['Content-Type'],'image/jpeg');return r.read()
            with ThreadPoolExecutor(max_workers=4) as pool:images=list(pool.map(read,range(4)))
            self.assertTrue(all(image==images[0] for image in images))
            with Image.open(io.BytesIO(images[0])) as im:im.verify()
            for identity in ('missing','unsafe','../.env'):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(base+'/api/source-poster?video_id='+identity)
                self.assertEqual(error.exception.code,404)
                error.exception.close()
            self.assertEqual((workspace.root/'videos.json').read_bytes(),original)
            self.assertEqual(hashlib.sha256(source.read_bytes()).digest(),source_hash)
        finally:server.shutdown();server.server_close();thread.join()
