"""Opt-in API smoke test. Synthetic timeline only; never prints credentials."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from pipeline_core.timeline import new_project,make_clip,apply
from pipeline_core.timeline_api import ai_direction

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--env-file',required=True);args=parser.parse_args()
    load_dotenv(args.env_file,override=False)
    p=new_project('Synthetic AI test');p['magnetic']=False
    c=make_clip(p,'T1',start=0,source_out=90,text='Synthetic title');p['clips']=[c]
    try:
        from openai import OpenAI
        client=OpenAI(timeout=45,max_retries=0)
        result=ai_direction(p,'Set the selected title opacity to 0.5 and move it 20 pixels to the right. Keep its timing and all other tracks unchanged.',c['id'],0,client)
        for command in result['commands']:p=apply(p,command)
        edited=next(x for x in p['clips'] if x['id']==c['id'])
        assert edited['properties']['opacity']==.5 and edited['properties']['x']==20
        assert edited['start']==0 and edited['duration']==90
        print('PASS: live AI generated validated edits to the current synthetic timeline')
    except Exception as exc:
        print('FAILED: '+type(exc).__name__)
        # Do not print provider error payloads; they may echo credential details.
        sys.exit(1)
