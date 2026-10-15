"""Append-only revisions, confined media registry, imports, and legacy recovery."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from copy import deepcopy
from pathlib import Path

from ffmpeg_bin import FFMPEG
from .storage import atomic_write_json, read_json
from .timeline import apply, fps_value, make_clip, new_project, uid, validate
from .timeline_render import safe_asset, run_ffmpeg, render

class Conflict(ValueError):
    pass

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()

class TimelineStore:
    def __init__(self, workspace, name="Untitled"):
        self.workspace = workspace
        self.root = workspace.root.resolve()
        self.key = hashlib.sha256(name.encode()).hexdigest()[:24]
        self.directory = self.root / "nextplay" / self.key
        # Check symlinked ancestors before creating any storage directories.
        self.directory.resolve().relative_to(self.root)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.directory.resolve().relative_to(self.root)
        self.name = name

    def history(self):
        result=[]
        for path in sorted(self.directory.glob("revision-*.json")):
            try:
                record=read_json(path)
                if record["checksum"] != digest(record["project"]):
                    continue
                result.append(dict(revision=record["project"]["revision"],label=record["label"],checksum=record["checksum"]))
            except (ValueError,KeyError,TypeError):
                continue
        return result

    def revision(self, number):
        if type(number) is not int or number < 1:
            raise ValueError("Invalid revision")
        path=self.directory/f"revision-{number:08d}.json"
        path.resolve().relative_to(self.root)
        record=read_json(path)
        if not record or record["checksum"] != digest(record["project"]):
            raise ValueError("Revision is missing or corrupt")
        return validate(record["project"])

    def load(self):
        history=self.history()
        if history:
            return self.revision(history[-1]["revision"])
        return self.commit(self.migrate(),0,"Imported original timeline")

    def commit(self, project, expected, label):
        history=self.history()
        latest=history[-1]["revision"] if history else 0
        if expected != latest:
            raise Conflict("Draft changed in another tab; reload before editing")
        p=validate(deepcopy(project))
        # Never reuse an occupied revision, including a damaged one.
        existing=[int(x.stem.split("-")[-1]) for x in self.directory.glob("revision-*.json")]
        p["revision"]=max(existing+[0])+1
        record=dict(project=p,label=str(label)[:240],checksum=digest(p))
        target=self.directory/f"revision-{p['revision']:08d}.json"
        # Exclusive create prevents concurrent writers replacing immutable history.
        with target.open("x",encoding="utf-8") as fh:
            json.dump(record,fh,ensure_ascii=False,indent=2)
            fh.flush()
            import os
            os.fsync(fh.fileno())
        try:
            atomic_write_json(self.directory/"head.json",dict(revision=p["revision"]))
        except OSError:
            pass  # The durable revision is authoritative; head is a disposable index.
        return p

    def asset(self,path,fps,metadata=None):
        path=Path(path).resolve()
        relative=path.relative_to(self.root).as_posix()
        kind="image" if path.suffix.lower() in {".jpg",".jpeg",".png",".webp"} else "audio" if path.suffix.lower() in {".wav",".mp3",".m4a",".aac",".ogg"} else "video"
        info=subprocess.run([FFMPEG,"-hide_banner","-i",str(path)],capture_output=True,text=True).stderr
        match=re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",info)
        seconds=int(match[1])*3600+int(match[2])*60+float(match[3]) if match else 3 if kind=="image" else 0
        if seconds <= 0 or (kind=="video" and "Video:" not in info) or (kind=="audio" and "Audio:" not in info):
            raise ValueError("Media could not be decoded")
        with path.open("rb") as fh:
            sha=hashlib.file_digest(fh,"sha256").hexdigest()
        a=dict(id=hashlib.sha256((relative+sha).encode()).hexdigest()[:24],path=relative,name=path.name,kind=kind,duration_frames=max(1,round(seconds*fps)),has_audio="Audio:" in info,
               sha256=sha,provenance=deepcopy(metadata or {}))
        a["provenance"].setdefault("rights_status","unknown")
        safe_asset(self.root,a)
        return a

    def migrate(self):
        from .ad_editor import load_ad_plans, _analysis_index, _candidate_index
        from .rights import load_rights_state, rights_status
        p=new_project(self.name)
        videos,segments=_analysis_index(self.workspace)
        candidates=_candidate_index(self.workspace)
        rights=load_rights_state(self.workspace)
        sources={**candidates,**videos}
        source_assets={}
        for ident,source in sources.items():
            try:
                a=self.asset(source["video_path"],fps_value(p),dict(video_id=ident,creator=source.get("creator",""),source_url=source.get("tiktok_url",""),rights_status=rights_status(rights,ident)))
                p["assets"][a["id"]]=a; source_assets[ident]=a["id"]
            except (ValueError,OSError):
                continue
        parent=next((x for x in load_ad_plans(self.workspace) if x.get("name")==self.name),None)
        p["provenance"]={"legacy_ad":self.name if parent else None,"legacy_plan":deepcopy(parent) if parent else None}
        frame=0
        for s in (parent or {}).get("segments",[]):
            ident=str(s.get("video_id",""))
            aid=source_assets.get(ident)
            if not aid:
                # Preserve unresolved references in migration warnings; never silently render wrong footage.
                p.setdefault("warnings",[]).append(f"Missing or outside-workspace media: {ident}")
                # Keep an offline placeholder with the original timing and provenance.
                aid="offline-"+hashlib.sha256(ident.encode()).hexdigest()[:20]
                p["assets"][aid]=dict(id=aid,path="offline/"+aid+".mp4",name="Offline: "+ident,kind="video",missing=True,
                    duration_frames=max(1,round(float(s["end"])*fps_value(p))),has_audio=False,
                    provenance=dict(video_id=ident,rights_status=rights_status(rights,ident)))
            frame+=round(float(s.get("gap_before",0))*fps_value(p))
            c=make_clip(p,"V1",aid,frame,round(float(s["start"])*fps_value(p)),round(float(s["end"])*fps_value(p)))
            c["provenance"]=deepcopy(s)
            if s.get("audio_mode")=="mute":
                c["properties"]["mute"]=True
            p["clips"].append(c)
            # Legacy replacement audio is reconstructed as its own linked track clip.
            if s.get("audio_mode")=="external" and s.get("audio_segment_id"):
                audio_source=segments.get(s["audio_segment_id"],{})
                audio_asset=source_assets.get(str(audio_source.get("video_id","")))
                if audio_asset:
                    c["properties"]["mute"]=True
                    b=make_clip(p,"A1",audio_asset,frame,round(float(s.get("audio_start",0))*fps_value(p)),round(float(s.get("audio_start",0))*fps_value(p))+c["duration"])
                    c["link"]=b["link"]=uid(); p["clips"].append(b)
            frame+=c["duration"]
            if s.get("text"):
                caption=make_clip(p,"T1",start=c["start"],source_out=c["duration"],text=s["text"])
                caption["properties"].update(y=700,font_size=52,text_box=True)
                caption["provenance"]={"caption_source":s.get("segment_id")}
                p["clips"].append(caption)
        return validate(p)

    def edit(self,commands,expected,label="Edit"):
        p=self.load()
        if p["revision"]!=expected:
            raise Conflict("Draft changed in another tab; reload before editing")
        if not isinstance(commands,list) or len(commands)>100:
            raise ValueError("Expected at most 100 edit commands")
        for command in commands:
            p=apply(p,command)
        return self.commit(p,expected,label)

    def restore(self,number,expected):
        current=self.load(); previous=self.revision(number)
        # Keep newer imported assets available when recovering old timeline layouts.
        for ident,asset in current["assets"].items():
            if ident not in previous["assets"]:
                a=deepcopy(asset)
                a["duration_frames"]=max(1,round(a["duration_frames"]*fps_value(previous)/fps_value(current)))
                previous["assets"][ident]=a
        return self.commit(previous,expected,f"Restored revision {number}")

    def import_media(self,data,filename,expected,metadata=None):
        if len(data)>512*1024*1024 or not data:
            raise ValueError("Import limit is 512 MB per file")
        name=Path(filename).name
        ext=Path(name).suffix.lower()
        if ext not in {".mp4",".mov",".m4v",".webm",".mp3",".wav",".m4a",".aac",".ogg",".png",".jpg",".jpeg",".webp"}:
            raise ValueError("Unsupported import format")
        p=self.load()
        if expected!=p["revision"]:
            raise Conflict("Reload before importing")
        folder=self.directory/"imports"; folder.mkdir(exist_ok=True); folder.resolve().relative_to(self.root)
        target=folder/(uid()+ext)
        try:
            with target.open("xb") as fh:
                fh.write(data)
            a=self.asset(target,fps_value(p),metadata)
            a["name"]=name
            p["assets"][a["id"]]=a
            p=self.commit(p,expected,f"Imported {name}")
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return p,a

    def visualizations(self,p,ident):
        a=p["assets"][ident]; source=safe_asset(self.root,a)
        folder=self.directory/"cache"; folder.mkdir(exist_ok=True); folder.resolve().relative_to(self.root)
        thumbnail=folder/(a["id"]+"-film.jpg"); wave=folder/(a["id"]+"-wave.png")
        if a["kind"]!="audio" and not thumbnail.exists():
            if a["kind"]=="image":
                run_ffmpeg(["-i",str(source),"-vf","scale=160:90:force_original_aspect_ratio=decrease,pad=160:90:(ow-iw)/2:(oh-ih)/2","-frames:v","1","-y",str(thumbnail)],60)
            else:
                spacing=max(.05,a["duration_frames"]/fps_value(p)/8)
                run_ffmpeg(["-i",str(source),"-vf",f"fps=1/{spacing},scale=160:90:force_original_aspect_ratio=decrease,pad=160:90:(ow-iw)/2:(oh-ih)/2,tile=8x1","-frames:v","1","-y",str(thumbnail)],60)
        if a["has_audio"] and not wave.exists():
            run_ffmpeg(["-i",str(source),"-filter_complex","aformat=channel_layouts=mono,showwavespic=s=800x80:colors=0x61d9bf","-frames:v","1","-y",str(wave)],60)
        return {k:"/file/"+v.relative_to(self.root).as_posix() for k,v in (("thumbnail",thumbnail),("waveform",wave)) if v.exists()}

    def export(self,p,preset="custom",draft=False):
        # Render the supplied current timeline only if it equals the authoritative saved revision.
        current=self.load()
        if digest(p)!=digest(current):
            raise Conflict("Save current edits before rendering")
        folder=self.directory/("drafts" if draft else "renders"); folder.mkdir(exist_ok=True); folder.resolve().relative_to(self.root)
        target=folder/f"r{p['revision']:08d}-{uid()}.mp4"
        render(p,self.root,target,preset,draft)
        used=sorted({c["asset"] for c in p["clips"] if c["asset"]})
        manifest=dict(revision=p["revision"],checksum=digest(p),preset=preset,draft=draft,assets=deepcopy(p["assets"]),used_asset_ids=used,
                      rights_cleared=all(p["assets"][a]["provenance"].get("rights_status") in {"owned","cleared"} for a in used),provenance=p["provenance"])
        atomic_write_json(target.with_suffix(".json"),manifest)
        return dict(url="/file/"+target.relative_to(self.root).as_posix(),manifest="/file/"+target.with_suffix(".json").relative_to(self.root).as_posix(),revision=p["revision"])

def edit_direction(project,instruction,selection,playhead):
    """Offline command interpreter. Unrecognized directions fail explicitly."""
    instruction=instruction.strip().lower()
    selected=next((c for c in project["clips"] if c["id"]==selection),None)
    if instruction in {"close gaps","close gaps on selected track"}:
        return [dict(op="close_gaps",track=selected["track"] if selected else "V1")]
    if instruction in {"split","split at playhead"}:
        return [dict(op="split",id=selection,frame=playhead)]
    if instruction in {"reverse","reverse selected clip"}:
        return [dict(op="reverse",id=selection)]
    if instruction in {"mute","mute selected clip"}:
        return [dict(op="properties",id=selection,values=dict(mute=True))]
    if instruction in {"ripple delete","delete selected clip"}:
        return [dict(op="delete",id=selection,ripple=True)]
    match=re.fullmatch(r"(?:set )?speed (?:to )?([.\d]+)x?",instruction)
    if match:
        return [dict(op="speed",id=selection,value=float(match[1]))]
    match=re.fullmatch(r"(?:move|trim left|trim right|slip|slide|roll) ([-+]?\d+) frames?",instruction)
    if match and selected:
        delta=int(match[1]); verb=instruction.split(" "+match[1])[0]
        if verb=="move":
            return [dict(op="move",id=selection,start=selected["start"]+delta)]
        if verb.startswith("trim"):
            return [dict(op="trim",id=selection,edge=verb.split()[-1],delta=delta)]
        return [dict(op=verb,id=selection,delta=delta)]
    raise ValueError("Direction not recognized. Supported: split at playhead, reverse, mute, close gaps, ripple delete, speed 1.25x, move/trim left/trim right/slip/slide/roll +10 frames. Structured AI command batches use the same validated edit endpoint.")
