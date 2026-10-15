"""Explicit live check: synthetic content only, no social posting."""
from social_platform.creative import generate,research_music
p={'title':'Daily momentum','brief':'A motivational video for entrepreneurs about taking one small step each day. No products, claims, discounts or testimonials. Invite viewers to follow for daily inspiration.','media':None}
try:
 data=generate(p,{'name':'NextPlay QA','voice':'Warm, clear, confident'},'.')
 print('LIVE COPY PASS:',len(data),'channel variants')
 for c,v in data.items():print(c+': '+v['caption'][:120])
except Exception as exc:print('LIVE COPY FAILED:',str(exc))
try:
 r=research_music('An uplifting entrepreneur video with spoken narration. Prefer instrumental options.','tiktok','US')
 print('LIVE MUSIC PASS:',len(r['sources']),'cited web sources;',len(r['text']),'characters')
except Exception as exc:print('LIVE MUSIC FAILED:',str(exc))
