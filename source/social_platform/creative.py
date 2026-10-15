from __future__ import annotations
import base64,json,os,re,subprocess
from datetime import datetime,timezone
from pathlib import Path
from openai import OpenAI
from ffmpeg_bin import FFMPEG
from .store import CHANNELS,LIMITS

def client():
    if not os.getenv('OPENAI_API_KEY'):raise ValueError('AI is not connected. Configure the server’s OpenAI key.')
    return OpenAI(timeout=90,max_retries=0)

def frames(path):
    result=[]
    for t in [0,2,5]:
        r=subprocess.run([FFMPEG,'-v','error','-ss',str(t),'-i',str(path),'-vf','scale=480:-2','-frames:v','1','-f','image2pipe','-vcodec','mjpeg','pipe:1'],capture_output=True,timeout=20)
        if r.returncode==0 and r.stdout:result.append({'type':'input_image','image_url':'data:image/jpeg;base64,'+base64.b64encode(r.stdout).decode(),'detail':'low'})
    return result

def generate(post,brand,root,direction='',api=None):
    api=api or client()
    props={k:{'type':'string'} for k in ['caption','hook','cta']}
    props['hashtags']={'type':'array','items':{'type':'string'},'maxItems':8}
    variant={'type':'object','properties':props,'required':list(props),'additionalProperties':False}
    schema={'type':'object','properties':{c:variant for c in CHANNELS},'required':list(CHANNELS),'additionalProperties':False}
    prompt=('Create social copy for one video. Return distinct TikTok, Instagram and Facebook versions. '
            'Use only supported details from the brief and supplied frames. Frames are sparse, have no audio, and may omit most of the video. '
            'Never claim you heard speech or watched the full video. Do not invent results, testimonials, discounts, products or factual claims. '
            'Captions should already include the hook and CTA where useful. Return hashtags without #, concise hooks, and a CTA. '
            'TikTok: conversational and concise; Instagram: polished, readable; Facebook: warm and conversational. '
            f'Character budgets including hashtags: {LIMITS}.\nBrand: {json.dumps(brand)}\n'
            f'Title: {post["title"]}\nBrief: {post["brief"]}\nRevision direction: {str(direction)[:2000]}')
    content=[{'type':'input_text','text':prompt}]
    if post.get('media'):
        path=(Path(root)/post['media']['path']).resolve()
        path.relative_to(Path(root).resolve())
        if path.is_file():content.extend(frames(path))
    try:
        r=api.responses.create(model=os.getenv('NEXTPLAY_COPY_MODEL','gpt-4.1-mini'),input=[{'role':'user','content':content}],text={'format':{'type':'json_schema','name':'social_variants','strict':True,'schema':schema}},store=False)
        data=json.loads(r.output_text)
    except Exception as exc:
        raise ValueError('AI copy could not be generated ('+type(exc).__name__+'). Your draft is unchanged.') from None
    if set(data)!=set(CHANNELS):raise ValueError('AI returned incomplete channel variants.')
    for c,v in data.items():
        if not isinstance(v,dict) or any(not isinstance(v.get(k),str) for k in ['caption','hook','cta']) or not isinstance(v.get('hashtags'),list) or any(not isinstance(h,str) for h in v['hashtags']):raise ValueError('AI returned invalid copy.')
        if len(v['caption']+' '+' '.join('#'+h.lstrip('#') for h in v['hashtags']))>LIMITS[c]:raise ValueError('AI returned a caption over the platform limit. Try a shorter direction.')
    return data

def research_music(brief,platform='tiktok',region='US',api=None):
    if platform not in CHANNELS:raise ValueError('Choose a valid platform.')
    api=api or client()
    prompt=(f'Research recent music/sound trends for {platform} in {region}, as of {datetime.now(timezone.utc).date()}. '
            f'Video mood and brief: {str(brief)[:3000]}. Search the web and provide up to five relevant sounds with clickable citations, '
            'source dates and a reason each fits. Clearly flag when evidence is stale or unavailable; do not invent trends, usage counts, growth rates or license availability. '
            'Native sounds may need an in-app step. No recommendation establishes commercial licensing. Include a licensed instrumental alternative category. '
            'Provide a short plain-text report with source links.')
    try:
        r=api.responses.create(model=os.getenv('NEXTPLAY_RESEARCH_MODEL','gpt-4.1-mini'),tools=[{'type':'web_search'}],input=prompt,store=False)
    except Exception as exc:raise ValueError('Music research failed ('+type(exc).__name__+'). Try again later.') from None
    sources=[]
    for out in r.output:
        for part in getattr(out,'content',[]) or []:
            for a in getattr(part,'annotations',[]) or []:
                if getattr(a,'type','')=='url_citation':
                    url=getattr(a,'url','')
                    if url.startswith('https://'):sources.append({'title':getattr(a,'title',url),'url':url})
    return {'text':r.output_text,'sources':sources,'at':datetime.now(timezone.utc).isoformat(),'platform':platform,'region':region}
