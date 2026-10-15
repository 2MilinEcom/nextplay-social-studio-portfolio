import json
import math
import struct
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from ffmpeg_bin import FFMPEG
from pipeline_core.timeline import *
from pipeline_core.timeline_store import TimelineStore, Conflict, digest, edit_direction
from pipeline_core.timeline_render import render, safe_asset, run_ffmpeg
from pipeline_core.timeline_api import ai_direction

def model():
    p=new_project()
    p['assets']={'a':dict(id='a',kind='video',duration_frames=600,has_audio=True,path='a.mp4',provenance={'rights_status':'unknown'})}
    p['magnetic']=False
    c=make_clip(p,'V1','a',30,60,180)
    p['clips']=[c]
    return p,c['id']

class EditTests(unittest.TestCase):
    def setUp(self):
        self.p,self.id=model()
    def do(self,op,**kw):
        return apply(self.p,dict(op=op,id=self.id,**kw))
    def test_atomic_failure(self):
        original=deepcopy(self.p)
        with self.assertRaises(ValueError):self.do('trim',edge='left',delta=200)
        self.assertEqual(original,self.p)
    def test_true_trim_both_edges_and_extend(self):
        p=self.do('trim',edge='left',delta=15);c=clip(p,self.id)
        self.assertEqual((c['start'],c['source_in'],c['duration']),(45,75,105))
        p=apply(p,dict(op='trim',id=self.id,edge='left',delta=-15));self.assertEqual(p,self.p)
        p=self.do('trim',edge='right',delta=15);self.assertEqual(clip(p,self.id)['source_out'],195)
    def test_handles_and_one_frame(self):
        p=self.do('trim',edge='right',delta=-119);self.assertEqual(clip(p,self.id)['duration'],1)
        with self.assertRaises(ValueError):self.do('slip',delta=-61)
        with self.assertRaises(ValueError):self.do('trim',edge='right',delta=-120)
    def test_slip_keeps_timeline(self):
        c=clip(self.do('slip',delta=10),self.id)
        self.assertEqual((c['start'],c['duration'],c['source_in'],c['source_out']),(30,120,70,190))
    def test_roll_and_slide(self):
        p=deepcopy(self.p);a=make_clip(p,'V1','a',0,30,60);b=make_clip(p,'V1','a',150,180,240);p['clips']=[a,*p['clips'],b]
        r=apply(p,dict(op='roll',id=self.id,delta=10));self.assertEqual(clip(r,b['id'])['start'],160);self.assertEqual(duration(r),210)
        r=apply(p,dict(op='slide',id=self.id,delta=10));self.assertEqual(clip(r,self.id)['source_in'],60);self.assertEqual(clip(r,a['id'])['duration'],40);self.assertEqual(clip(r,b['id'])['duration'],50)
    def test_roll_gaps_rejected(self):
        self.p['clips'].append(make_clip(self.p,'V1','a',200,180,240))
        with self.assertRaises(ValueError):self.do('roll',delta=10)
    def test_ripple_trim_sync_tracks(self):
        b=make_clip(self.p,'A1','a',150,0,60);self.p['clips'].append(b)
        r=self.do('ripple_trim',edge='right',delta=10);self.assertEqual(clip(r,b['id'])['start'],160)
        r=self.do('ripple_trim',edge='left',delta=10);self.assertEqual(clip(r,self.id)['start'],30);self.assertEqual(clip(r,b['id'])['start'],140)
    def test_split_join_round_trip(self):
        r=self.do('split',frame=90);b=next(c for c in r['clips'] if c['id']!=self.id)
        r=apply(r,dict(op='join',id=self.id,other=b['id']));self.assertEqual(r,self.p)
    def test_linked_split_join_round_trip(self):
        p=self.do('detach',track='A1')
        r=apply(p,dict(op='split',id=self.id,frame=90))
        other=next(c for c in r['clips'] if c['kind']=='video' and c['id']!=self.id)
        r=apply(r,dict(op='join',id=self.id,other=other['id']))
        self.assertEqual(r,p)
    def test_ai_invalid_format_keeps_original(self):
        client=Mock();client.responses.create.return_value=SimpleNamespace(output_text='{"summary":"bad","commands":["properties(id,values)"]}')
        before=deepcopy(self.p)
        with self.assertRaisesRegex(ValueError,'invalid edit format'):ai_direction(self.p,'change',self.id,0,client)
        self.assertEqual(self.p,before)
        client.responses.create.side_effect=RuntimeError('secret credential value')
        with self.assertRaises(ValueError) as error:ai_direction(self.p,'change',self.id,0,client)
        self.assertNotIn('secret',str(error.exception))
    def test_replace_source_on_new_clip_preserves_prior_state(self):
        self.p['assets']['b']=dict(self.p['assets']['a'],id='b',path='replacement.mp4')
        before=deepcopy(self.p)
        r=self.do('replace_source',asset='b')
        self.assertEqual(clip(r,self.id)['asset'],'b')
        self.assertEqual(clip(r,self.id)['provenance']['replaced_asset_ids'],['a'])
        self.assertEqual(self.p,before)
    def test_freeze_unfreeze_preserves_moving_source(self):
        r=self.do('freeze',source_frame=90,duration=30)
        self.assertTrue(clip(r,self.id)['freeze'])
        r=apply(r,dict(op='unfreeze',id=self.id));self.assertEqual(r,self.p)
    def test_split_reverse(self):
        self.p['clips'][0]['reverse']=True
        r=self.do('split',frame=90);a=clip(r,self.id);b=next(c for c in r['clips'] if c['id']!=self.id)
        self.assertEqual((a['source_in'],a['source_out'],b['source_in'],b['source_out']),(120,180,60,120))
    def test_speed_split_source_mapping(self):
        self.p=self.do('speed',value=2)
        r=self.do('split',frame=60);self.assertEqual(clip(r,self.id)['source_out'],120)
    def test_delete_lift_ripple_duplicate(self):
        b=make_clip(self.p,'V1','a',150,0,60);self.p['clips'].append(b)
        self.assertEqual(clip(self.do('delete',ripple=False),b['id'])['start'],150)
        self.assertEqual(clip(self.do('delete',ripple=True),b['id'])['start'],30)
        self.assertEqual(len(self.do('duplicate')['clips']),3)
    def test_magnetic_insert_reorder_start(self):
        self.p['magnetic']=True
        b=make_clip(self.p,'V1','a',150,180,240);self.p['clips'].append(b)
        r=apply(self.p,dict(op='move',id=b['id'],start=0));self.assertEqual(clip(r,b['id'])['start'],0)
        self.assertEqual(clip(r,self.id)['start'],90)
        r=apply(self.p,dict(op='insert',track='V1',asset='a',start=30,source_in=0,source_out=30));self.assertEqual(clip(r,self.id)['start'],60)
    def test_nonmagnetic_move_leaves_other_clips(self):
        b=make_clip(self.p,'V1','a',200,180,240);self.p['clips'].append(b)
        self.assertEqual(clip(self.do('move',start=0),b['id'])['start'],200)
    def test_lock_blocks_cross_track_and_ripple(self):
        self.p['tracks'][0]['locked']=True
        with self.assertRaises(ValueError):self.do('reverse')
        self.p['tracks'][0]['locked']=False;self.p['tracks'][1]['locked']=True
        self.p['clips'].append(make_clip(self.p,'A1','a',150,0,60))
        with self.assertRaises(ValueError):self.do('delete',ripple=True)
    def test_track_types_and_controls(self):
        r=apply(self.p,dict(op='add_track',kind='image',name='Logo'))
        r=apply(r,dict(op='track',id=r['tracks'][-1]['id'],values={'solo':True,'mute':True,'locked':True}))
        self.assertTrue(r['tracks'][-1]['solo'])
        with self.assertRaises(ValueError):self.do('move',track='A1',start=0)
    def test_keyframes_split_preserve_curve(self):
        self.p=self.do('keyframe',field='x',frame=0,value=0)
        self.p=self.do('keyframe',field='x',frame=120,value=120)
        r=self.do('split',frame=90);b=next(c for c in r['clips'] if c['id']!=self.id)
        self.assertEqual(value_at(clip(r,self.id),'x',30),30)
        self.assertEqual(value_at(b,'x',30),90)
    def test_keyframe_validation_and_removal(self):
        with self.assertRaises(ValueError):self.do('keyframe',field='opacity',frame=121,value=.5)
        with self.assertRaises(ValueError):self.do('keyframe',field='volume',frame=1,value=float('nan'))
        self.p=self.do('keyframe',field='opacity',frame=0,value=.5)
        self.assertEqual(self.do('keyframe',field='opacity',frame=0,delete=True)['clips'][0]['keyframes']['opacity'],[])
    def test_freeze_reverse_crop_properties(self):
        r=self.do('freeze',source_frame=70,duration=90);self.assertTrue(clip(r,self.id)['freeze'])
        with self.assertRaises(ValueError):self.do('properties',values={'crop':[.6,0,.6,0]})
        with self.assertRaises(ValueError):self.do('properties',values={'path':'/etc/passwd'})
    def test_detach_link_move_unlink_replace(self):
        self.p=self.do('detach');b=next(c for c in self.p['clips'] if c['kind']=='audio')
        r=self.do('move',start=60);self.assertEqual(clip(r,b['id'])['start'],60);self.assertTrue(clip(r,self.id)['properties']['mute'])
        r=self.do('unlink');self.assertTrue(all(c['link'] is None for c in r['clips']))
        r=self.do('replace_audio',asset='a',source_in=0,track='A1');self.assertEqual(len(r['clips']),3)
    def test_frame_timecode_and_snapping(self):
        for fps in ('24','25','30','30000/1001','60'):
            for f in (0,1,1799,108005):self.assertEqual(parse_timecode(timecode(f,fps),fps),f)
        self.p['snapping']=True
        self.assertEqual(snap(self.p,33),30);self.assertEqual(snap(self.p,100),100)
        with self.assertRaises(ValueError):parse_timecode('00:00:00:30')
    def test_close_gaps_and_markers(self):
        r=apply(self.p,dict(op='close_gaps',track='V1'));self.assertEqual(r['clips'][0]['start'],0)
        r=apply(r,dict(op='marker',frame=10,label='Cut'));self.assertEqual(snap(r,12),10)
    def test_local_directions(self):
        self.assertEqual(edit_direction(self.p,'speed 1.25x',self.id,0)[0]['value'],1.25)
        self.assertEqual(edit_direction(self.p,'slip +10 frames',self.id,0)[0]['delta'],10)
        with self.assertRaises(ValueError):edit_direction(self.p,'make it magical',self.id,0)
    def test_overwrite_preserves_source_handles(self):
        p=apply(self.p,dict(op='insert',track='V1',asset='a',start=60,source_in=0,source_out=30,mode='overwrite'))
        ranges=sorted((c['start'],c['duration'],c['source_in'],c['source_out']) for c in p['clips'])
        self.assertEqual(ranges,[(30,30,60,90),(60,30,0,30),(90,60,120,180)])
    def test_linked_trim_split_delete_duplicate(self):
        self.p=self.do('detach')
        p=self.do('trim',edge='left',delta=10)
        self.assertEqual({(c['start'],c['duration']) for c in p['clips']},{(40,110)})
        p=self.do('split',frame=90)
        self.assertEqual(len(p['clips']),4)
        self.assertEqual(len({c['link'] for c in p['clips']}),2)
        self.assertEqual(len(self.do('duplicate')['clips']),4)
        self.assertEqual(len(self.do('delete',ripple=True)['clips']),0)
        self.p['tracks'][1]['locked']=True
        with self.assertRaises(ValueError):self.do('trim',edge='right',delta=-1)
    def test_join_preserves_split_keyframes(self):
        self.p=self.do('keyframe',field='x',frame=0,value=0)
        self.p=self.do('keyframe',field='x',frame=120,value=120)
        p=self.do('split',frame=90);b=next(c for c in p['clips'] if c['id']!=self.id)
        p=apply(p,dict(op='join',id=self.id,other=b['id']))
        for f in range(120):self.assertAlmostEqual(value_at(p['clips'][0],'x',f),f)
    def test_frame_rate_conversion_preserves_seconds(self):
        p=apply(self.p,dict(op='fps',value='60'))
        self.assertEqual((p['clips'][0]['start'],p['clips'][0]['duration'],p['clips'][0]['source_in']),(60,240,120))
        self.assertEqual(apply(p,dict(op='fps',value='30')),self.p)
    def test_transitions_preserve_overlaps_and_source(self):
        b=make_clip(self.p,'V1','a',150,180,240);self.p['clips'].append(b)
        p=self.do('transition',kind='dissolve',frames=10)
        self.assertEqual(clip(p,b['id'])['start'],140)
        self.assertEqual(clip(p,b['id'])['source_in'],180)
        self.assertEqual(clip(p,b['id'])['properties']['transition_in'],10)
        p=apply(p,dict(op='close_gaps',track='V1'))
        self.assertEqual(clip(p,b['id'])['start'],110)  # Leading 30f gap closes; overlap stays.
    def test_captions_import_export_unicode(self):
        from pipeline_core.timeline_captions import import_captions,export_srt
        text='1\n00:00:00,000 --> 00:00:01,000\nCafé 世界\n\n2\n00:00:01,500 --> 00:00:02,000\n<b>NextPlay</b>\n'
        p=import_captions(self.p,text)
        self.assertIn('Café 世界',export_srt(p));self.assertNotIn('<b>',export_srt(p))
        with self.assertRaises(ValueError):import_captions(self.p,'invalid')
    def test_ai_commands_use_current_timeline_and_are_validated(self):
        client=Mock();client.responses.create.return_value=SimpleNamespace(output_text=json.dumps({'summary':'Move selected clip','commands':[json.dumps({'op':'move','id':self.id,'start':0})]}))
        result=ai_direction(self.p,'move to beginning',self.id,0,client)
        self.assertEqual(result['commands'][0]['start'],0)
        prompt=client.responses.create.call_args.kwargs['input'];self.assertIn(self.id,prompt);self.assertNotIn('"path":',prompt)
        client.responses.create.return_value=SimpleNamespace(output_text=json.dumps({'summary':'Bad','commands':[json.dumps({'op':'properties','id':self.id,'values':{'opacity':5}})]}))
        with self.assertRaises(ValueError):ai_direction(self.p,'bad',self.id,0,client)

class StoreRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        cls.media=cls.root/'source.mp4'
        run_ffmpeg(['-f','lavfi','-i','testsrc2=s=160x120:r=30:d=4','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=4','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest','-y',str(cls.media)])
        from PIL import Image
        Image.new('RGBA',(64,64),(255,0,0,128)).save(cls.root/'logo.png')
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def setUp(self):
        self.workspace=SimpleNamespace(root=self.root,ads_dir=self.root/'ads',analysis_dir=self.root/'analysis',load_candidates=lambda:[])
        self.workspace.ads_dir.mkdir(exist_ok=True);self.workspace.analysis_dir.mkdir(exist_ok=True)
        self.store=TimelineStore(self.workspace,uid())
        self.p=new_project();self.p['canvas']={'width':160,'height':120,'background':'#112233'};self.p['magnetic']=False
        a=self.store.asset(self.media,30,{'rights_status':'cleared','creator':'synthetic','source_url':'test:generated'})
        self.p['assets'][a['id']]=a;self.a=a
        c=make_clip(self.p,'V1',a['id'],0,0,60);self.p['clips']=[c];self.id=c['id']
    def output(self):return self.root/(uid()+'.mp4')
    def pixels(self,path,t=0):
        r=subprocess.run([FFMPEG,'-v','error','-ss',str(t),'-i',str(path),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'],capture_output=True,check=True)
        return r.stdout
    def test_version_restore_conflict_and_corrupt_head(self):
        p=self.store.commit(self.p,0,'Original');p=self.store.edit([dict(op='reverse',id=self.id)],p['revision'],'Reverse')
        original=self.store.revision(1)
        restored=self.store.restore(1,p['revision']);self.assertEqual(restored['revision'],3);self.assertFalse(restored['clips'][0]['reverse']);self.assertEqual(original,self.store.revision(1))
        with self.assertRaises(Conflict):self.store.edit([],1)
        (self.store.directory/'head.json').write_text('broken')
        self.assertEqual(self.store.load()['revision'],3)
        (self.store.directory/'revision-00000004.json').write_text('{broken')
        self.assertEqual(self.store.commit(restored,3,'Recovered')['revision'],5)
    def test_offline_legacy_timing_and_relink(self):
        from unittest.mock import patch
        legacy={'name':self.store.name,'segments':[{'video_id':'missing','start':1,'end':2,'gap_before':.5,'text':'Offline caption'}]}
        with patch('pipeline_core.ad_editor.load_ad_plans',return_value=[legacy]), patch('pipeline_core.ad_editor._analysis_index',return_value=({},{})), patch('pipeline_core.ad_editor._candidate_index',return_value={}):
            p=self.store.migrate()
        c=next(x for x in p['clips'] if x['kind']=='video')
        self.assertEqual((c['start'],c['duration'],c['source_in']),(15,30,30))
        self.assertTrue(p['assets'][c['asset']]['missing'])
        p['assets'][self.a['id']]=self.a
        p=apply(p,dict(op='replace_source',id=c['id'],asset=self.a['id']))
        self.assertEqual(clip(p,c['id'])['provenance']['video_id'],'missing');self.assertNotIn('warnings',p)
        out=self.output();render(p,self.root,out);self.assertTrue(out.exists())
    def test_import_unique_and_provenance(self):
        p=self.store.commit(self.p,0,'Original');before=self.media.read_bytes()
        p,a=self.store.import_media(before,'../../source.mp4',p['revision'],{'rights_status':'owned'})
        self.assertEqual(a['name'],'source.mp4');self.assertNotEqual(a['path'],'source.mp4');self.assertEqual(self.media.read_bytes(),before)
        self.assertEqual(a['provenance']['rights_status'],'owned')
        with self.assertRaises(ValueError):self.store.import_media(b'bad','evil.env',p['revision'])
    def test_storage_symlink_rejected_before_directory_creation(self):
        with tempfile.TemporaryDirectory() as inside, tempfile.TemporaryDirectory() as outside:
            (Path(inside)/'nextplay').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(ValueError):TimelineStore(SimpleNamespace(root=Path(inside)),'unsafe')
            self.assertEqual(list(Path(outside).iterdir()),[])
    def test_path_confinement_and_symlinks(self):
        for path in ('../../etc/passwd','/etc/passwd','../source.mp4'):
            with self.assertRaises(ValueError):safe_asset(self.root,{'path':path})
        (self.root/'escape.mp4').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):safe_asset(self.root,{'path':'escape.mp4'})
        (self.root/'escape.mp4').unlink()
    def test_render_baseline_and_non_destructive_export(self):
        before=self.media.read_bytes();p=self.store.commit(self.p,0,'Original');a=self.store.export(p,'custom',True);b=self.store.export(p,'custom',False)
        self.assertNotEqual(a['url'],b['url']);self.assertEqual(self.media.read_bytes(),before);self.assertEqual(len(self.store.history()),1)
        out=self.root/b['url'].removeprefix('/file/');self.assertTrue(out.exists());self.assertEqual(json.loads(out.with_suffix('.json').read_text())['assets'][self.a['id']]['provenance']['rights_status'],'cleared')
        with self.assertRaises(ValueError):render(p,self.root,out)
    def test_render_multitrack_text_logo_audio_keyframes(self):
        p=apply(self.p,dict(op='add_track',kind='image',name='Logo'));a=self.store.asset(self.root/'logo.png',30);p['assets'][a['id']]=a
        p=apply(p,dict(op='insert',track=p['tracks'][-1]['id'],asset=a['id'],start=0,source_out=60,ripple=False));logo=p['clips'][-1]
        p=apply(p,dict(op='properties',id=logo['id'],values={'scale':.35,'x':40,'opacity':.7}))
        p=apply(p,dict(op='insert',track='T1',start=0,source_out=60,text='NextPlay',ripple=False));text=p['clips'][-1]
        p=apply(p,dict(op='properties',id=text['id'],values={'font_size':18,'y':30,'text_box':True}))
        p=apply(p,dict(op='insert',track='A1',asset=self.a['id'],start=15,source_in=0,source_out=45,ripple=False));audio=p['clips'][-1]
        p=apply(p,dict(op='track',id='A1',values={'ducking':True}))
        for field,start,finish in [('x',-20,20),('y',-10,10),('scale',.8,1.2),('rotation',0,20),('opacity',.5,1),('volume',0,1),('pan',-1,1)]:
            p=apply(p,dict(op='keyframe',id=self.id,field=field,frame=0,value=start));p=apply(p,dict(op='keyframe',id=self.id,field=field,frame=60,value=finish))
        out=self.output();render(p,self.root,out);self.assertGreater(out.stat().st_size,1000)
        self.assertNotEqual(self.pixels(out,.2),self.pixels(out,1.5))
    def test_render_speed_reverse_freeze(self):
        baseline=self.output();render(self.p,self.root,baseline)
        rev=apply(self.p,dict(op='reverse',id=self.id));out=self.output();render(rev,self.root,out)
        self.assertNotEqual(self.pixels(baseline,.1),self.pixels(out,.1))
        freeze=apply(self.p,dict(op='freeze',id=self.id,source_frame=15,duration=60));out=self.output();render(freeze,self.root,out)
        first,last=self.pixels(out,.1),self.pixels(out,1.5)
        self.assertLess(sum(abs(a-b) for a,b in zip(first,last))/len(first),1)
        fast=apply(self.p,dict(op='speed',id=self.id,value=4));out=self.output();render(fast,self.root,out)
        info=subprocess.run([FFMPEG,'-i',str(out)],capture_output=True,text=True).stderr;self.assertIn('00:00:00.50',info)
    def test_render_transform_color_crop_fades_gap_solo(self):
        p=apply(self.p,dict(op='properties',id=self.id,values={'crop':[.1,.1,.1,.1],'fit':'fill','flip_x':True,'flip_y':True,'brightness':.1,'contrast':1.2,'saturation':.5,'fade_in':10,'fade_out':10,'transition_in':10,'transition_out':10}))
        p=apply(p,dict(op='move',id=self.id,start=15));out=self.output();render(p,self.root,out)
        pixels=self.pixels(out,0);self.assertEqual(len(pixels),160*120*3)
        self.assertLess(max(pixels[0:3])-min(pixels[0:3]),50)
        p=apply(p,dict(op='track',id='V1',values={'hidden':True}));out=self.output();render(p,self.root,out)
        self.assertEqual(self.pixels(out,0),self.pixels(out,1))
    def test_thumbnails_and_waveforms(self):
        d=self.store.visualizations(self.p,self.a['id']);self.assertEqual(set(d),{'thumbnail','waveform'})
        self.assertEqual(d,self.store.visualizations(self.p,self.a['id']))
    def samples(self,path):
        r=subprocess.run([FFMPEG,'-v','error','-i',str(path),'-vn','-ar','48000','-ac','2','-f','f32le','-'],capture_output=True,check=True)
        return struct.unpack('<'+'f'*(len(r.stdout)//4),r.stdout)
    def test_render_audio_volume_pan_fades_and_mute(self):
        p=apply(self.p,dict(op='keyframe',id=self.id,field='pan',frame=0,value=-1))
        p=apply(p,dict(op='keyframe',id=self.id,field='pan',frame=60,value=1))
        p=apply(p,dict(op='properties',id=self.id,values={'fade_in':15,'fade_out':15,'volume':.5}))
        out=self.output();render(p,self.root,out);s=self.samples(out)
        def rms(start,end,ch):
            values=s[round(start*48000)*2+ch:round(end*48000)*2:2]
            return math.sqrt(sum(x*x for x in values)/len(values))
        self.assertGreater(rms(.5,.75,0),rms(.5,.75,1)*1.2)
        self.assertGreater(rms(1.25,1.5,1),rms(1.25,1.5,0)*1.2)
        self.assertLess(rms(0,.1,0),rms(.5,.75,0)*.5)
        p=apply(p,dict(op='track',id='V1',values={'mute':True}));out=self.output();render(p,self.root,out)
        self.assertLess(max(abs(x) for x in self.samples(out)),.0001)
        self.assertGreater(sum(self.pixels(out,.5)),10000) # Audio mute must not hide video.
    def test_render_ducking_and_audio_solo(self):
        tone=self.root/'foreground.wav'
        run_ffmpeg(['-f','lavfi','-i','sine=frequency=880:sample_rate=48000:duration=2','-y',str(tone)])
        a=self.store.asset(tone,30);p=apply(self.p,dict(op='properties',id=self.id,values={'mute':True}));p['assets'][a['id']]=a
        p=apply(p,dict(op='insert',track='A1',asset=self.a['id'],start=0,source_in=0,source_out=60,ripple=False))
        p=apply(p,dict(op='track',id='A1',values={'ducking':True}))
        p=apply(p,dict(op='add_track',kind='audio',name='Voice'))
        tid=p['tracks'][-1]['id'];p=apply(p,dict(op='insert',track=tid,asset=a['id'],start=30,source_in=0,source_out=30,ripple=False))
        def amplitude(samples,frequency,time):
            start=round(time*48000);count=4800
            return abs(sum(samples[(start+j)*2]*complex(math.cos(2*math.pi*frequency*j/48000),-math.sin(2*math.pi*frequency*j/48000)) for j in range(count)))*2/count
        out=self.output();render(p,self.root,out);samples=self.samples(out)
        self.assertLess(amplitude(samples,440,1.4),amplitude(samples,440,.4)*.4)
        p=apply(p,dict(op='track',id=tid,values={'solo':True}));out=self.output();render(p,self.root,out);samples=self.samples(out)
        self.assertLess(amplitude(samples,440,.4),.001)
        self.assertGreater(amplitude(samples,880,1.4),.05)
    def test_render_cross_dissolve_pixel_blend(self):
        p=deepcopy(self.p);p['clips']=[]
        for color,start in [('red',0),('blue',30)]:
            path=self.root/(color+'.mp4');run_ffmpeg(['-f','lavfi','-i','color=c='+color+':s=160x120:r=30:d=1','-c:v','libx264','-pix_fmt','yuv420p','-y',str(path)])
            a=self.store.asset(path,30);p['assets'][a['id']]=a;p['clips'].append(make_clip(p,'V1',a['id'],start,0,30))
        p=apply(p,dict(op='transition',id=p['clips'][0]['id'],kind='dissolve',frames=15))
        out=self.output();render(p,self.root,out)
        first=self.pixels(out,.1)[:3];middle=self.pixels(out,.75)[:3];last=self.pixels(out,1.3)[:3]
        self.assertGreater(first[0],200);self.assertGreater(last[2],200)
        self.assertGreater(middle[0],80);self.assertGreater(middle[2],80)
    def test_constant_opacity_pixel_blending(self):
        path=self.root/'opacity-red.mp4'
        run_ffmpeg(['-f','lavfi','-i','color=c=red:s=160x120:r=30:d=1','-c:v','libx264','-pix_fmt','yuv420p','-y',str(path)])
        a=self.store.asset(path,30)
        for alpha in (0,.5,1):
            p=deepcopy(self.p);p['assets'][a['id']]=a
            p['canvas']['background']='#000000'
            p['clips']=[make_clip(p,'V1',a['id'],0,0,30)]
            p=apply(p,dict(op='properties',id=p['clips'][0]['id'],values={'opacity':alpha}))
            out=self.output();render(p,self.root,out)
            red,green,blue=self.pixels(out,.5)[:3]
            self.assertLess(abs(red-255*alpha),12)
            self.assertLess(max(green,blue),12)
    def test_export_presets_and_fractional_fps(self):
        p=apply(self.p,dict(op='fps',value='30000/1001'))
        p=apply(p,dict(op='properties',id=self.id,values={'fit':'stretch'}))
        out=self.output();render(p,self.root,out,'square',True)
        info=subprocess.run([FFMPEG,'-i',str(out)],capture_output=True,text=True).stderr
        self.assertIn('640x640',info);self.assertIn('29.97',info)
    def test_animated_opacity_matches_frame_time_after_scaling(self):
        path=self.root/'animated-opacity-red.mp4'
        run_ffmpeg(['-f','lavfi','-i','color=c=red:s=160x120:r=30:d=1','-c:v','libx264','-pix_fmt','yuv420p','-y',str(path)])
        a=self.store.asset(path,30);p=deepcopy(self.p);p['assets'][a['id']]=a
        p['canvas']={'width':320,'height':240,'background':'#000000'}
        c=make_clip(p,'V1',a['id'],0,0,30);p['clips']=[c]
        p=apply(p,dict(op='keyframe',id=c['id'],field='opacity',frame=0,value=0))
        p=apply(p,dict(op='keyframe',id=c['id'],field='opacity',frame=30,value=1))
        out=self.output();render(p,self.root,out)
        for time in (.1,.5,.9):
            red=self.pixels(out,time)[0]
            self.assertLess(abs(red-255*time),15)
    def test_render_failure_leaves_history_and_outputs_intact(self):
        p=self.store.commit(self.p,0,'Original');out=self.output()
        p['assets'][self.a['id']]['path']='missing.mp4'
        with self.assertRaises(ValueError):render(p,self.root,out)
        self.assertFalse(out.exists());self.assertFalse(out.with_name(out.stem+'.partial.mp4').exists());self.assertEqual(len(self.store.history()),1)
    def test_text_alpha_preserves_footage_outside_glyphs(self):
        baseline=self.output();render(self.p,self.root,baseline)
        p=apply(self.p,dict(op='insert',track='T1',start=0,source_out=60,text='Text',ripple=False))
        p=apply(p,dict(op='properties',id=p['clips'][-1]['id'],values={'font_size':18}))
        out=self.output();render(p,self.root,out)
        a,b=self.pixels(baseline,.5),self.pixels(out,.5)
        self.assertLess(max(abs(x-y) for x,y in zip(a[:30],b[:30])),12)
        self.assertNotEqual(a,b)

if __name__=='__main__':unittest.main()
