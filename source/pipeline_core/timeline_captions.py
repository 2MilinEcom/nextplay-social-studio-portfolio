"""Editable SRT/VTT captions, represented by ordinary text timeline clips."""
import re
from .timeline import make_clip, fps_value, validate
from copy import deepcopy

def timestamp(value):
    parts=value.replace(',','.').split(':')
    if len(parts) not in {2,3}:
        raise ValueError('Invalid subtitle timestamp')
    parts=[float(x) for x in parts]
    return sum(x*60**i for i,x in enumerate(reversed(parts)))

def import_captions(project,text,track_id='T1',offset=0):
    p=deepcopy(project)
    track=next((t for t in p['tracks'] if t['id']==track_id),None)
    if not track or track['kind']!='text' or track.get('locked'):
        raise ValueError('Choose an unlocked text track')
    count=0
    for block in re.split(r'\n\s*\n',text.replace('\r','').strip()):
        lines=block.splitlines()
        timing=next((i for i,line in enumerate(lines) if '-->' in line),None)
        if timing is None:
            continue
        match=re.fullmatch(r'\s*([\d:.,]+)\s*-->\s*([\d:.,]+)(?:\s+.*)?',lines[timing])
        if not match:
            raise ValueError('Malformed subtitle cue')
        start=round(timestamp(match[1])*fps_value(p))+offset
        end=round(timestamp(match[2])*fps_value(p))+offset
        if start<0 or end<=start:
            raise ValueError('Subtitle cues must have positive duration')
        content='\n'.join(lines[timing+1:])
        content=re.sub(r'<[^>]*>','',content)
        c=make_clip(p,track_id,start=start,source_out=end-start,text=content)
        c['properties'].update(y=p['canvas']['height']*.36,font_size=max(12,round(p['canvas']['height']*.027)),text_box=True)
        c['provenance']={'caption_import':'SRT/VTT','cue':count+1}
        p['clips'].append(c);count+=1
    if not count:
        raise ValueError('No subtitle cues found')
    return validate(p)

def export_srt(project):
    def stamp(frame):
        ms=round(frame/fps_value(project)*1000)
        h,ms=divmod(ms,3600000);m,ms=divmod(ms,60000);s,ms=divmod(ms,1000)
        return f'{h:02}:{m:02}:{s:02},{ms:03}'
    clips=sorted((c for c in project['clips'] if c['kind']=='text'),key=lambda c:c['start'])
    return '\n\n'.join(f"{i}\n{stamp(c['start'])} --> {stamp(c['start']+c['duration'])}\n{c['properties']['text']}" for i,c in enumerate(clips,1))+'\n'
