"""Opt-in live AI workflow using generated speech and video, never user media."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pipeline_core.timeline_render import run_ffmpeg
from pipeline_core.analyzer import OpenAIAnalyzer
from pipeline_core.creative_quality import OpenAICreativeReviewer
from pipeline_core.timeline_api import ai_direction
from pipeline_core.timeline import new_project,make_clip,apply,clip
from pipeline_core.models import Product
from pipeline_core.storage import atomic_write_json

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--env-file',required=True);parser.add_argument('--audio',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(args.env_file,override=False)
    from openai import OpenAI
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=True)
    video=root/'generated-test.mp4'
    run_ffmpeg(['-f','lavfi','-i','color=c=blue:s=320x240:r=30:d=12','-i',str(args.audio),'-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest','-y',str(video)])
    product=Product('test:generated','123','Synthetic blue test card',description='A generated blue color card used only to test video analysis. No real product or user footage.')
    job=dict(product=product.to_dict(),source=dict(local_path=str(video),video_id='synthetic',creator='Synthetic fixture',tiktok_url='test:generated'))
    analyzer=OpenAIAnalyzer(analysis_model='gpt-5.6-terra',frame_count=3);analyzer.client=OpenAI(timeout=60,max_retries=0)
    result=analyzer.analyze_job(job);assert result['status']=='complete';assert result['transcript'].get('segments')
    atomic_write_json(root/'analysis.json',result);print('PASS: live generated-speech transcription and video analysis',flush=True)
    plan=dict(name='Synthetic review',output_path=str(video),segments=result['segments'],duration_seconds=result['duration_seconds'],target_duration_seconds=12)
    reviewer=OpenAICreativeReviewer(model='gpt-5.6-terra',frame_count=4);reviewer.client=OpenAI(timeout=60,max_retries=0)
    quality=reviewer.review(product,plan);assert 0<=quality['overall']<=100
    atomic_write_json(root/'quality.json',quality);print('PASS: live finished-video creative review',flush=True)
    p=new_project('Synthetic live edit');c=make_clip(p,'T1',start=0,source_out=90,text='Synthetic title');p['clips']=[c]
    edits=ai_direction(p,'Set selected title opacity to 0.7 and shift it 12 pixels right, keeping timing unchanged.',c['id'],0)
    for command in edits['commands']:p=apply(p,command)
    assert clip(p,c['id'])['properties']['opacity']==.7 and clip(p,c['id'])['properties']['x']==12 and clip(p,c['id'])['duration']==90
    atomic_write_json(root/'timeline.json',p);print('PASS: live validated timeline-aware AI editing',flush=True)
    atomic_write_json(root/'results.json',dict(transcription=True,analysis=True,creative_review=True,timeline_ai=True,media='generated synthetic fixtures only'))
if __name__=='__main__':
    try:main()
    except Exception as exc:
        # Provider error payloads can echo credential fragments. Never print them.
        import traceback
        print('FAILED: '+type(exc).__name__,flush=True)
        print([(Path(x.filename).name,x.lineno,x.name) for x in traceback.extract_tb(exc.__traceback__)],flush=True)
        if str(exc).startswith('FFmpeg failed:'):print(str(exc),flush=True)
        sys.exit(1)
