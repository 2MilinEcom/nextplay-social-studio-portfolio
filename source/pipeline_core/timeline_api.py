"""Routes shared by the existing dashboard and the dedicated timeline editor."""
from pathlib import Path
from urllib.parse import parse_qs
from copy import deepcopy
import json
import os

from .timeline import apply, validate
from .timeline_captions import import_captions, export_srt
from .timeline_store import TimelineStore, Conflict, edit_direction

STATIC = Path(__file__).parent / "static"

def ai_direction(project,instruction,selection,playhead,client=None):
    from openai import OpenAI
    if client is None and not os.getenv("OPENAI_API_KEY"):
        raise ValueError("AI requires the existing OPENAI_API_KEY configuration. Local precise directions remain available.")
    client=client or OpenAI(timeout=45,max_retries=0)
    context=deepcopy(project)
    context.pop("provenance",None)
    for c in context["clips"]:
        c["provenance"]={k:v for k,v in c.get("provenance",{}).items() if k in {"role","text","segment_id","reason"}}
    for a in context["assets"].values():
        a.pop("path",None)
    schema={"type":"object","properties":{"summary":{"type":"string"},"commands":{"type":"array","items":{"type":"string"},"maxItems":30}},"required":["summary","commands"],"additionalProperties":False}
    prompt=("You edit the CURRENT saved short-form multitrack timeline below. Return an atomic list of JSON edit command strings, never a replacement project. "
            "Preserve all other tracks, media IDs, rights and provenance. Do not invent media or claims. All times are integer project frames. "
            'Each command string MUST contain a complete JSON object with an "op" field and named fields. '
            'Example: {"summary":"Adjusted title","commands":["{\\"op\\":\\"properties\\",\\"id\\":\\"existing-id\\",\\"values\\":{\\"opacity\\":0.5,\\"x\\":20}}"]}. '
            'Never return function calls such as properties(id,values), Markdown, or Python. The notation below only documents argument names. '
            "Commands: insert(track,asset,start,source_in,source_out,ripple), move(id,start,track,magnetic), trim/ripple_trim(id,edge:left|right,delta), slip/slide/roll(id,delta), "
            "split(id,frame:absolute), join(id,other), delete(id,ripple), duplicate(id), speed(id,value:.25..4), reverse(id), freeze(id,source_frame,duration), unfreeze(id), "
            "properties(id,values), keyframe(id,field,frame:clip-local,value,interpolation:linear|hold,delete), detach(id,track), unlink(id), replace_audio(id,asset,source_in,track), "
            "add_track(kind,name), track(id,values:{locked,mute,solo,ducking,name}), close_gaps(track), mode(magnetic,snapping), canvas(values), marker(frame,label). "
            "Properties allowed: x,y in canvas pixels,scale:.05..8,rotation degrees,opacity:0..1,volume:0..4,pan:-1..1,brightness:-1..1,contrast:0..3,saturation:0..3," 
            "fit:fit|fill|stretch,crop:[left,top,right,bottom] fractions,flip_x,flip_y,mute,fade_in,fade_out,transition_in,transition_out in frames,text,font_size,color,text_background,text_box. "
            "Keyframes supported for x,y,scale,rotation,opacity,volume,pan. For source handles, respect assets duration_frames. Don't edit locked tracks. "
            "Roll requires adjacent clips and slide requires both neighbors. Transition fades are alpha dissolves; overlap clips on stacked tracks for crossfades. "
            "If instruction is ambiguous or impossible return no commands and explain what is needed.\n"
            f"Selection: {selection}; playhead: {playhead}\nUser direction: {instruction[:2400]}\nTimeline: {json.dumps(context,ensure_ascii=False)}")
    try:
        response=client.responses.create(model=os.getenv("NEXTPLAY_AI_MODEL","gpt-5.6-terra"),input=prompt,
        text={"format":{"type":"json_schema","name":"timeline_edits","strict":True,"schema":schema}})
    except Exception as exc:
        raise ValueError("AI provider request failed ("+type(exc).__name__+"). Check the configured model, connection and API account.") from None
    try:
        data=json.loads(response.output_text)
        commands=[json.loads(s) for s in data["commands"]]
        if not isinstance(data["summary"],str) or any(not isinstance(c,dict) for c in commands):
            raise ValueError()
    except (ValueError,TypeError,KeyError):
        raise ValueError("AI returned an invalid edit format; the timeline was preserved. Try a more precise direction.") from None
    if len(commands)>30:
        raise ValueError("AI command limit exceeded")
    p=project
    for command in commands:
        p=apply(p,command)
    return dict(commands=commands,summary=data["summary"],provider="openai")

def get_route(handler, parsed):
    if parsed.path == "/timeline":
        handler._html((STATIC/"timeline.html").read_text())
        return True
    if parsed.path.startswith("/timeline-static/"):
        name=parsed.path.rsplit("/",1)[-1]
        if name not in {"timeline.css","timeline.js"}:
            handler.send_error(404); return True
        body=(STATIC/name).read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type","text/css" if name.endswith("css") else "text/javascript")
        handler.send_header("Content-Length",str(len(body)))
        handler.end_headers(); handler.wfile.write(body)
        return True
    return False

def post_route(handler,parsed):
    if not parsed.path.startswith("/api/timeline/"):
        return False
    try:
        if handler.headers.get("X-NextPlay") != "timeline":
            raise ValueError("Missing same-origin editor header")
        _,workspace=handler.context.snapshot()
        action=parsed.path.rsplit("/",1)[-1]
        if action=="import":
            query=parse_qs(parsed.query)
            name=query.get("name",["Untitled"])[0][:200]
            length=int(handler.headers.get("Content-Length","0"))
            if not 0 < length <= 512*1024*1024:
                raise ValueError("Import limit is 512 MB")
            data=handler.rfile.read(length)
            if len(data)!=length:
                raise ValueError("Incomplete upload")
            rights=query.get("rights",["unknown"])[0]
            if rights not in {"unknown","cleared","owned"}:
                raise ValueError("Invalid import rights")
            with handler.context.lock:
                p,a=TimelineStore(workspace,name).import_media(data,query.get("filename",[""])[0],int(query.get("revision",["0"])[0]),dict(rights_status=rights,import_method="local upload"))
            handler._json(dict(project=p,asset=a)); return True
        body=handler._body_json()
        name=str(body.get("name","Untitled"))[:200]
        store=TimelineStore(workspace,name)
        with handler.context.lock:
            p=store.load()
            if action=="load":
                result=dict(project=p)
            elif action=="history":
                result=dict(history=store.history())
            elif action=="captions":
                edited=import_captions(p,body["text"],body.get("track","T1"),body.get("offset",0))
                result=dict(project=store.commit(edited,body.get("revision"),"Imported captions"))
            elif action=="export-captions":
                result=dict(text=export_srt(p))
            elif action=="edit":
                result=dict(project=store.edit(body.get("commands",[]),body.get("revision"),body.get("label","Edit")))
            elif action=="restore":
                result=dict(project=store.restore(body.get("restore_revision"),body.get("revision")))
            elif action=="visualizations":
                result=store.visualizations(p,body["asset"])
            elif action in {"render","direction"}:
                if body.get("revision")!=p["revision"]:
                    raise Conflict("Draft changed; reload before continuing")
                result=None
            else:
                raise ValueError("Unknown timeline action")
        # Long rendering / AI operations must not block autosave and state reads.
        if action=="render":
            result=store.export(p,body.get("preset","custom"),bool(body.get("draft")))
        elif action=="direction":
            try:
                commands=edit_direction(p,body.get("instruction",""),body.get("selection"),body.get("playhead",0))
                result=dict(commands=commands,summary="Applied precise local timeline direction",provider="local")
            except ValueError:
                result=ai_direction(p,body.get("instruction",""),body.get("selection"),body.get("playhead",0))
        handler._json(result)
    except Exception as exc:
        handler._json(dict(error=str(exc)),status=409 if isinstance(exc,Conflict) else 400)
    return True
