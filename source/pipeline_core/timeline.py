"""Frame-based, non-destructive timeline. All edits are atomic validated transactions."""
from __future__ import annotations

import math
import re
import uuid
from copy import deepcopy
from fractions import Fraction

SCHEMA = 4
KINDS = {"video", "audio", "text", "image"}
ANIMATED = {"x", "y", "scale", "rotation", "opacity", "volume", "pan"}
DEFAULTS = dict(x=0., y=0., scale=1., rotation=0., opacity=1., volume=1., pan=0.,
                brightness=0., contrast=1., saturation=1., fit="fit", crop=[0., 0., 0., 0.],
                flip_x=False, flip_y=False, mute=False, fade_in=0, fade_out=0,
                transition_in=0, transition_out=0, text="", font_size=48, color="#ffffff",
                text_background="#000000", text_box=False)

def uid():
    return uuid.uuid4().hex

def fps_value(project):
    return float(Fraction(str(project["fps"])))

def timecode(frame, fps="30"):
    """Non-drop SMPTE label; fractional rates use nominal frame numbering."""
    n = round(float(Fraction(str(fps))))
    h, rem = divmod(int(frame), n * 3600)
    m, rem = divmod(rem, n * 60)
    s, f = divmod(rem, n)
    return f"{h:02}:{m:02}:{s:02}:{f:02}"

def parse_timecode(value, fps="30"):
    n = round(float(Fraction(str(fps))))
    parts = [int(x) for x in str(value).split(":")]
    if len(parts) != 4 or any(x < 0 for x in parts) or parts[1] >= 60 or parts[2] >= 60 or parts[3] >= n:
        raise ValueError("Use non-drop HH:MM:SS:FF timecode")
    h, m, s, f = parts
    return ((h * 60 + m) * 60 + s) * n + f

def new_project(name="Untitled", fps="30"):
    return dict(schema=SCHEMA, id=uid(), name=name, fps=str(fps), revision=0,
                canvas=dict(width=1080, height=1920, background="#000000"), assets={},
                tracks=[dict(id="V1", name="Video 1", kind="video", locked=False, mute=False, solo=False),
                        dict(id="A1", name="Audio 1", kind="audio", locked=False, mute=False, solo=False),
                        dict(id="T1", name="Text 1", kind="text", locked=False, mute=False, solo=False)],
                clips=[], magnetic=True, snapping=True, markers=[], provenance={})

def duration(project):
    return max([c["start"] + c["duration"] for c in project["clips"]] + [1])

def clip(project, ident):
    return next((c for c in project["clips"] if c["id"] == ident), None) or _missing("Clip")

def track(project, ident):
    return next((t for t in project["tracks"] if t["id"] == ident), None) or _missing("Track")

def _missing(what):
    raise ValueError(f"{what} not found")

def value_at(c, field, frame):
    keys = c.get("keyframes", {}).get(field, [])
    if not keys:
        return c["properties"].get(field, DEFAULTS[field])
    if frame <= keys[0]["frame"]:
        return keys[0]["value"]
    for a, b in zip(keys, keys[1:]):
        if frame <= b["frame"]:
            if a.get("interpolation") == "hold":
                return a["value"]
            ratio = (frame - a["frame"]) / (b["frame"] - a["frame"])
            return a["value"] + (b["value"] - a["value"]) * ratio
    return keys[-1]["value"]

def rebase_keys(c, offset, length):
    for field, keys in list(c.get("keyframes", {}).items()):
        start, end = value_at(c, field, offset), value_at(c, field, offset + length)
        mode = next((k.get("interpolation", "linear") for k in reversed(keys) if k["frame"] <= offset), "linear")
        c["keyframes"][field] = ([dict(frame=0, value=start, interpolation=mode)] +
            [dict(k, frame=k["frame"] - offset) for k in keys if offset < k["frame"] < offset + length] +
            [dict(frame=length, value=end, interpolation="linear")])

def make_clip(project, track_id, asset_id=None, start=0, source_in=0, source_out=None, text=""):
    t = track(project, track_id)
    if asset_id:
        asset = project["assets"][asset_id]
        out = source_out if source_out is not None else asset["duration_frames"]
        length = out - source_in
    else:
        out, length = source_out or round(fps_value(project) * 3), source_out or round(fps_value(project) * 3)
    return dict(id=uid(), track=track_id, kind=t["kind"], asset=asset_id, start=int(start),
                duration=int(length), source_in=int(source_in), source_out=int(out), speed=1., reverse=False,
                freeze=False, link=None, properties=dict(deepcopy(DEFAULTS), text=text), keyframes={})

def finite(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"Invalid {label}: expected {low} to {high}")

def validate(project):
    if project.get("schema") != SCHEMA:
        raise ValueError("Unsupported timeline schema")
    if project["fps"] not in {"24", "25", "30", "50", "60", "24000/1001", "30000/1001", "60000/1001"}:
        raise ValueError("Unsupported frame rate")
    for dimension in ("width", "height"):
        v = project["canvas"][dimension]
        if type(v) is not int or not 64 <= v <= 4096 or v % 2:
            raise ValueError("Canvas dimensions must be even, 64–4096")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", project["canvas"]["background"]):
        raise ValueError("Invalid canvas color")
    if len(project["clips"]) > 200 or len(project["tracks"]) > 32 or duration(project) > fps_value(project) * 600:
        raise ValueError("Timeline limit: 200 clips, 32 tracks, 10 minutes")
    tids, cids = set(), set()
    for t in project["tracks"]:
        if t["id"] in tids or t["kind"] not in KINDS:
            raise ValueError("Invalid or duplicate track")
        tids.add(t["id"])
    for c in project["clips"]:
        if c["id"] in cids or c["track"] not in tids or c["kind"] != track(project, c["track"])["kind"]:
            raise ValueError("Invalid clip identity or track")
        cids.add(c["id"])
        for field in ("start", "duration", "source_in", "source_out"):
            if type(c[field]) is not int or c[field] < (1 if field == "duration" else 0):
                raise ValueError(f"Invalid frame field: {field}")
        finite(c["speed"], .25, 4, "speed")
        if c["kind"] != "text":
            a = project["assets"].get(c["asset"])
            if not a or (c["kind"] == "audio" and not a.get("has_audio")) or (c["kind"] in {"image", "video"} and a["kind"] not in {"image", "video"}):
                raise ValueError("Incompatible or missing media")
            if c["kind"] != "image" and not (0 <= c["source_in"] < c["source_out"] <= a["duration_frames"]):
                raise ValueError("Source handles exhausted")
            if not c["freeze"] and c["kind"] != "image" and abs((c["source_out"] - c["source_in"]) / c["speed"] - c["duration"]) > 1:
                raise ValueError("Source duration and speed disagree")
        p = c["properties"]
        limits = dict(x=(-8192,8192), y=(-8192,8192), scale=(.05,8), rotation=(-3600,3600),
                      opacity=(0,1), volume=(0,4), pan=(-1,1), brightness=(-1,1), contrast=(0,3), saturation=(0,3), font_size=(8,300))
        for field, bounds in limits.items():
            finite(p[field], *bounds, field)
        if p["fit"] not in {"fit", "fill", "stretch"} or len(p["text"]) > 10000:
            raise ValueError("Invalid fit or text")
        for field in ("color", "text_background"):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", p[field]):
                raise ValueError("Invalid text color")
        crop = p["crop"]
        if len(crop) != 4:
            raise ValueError("Crop needs left/top/right/bottom fractions")
        for v in crop:
            finite(v, 0, .95, "crop")
        if crop[0] + crop[2] >= .99 or crop[1] + crop[3] >= .99:
            raise ValueError("Crop removes the image")
        for field in ("fade_in", "fade_out", "transition_in", "transition_out"):
            if type(p[field]) is not int or not 0 <= p[field] <= c["duration"]:
                raise ValueError("Fade/transition exceeds clip duration")
        for field, keys in c.get("keyframes", {}).items():
            if field not in ANIMATED or len(keys) > 200:
                raise ValueError("Unsupported keyframe property")
            previous = -1
            for k in keys:
                if type(k["frame"]) is not int or not previous < k["frame"] <= c["duration"] or k.get("interpolation", "linear") not in {"linear", "hold"}:
                    raise ValueError("Keyframes must be ordered, unique and inside clip")
                finite(k["value"], *limits[field], field)
                previous = k["frame"]
    return project

def _editable(project, c):
    if track(project, c["track"]).get("locked"):
        raise ValueError("Track is locked")

def _shift(project, boundary, delta, exclude=()):
    for c in project["clips"]:
        if c["id"] not in exclude and c["start"] >= boundary:
            _editable(project, c)
            c["start"] += delta

def _trim(c, edge, delta):
    old = c["duration"]
    source_delta = round(delta * c["speed"])
    if edge == "left":
        c["start"] += delta
        c["duration"] -= delta
        if not c["freeze"] and c["kind"] not in {"text", "image"}:
            c["source_out" if c["reverse"] else "source_in"] += -source_delta if c["reverse"] else source_delta
        rebase_keys(c, delta, c["duration"])
    elif edge == "right":
        c["duration"] += delta
        if not c["freeze"] and c["kind"] not in {"text", "image"}:
            c["source_in" if c["reverse"] else "source_out"] += -source_delta if c["reverse"] else source_delta
        rebase_keys(c, 0, c["duration"])
    else:
        raise ValueError("Trim edge must be left or right")
    for f in ("fade_in", "fade_out", "transition_in", "transition_out"):
        c["properties"][f] = min(c["properties"][f], max(0,c["duration"]))

def _trim_with_links(project,c,edge,delta):
    for x in project["clips"]:
        if x["id"]==c["id"] or (c.get("link") and x.get("link")==c["link"]):
            _editable(project,x); _trim(x,edge,delta)

def apply(project, command):
    """Return a new state; failed commands leave the caller's state untouched."""
    op=command["op"]
    if op=="join" and command.get("linked",True):
        left=clip(project,command["id"]); right=clip(project,command["other"])
        if left.get("link") or right.get("link"):
            a=[x for x in project["clips"] if x.get("link") and x["link"]==left.get("link")]
            b=[x for x in project["clips"] if x.get("link") and x["link"]==right.get("link")]
            if {x["track"] for x in a}!={x["track"] for x in b} or len(a)!=len(b):
                raise ValueError("Join requires matching linked groups; unlink first for an independent join")
            result=deepcopy(project)
            for x in a+b:
                _editable(result,x)
                clip(result,x["id"])["link"]=left["link"]
            for x in a:
                other=next(y for y in b if y["track"]==x["track"])
                result=apply(result,dict(op="join",id=x["id"],other=other["id"],linked=False))
            return validate(result)
    original=clip(project,command["id"]) if command.get("id") and op in {"trim","ripple_trim","split","delete","reverse","speed","slip","duplicate"} else None
    linked=[x for x in project["clips"] if original and original.get("link") and x.get("link")==original["link"] and x["id"]!=original["id"]]
    if linked and command.get("linked",True):
        # Linked audio/video are one atomic edit, with a single ripple shift.
        for x in linked:
            _editable(project,x)
        result=apply(project,dict(command,linked=False))
        right_link=uid()
        if op in {"split","duplicate"}:
            added=next(c for c in result["clips"] if c["id"] not in {x["id"] for x in project["clips"]})
            added["link"]=right_link
        for x in linked:
            mirror=dict(command,id=x["id"],linked=False,ripple=False)
            if op=="ripple_trim":
                mirror["op"]="trim"
            magnetic=result["magnetic"]
            if op in {"speed","duplicate"}:
                result["magnetic"]=False
            before={c["id"] for c in result["clips"]}
            result=apply(result,mirror)
            result["magnetic"]=magnetic
            if op=="ripple_trim" and command["edge"]=="left":
                clip(result,x["id"])["start"]-=command["delta"]
            if op in {"split","duplicate"}:
                added=next(c for c in result["clips"] if c["id"] not in before)
                added["link"]=right_link
        return validate(result)
    p = deepcopy(project)
    c = clip(p, command["id"]) if "id" in command and op not in {"track", "remove_track", "reorder_track"} else None
    if c:
        _editable(p, c)
    delta = command.get("delta", 0)
    if type(delta) is not int:
        raise ValueError("Delta must be integer frames")
    if op == "insert":
        t = track(p, command["track"])
        if t.get("locked"):
            raise ValueError("Track is locked")
        c = make_clip(p, t["id"], command.get("asset"), command.get("start", 0), command.get("source_in", 0), command.get("source_out"), command.get("text", ""))
        mode=command.get("mode")
        if mode not in {None,"ripple","overlay","overwrite"}:
            raise ValueError("Invalid insertion mode")
        if mode=="overwrite":
            start,end=c["start"],c["start"]+c["duration"]
            ids=[x["id"] for x in p["clips"] if x["track"]==c["track"] and x["start"]<end and x["start"]+x["duration"]>start]
            for ident in ids:
                x=clip(p,ident); a,b=x["start"],x["start"]+x["duration"]
                if a>=start and b<=end:
                    p=apply(p,dict(op="delete",id=ident,ripple=False))
                elif a<start and b>end:
                    before={z["id"] for z in p["clips"]}
                    p=apply(p,dict(op="split",id=ident,frame=start))
                    right=next(z for z in p["clips"] if z["id"] not in before and z["track"]==c["track"])
                    p=apply(p,dict(op="trim",id=right["id"],edge="left",delta=end-start))
                elif a<start:
                    p=apply(p,dict(op="trim",id=ident,edge="right",delta=start-b))
                else:
                    p=apply(p,dict(op="trim",id=ident,edge="left",delta=end-a))
        elif mode=="ripple" or (mode is None and command.get("ripple",p["magnetic"] and t["kind"]=="video")):
            _shift(p, c["start"], c["duration"])
        p["clips"].append(c)
    elif op == "move":
        dest = command.get("track", c["track"])
        if track(p, dest).get("locked") or track(p, dest)["kind"] != c["kind"]:
            raise ValueError("Incompatible or locked destination")
        shift = command["start"] - c["start"]
        linked = [x for x in p["clips"] if x["id"] == c["id"] or (command.get("linked", True) and c.get("link") and x.get("link") == c["link"])]
        if p["magnetic"] and command.get("magnetic", True):
            _shift(p, c["start"] + c["duration"], -c["duration"], [x["id"] for x in linked])
            target = command["start"]
            _shift(p, target, c["duration"], [x["id"] for x in linked])
        for x in linked:
            _editable(p, x)
            x["start"] += shift
        c["track"] = dest
    elif op in {"trim", "ripple_trim"}:
        old_end = c["start"] + c["duration"]
        edge = command["edge"]
        _trim(c, edge, delta)
        if op == "ripple_trim":
            if edge == "left":
                c["start"] -= delta
            _shift(p, old_end, delta if edge == "right" else -delta, [c["id"]])
    elif op == "slip":
        c["source_in"] += delta
        c["source_out"] += delta
    elif op in {"roll", "slide"}:
        neighbors = sorted([x for x in p["clips"] if x["track"] == c["track"]], key=lambda x:x["start"])
        i = neighbors.index(c)
        if op == "roll":
            if i + 1 >= len(neighbors):
                raise ValueError("Roll needs an adjacent right clip")
            b = neighbors[i+1]
            _editable(p, b)
            if c["start"] + c["duration"] != b["start"]:
                raise ValueError("Roll requires a shared cut")
            _trim_with_links(p,c,"right",delta)
            _trim_with_links(p,b,"left",delta)
        else:
            if not 0 < i < len(neighbors)-1:
                raise ValueError("Slide needs two adjacent neighbors")
            a, b = neighbors[i-1], neighbors[i+1]
            _editable(p,a); _editable(p,b)
            if a["start"]+a["duration"] != c["start"] or c["start"]+c["duration"] != b["start"]:
                raise ValueError("Slide requires adjacent cuts")
            _trim_with_links(p,a,"right",delta); _trim_with_links(p,b,"left",delta)
            for x in p["clips"]:
                if x["id"]==c["id"] or (c.get("link") and x.get("link")==c["link"]):
                    _editable(p,x); x["start"] += delta
    elif op == "split":
        offset = command["frame"] - c["start"]
        if not 0 < offset < c["duration"]:
            raise ValueError("Split must be inside clip")
        b = deepcopy(c)
        b["id"] = uid(); b["start"] += offset; b["duration"] -= offset
        if c["kind"] not in {"text", "image"} and not c["freeze"]:
            cut = c["source_out"] - round(offset*c["speed"]) if c["reverse"] else c["source_in"] + round(offset*c["speed"])
            b["source_out" if c["reverse"] else "source_in"] = cut
            c["source_in" if c["reverse"] else "source_out"] = cut
        rebase_keys(b, offset, b["duration"])
        rebase_keys(c, 0, offset); c["duration"] = offset
        for x in (c,b):
            for f in ("fade_in","fade_out","transition_in","transition_out"):
                x["properties"][f] = min(x["properties"][f],x["duration"])
        p["clips"].append(b)
    elif op == "join":
        b = clip(p,command["other"]); _editable(p,b)
        if b["start"] < c["start"]:
            c,b = b,c
        fields = ("track","asset","kind","speed","reverse","freeze","properties","link")
        contiguous = c["source_in"] == b["source_out"] if c["reverse"] else c["source_out"] == b["source_in"]
        if c["start"]+c["duration"] != b["start"] or any(c[f] != b[f] for f in fields) or (c["kind"] not in {"text","image"} and not c["freeze"] and not contiguous):
            raise ValueError("Join requires matching clips with contiguous source handles")
        seam=c["duration"]
        merged={}
        for field in set(c["keyframes"]) | set(b["keyframes"]):
            if abs(value_at(c,field,seam)-value_at(b,field,0))>1e-7:
                raise ValueError("Keyframe values disagree at the join; align the boundary values first")
            left=deepcopy(c["keyframes"].get(field,[])) or [dict(frame=0,value=value_at(c,field,0),interpolation="hold")]
            right=deepcopy(b["keyframes"].get(field,[])) or [dict(frame=0,value=value_at(b,field,0),interpolation="hold")]
            left=[k for k in left if k["frame"]<seam]
            merged[field]=left+[dict(k,frame=k["frame"]+seam) for k in right]
        c["keyframes"]=merged
        c["duration"] += b["duration"]
        c["source_in"] = min(c["source_in"],b["source_in"]); c["source_out"] = max(c["source_out"],b["source_out"])
        p["clips"].remove(b)
    elif op == "transition":
        frames=command["frames"]
        if type(frames) is not int or frames<1:
            raise ValueError("Transition needs a positive integer frame length")
        b=next((x for x in p["clips"] if x["track"]==c["track"] and x["start"]==c["start"]+c["duration"]),None)
        if not b or c["kind"] not in {"video","image"}:
            raise ValueError("Choose a video/image clip with an adjacent right clip")
        _editable(p,b)
        if frames>=min(c["duration"],b["duration"]):
            raise ValueError("Transition exceeds available clips")
        if command.get("kind","dissolve")=="dissolve":
            # Incoming alpha over an opaque outgoing layer gives a true cross dissolve.
            _shift(p,b["start"],-frames,[c["id"]])
            c["properties"]["transition_out"]=0
            b["properties"]["transition_in"]=frames
        elif command["kind"]=="fade_black":
            c["properties"]["transition_out"]=frames
            b["properties"]["transition_in"]=frames
        else:
            raise ValueError("Unknown transition")
        c["properties"]["fade_out"]=frames
        b["properties"]["fade_in"]=frames
    elif op in {"delete","duplicate"}:
        if op == "delete":
            p["clips"].remove(c)
            if command.get("ripple",p["magnetic"]):
                _shift(p,c["start"]+c["duration"],-c["duration"])
        else:
            b = deepcopy(c); b["id"] = uid(); b["link"] = None
            b["start"] = command.get("start",c["start"]+c["duration"])
            if command.get("ripple",p["magnetic"]):
                _shift(p,b["start"],b["duration"])
            p["clips"].append(b)
    elif op == "replace_source":
        asset=p["assets"][command["asset"]]
        if asset["kind"]!=c["kind"] and not (c["kind"]=="audio" and asset["has_audio"]):
            raise ValueError("Replacement source must match the clip type")
        c.setdefault("provenance",{}).setdefault("replaced_asset_ids",[]).append(c["asset"])
        c["asset"]=asset["id"]
        if not any(p["assets"].get(x["asset"],{}).get("missing") for x in p["clips"]):
            p.pop("warnings",None)
    elif op == "speed":
        if c["freeze"] or c["kind"] in {"image","text"}:
            raise ValueError("Speed changes require moving video or audio; trim a still layer to change its duration")
        old_end = c["start"]+c["duration"]
        c["speed"] = command["value"]
        finite(c["speed"],.25,4,"speed")
        old_duration = c["duration"]
        c["duration"] = max(1,round((c["source_out"]-c["source_in"])/c["speed"]))
        for keys in c["keyframes"].values():
            for k in keys:
                k["frame"] = round(k["frame"]*c["duration"]/old_duration)
        for f in ("fade_in","fade_out","transition_in","transition_out"):
            c["properties"][f] = min(c["properties"][f],c["duration"])
        if p["magnetic"]:
            _shift(p,old_end,c["duration"]-old_duration,[c["id"]])
    elif op == "reverse":
        c["reverse"] = not c["reverse"]
    elif op == "freeze":
        if c["kind"]!="video":
            raise ValueError("Freeze requires a video clip")
        if not c["freeze"]:
            c["before_freeze"]={k:deepcopy(c[k]) for k in ("source_in","source_out","duration","speed","keyframes")}
        f = command.get("source_frame",c["source_in"])
        c["source_in"] = f; c["source_out"] = f+1; c["freeze"] = True
        c["duration"] = command.get("duration",c["duration"])
        rebase_keys(c,0,c["duration"])
        for field in ("fade_in","fade_out","transition_in","transition_out"):
            c["properties"][field]=min(c["properties"][field],c["duration"])
    elif op == "unfreeze":
        if not c["freeze"] or not c.get("before_freeze"):
            raise ValueError("No moving source state available; restore an earlier revision")
        c.update(c.pop("before_freeze")); c["freeze"]=False
    elif op == "properties":
        if set(command["values"]) - set(DEFAULTS):
            raise ValueError("Unknown clip property")
        c["properties"].update(deepcopy(command["values"]))
    elif op == "keyframe":
        field = command["field"]
        if field not in ANIMATED:
            raise ValueError("Unsupported keyframe property")
        keys = [k for k in c["keyframes"].get(field,[]) if k["frame"] != command["frame"]]
        if not command.get("delete"):
            keys.append(dict(frame=command["frame"],value=command["value"],interpolation=command.get("interpolation","linear")))
        c["keyframes"][field] = sorted(keys,key=lambda k:k["frame"])
    elif op == "detach":
        a = p["assets"].get(c["asset"],{})
        if c["kind"] != "video" or not a.get("has_audio"):
            raise ValueError("Clip has no audio to detach")
        dest = command.get("track","A1")
        if track(p,dest)["kind"] != "audio" or track(p,dest).get("locked"):
            raise ValueError("Choose an unlocked audio track")
        b = deepcopy(c); b["id"] = uid(); b["track"] = dest; b["kind"] = "audio"
        c["link"] = b["link"] = uid(); b["properties"]["mute"] = False; c["properties"]["mute"] = True
        p["clips"].append(b)
    elif op == "unlink":
        link = c["link"]
        for x in p["clips"]:
            if x["id"] == c["id"] or (link and x["link"] == link):
                _editable(p,x); x["link"] = None
    elif op == "replace_audio":
        a = p["assets"][command["asset"]]
        b = make_clip(p,command.get("track","A1"),command["asset"],c["start"],command.get("source_in",0),command.get("source_in",0)+c["duration"])
        if track(p,b["track"]).get("locked") or not a.get("has_audio"):
            raise ValueError("Audio source or track unavailable")
        c["properties"]["mute"] = True
        c["link"] = b["link"] = uid(); p["clips"].append(b)
    elif op == "add_track":
        p["tracks"].append(dict(id=uid(),name=command.get("name",command["kind"].title()),kind=command["kind"],locked=False,mute=False,solo=False))
    elif op == "track":
        t = track(p,command["id"])
        if set(command["values"]) - {"name","locked","mute","solo","ducking","hidden"}:
            raise ValueError("Unknown track property")
        t.update(command["values"])
    elif op == "reorder_track":
        t = track(p,command["id"])
        index = command["index"]
        if type(index) is not int or not 0 <= index < len(p["tracks"]):
            raise ValueError("Invalid track order")
        p["tracks"].remove(t); p["tracks"].insert(index,t)
    elif op == "remove_track":
        t = track(p,command["id"])
        if t.get("locked") or any(c["track"] == t["id"] for c in p["clips"]):
            raise ValueError("Only empty unlocked tracks can be removed")
        p["tracks"].remove(t)
    elif op == "fps":
        old = fps_value(p)
        p["fps"] = str(command["value"])
        ratio = fps_value(p)/old
        for a in p["assets"].values():
            a["duration_frames"] = max(1,round(a["duration_frames"]*ratio))
        for c in p["clips"]:
            for f in ("start","duration","source_in","source_out"):
                c[f] = round(c[f]*ratio)
            for keys in c["keyframes"].values():
                for k in keys:
                    k["frame"] = round(k["frame"]*ratio)
            for f in ("fade_in","fade_out","transition_in","transition_out"):
                c["properties"][f] = round(c["properties"][f]*ratio)
        for m in p["markers"]:
            m["frame"] = round(m["frame"]*ratio)
    elif op == "canvas":
        p["canvas"].update(command["values"])
    elif op == "mode":
        p["magnetic"] = bool(command.get("magnetic",p["magnetic"]))
        p["snapping"] = bool(command.get("snapping",p["snapping"]))
    elif op == "close_gaps":
        items = sorted([x for x in p["clips"] if x["track"] == command["track"]],key=lambda x:x["start"])
        end = 0
        removed = 0
        for x in items:
            original_start=x["start"]
            removed+=max(0,original_start-end)
            shift=-removed
            for linked in p["clips"]:
                if linked["id"]==x["id"] or (x.get("link") and linked.get("link")==x["link"]):
                    _editable(p,linked); linked["start"]+=shift
            end=max(end,original_start+x["duration"])
    elif op == "marker":
        p["markers"].append(dict(frame=command["frame"],label=command.get("label","Marker")))
    else:
        raise ValueError(f"Unsupported edit: {op}")
    return validate(p)

def snap(project, frame, threshold=6, exclude=None, playhead=None):
    if not project["snapping"]:
        return max(0,frame)
    points = [0] + [m["frame"] for m in project["markers"]]
    if playhead is not None:
        points.append(playhead)
    for c in project["clips"]:
        if c["id"] != exclude:
            points.extend([c["start"],c["start"]+c["duration"]])
    closest = min(points,key=lambda x:abs(x-frame))
    return max(0,closest if abs(closest-frame) <= threshold else frame)
