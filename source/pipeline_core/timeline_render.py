"""FFmpeg compositor for the same frame model used by every editing operation."""
from __future__ import annotations

import json
import math
import os
import re
from functools import lru_cache
import subprocess
import tempfile
import textwrap
from pathlib import Path

from ffmpeg_bin import FFMPEG
from .timeline import duration, fps_value, validate

PRESETS = {"portrait": (1080,1920), "square": (1080,1080), "landscape": (1920,1080),
           "draft": (360,640), "custom": None}

@lru_cache(maxsize=1)
def graph_option():
    info=subprocess.run([FFMPEG,"-version"],capture_output=True,text=True).stdout
    match=re.search(r"ffmpeg version (\d+)",info)
    return "-/filter_complex" if match and int(match[1])>=8 else "-filter_complex_script"

def run_ffmpeg(args, timeout=600):
    result = subprocess.run([FFMPEG,"-hide_banner","-loglevel","error",*args],capture_output=True,text=True,timeout=timeout)
    if result.returncode:
        raise RuntimeError("FFmpeg failed: " + result.stderr[-6000:])

def safe_asset(root, asset):
    path = (Path(root) / asset["path"]).resolve()
    path.relative_to(Path(root).resolve())
    if not path.is_file() or path.suffix.lower() not in {".mp4",".mov",".m4v",".webm",".mp3",".wav",".m4a",".aac",".ogg",".png",".jpg",".jpeg",".webp"}:
        raise ValueError("Missing or unsupported project media")
    return path

def expr(c, field, fps, variable="t"):
    keys = c["keyframes"].get(field,[])
    if not keys:
        return str(c["properties"][field])
    result = str(keys[-1]["value"])
    for a,b in reversed(list(zip(keys,keys[1:]))):
        ta,tb = a["frame"]/fps,b["frame"]/fps
        v = str(a["value"]) if a.get("interpolation") == "hold" else f"({a['value']}+({b['value']}-{a['value']})*({variable}-{ta})/{tb-ta})"
        result = f"if(lt({variable},{tb}),{v},{result})"
    return f"if(lt({variable},{keys[0]['frame']/fps}),{keys[0]['value']},{result})"

def text_image(c, target, width, height):
    from PIL import Image, ImageDraw, ImageFont
    p = c["properties"]
    image = Image.new("RGBA",(width,height),(0,0,0,0))
    font_candidates = [Path(__file__).parent / "static" / "font.ttf",Path("/System/Library/Fonts/Supplemental/Arial.ttf"),Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    font_path = next((x for x in font_candidates if x.exists()),None)
    font = ImageFont.truetype(str(font_path),int(p["font_size"])) if font_path else ImageFont.load_default(size=int(p["font_size"]))
    draw = ImageDraw.Draw(image)
    # Pixel-measured wrapping, preserving explicit line breaks and Unicode.
    lines = []
    for paragraph in p["text"].split("\n"):
        line = ""
        for word in paragraph.split(" "):
            candidate = (line + " " + word).strip()
            if draw.textlength(candidate,font=font) > width*.9 and line:
                lines.append(line); line = word
            else:
                line = candidate
        lines.append(line)
    text = "\n".join(lines)
    bbox = draw.multiline_textbbox((0,0),text,font=font,spacing=8,align="center")
    x,y = (width-(bbox[2]-bbox[0]))/2,(height-(bbox[3]-bbox[1]))/2-bbox[1]
    if p["text_box"]:
        draw.rounded_rectangle((x-16,y+bbox[1]-12,x+bbox[2]+16,y+bbox[3]+12),radius=12,fill=p["text_background"])
    draw.multiline_text((x,y),text,font=font,fill=p["color"],spacing=8,align="center",stroke_width=1,stroke_fill=p["text_background"])
    image.save(target)

def render(project, root, output, preset="custom", draft=False):
    validate(project)
    if preset not in PRESETS:
        raise ValueError("Unknown export preset")
    canvas = project["canvas"]
    width,height = PRESETS[preset] or (canvas["width"],canvas["height"])
    if draft:
        ratio = min(1.,640/max(width,height))
        width,height = int(width*ratio)//2*2,int(height*ratio)//2*2
    sx,sy = width/canvas["width"],height/canvas["height"]
    fps = fps_value(project); seconds = duration(project)/fps
    output = Path(output)
    if output.exists():
        raise ValueError("Rendering never overwrites an existing output")
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nextplay-render-") as temp:
        temp = Path(temp)
        args = ["-filter_complex_threads","1","-f","lavfi","-i",f"color=c={canvas['background']}:s={width}x{height}:r={project['fps']}:d={seconds}"]
        filters = ["[0:v]format=rgba[base]"]
        index = 1; visuals = []; audio = []
        tracks = {t["id"]:t for t in project["tracks"]}
        audio_solo = any(t.get("solo") for t in project["tracks"] if t["kind"] in {"video","audio"})
        visual_solo = any(t.get("solo") for t in project["tracks"] if t["kind"] in {"text","image"})
        # Track order is back-to-front, within a track later clips cover earlier ones.
        ordered = sorted(project["clips"],key=lambda c:(list(tracks).index(c["track"]),c["start"],project["clips"].index(c)))
        foreground = [c for c in ordered if c["kind"] in {"audio","video"} and not tracks[c["track"]].get("ducking") and
                      not tracks[c["track"]].get("mute") and not c["properties"]["mute"] and not c["freeze"] and
                      project["assets"].get(c["asset"],{}).get("has_audio") and (not audio_solo or tracks[c["track"]].get("solo"))]
        for c in ordered:
            t = tracks[c["track"]]; p = c["properties"]
            start,length = c["start"]/fps,c["duration"]/fps
            asset = project["assets"].get(c["asset"],{})
            visible = c["kind"] != "audio" and not t.get("hidden") and (not visual_solo or t.get("solo"))
            audible = c["kind"] in {"video","audio"} and asset.get("has_audio") and not c["freeze"] and not p["mute"] and not t.get("mute") and (not audio_solo or t.get("solo"))
            if not visible and not audible:
                continue
            path = safe_asset(root,asset) if c["kind"] != "text" else temp / f"text-{c['id']}.png"
            if c["kind"] == "text":
                text_image(c,path,canvas["width"],canvas["height"])
            if c["kind"] in {"text","image"}:
                args += ["-loop","1","-framerate",project["fps"],"-i",str(path)]
            else:
                args += ["-i",str(path)]
            stream = index; index += 1
            if visible:
                source_start,source_end = c["source_in"]/fps,c["source_out"]/fps
                chain = []
                if c["kind"] not in {"text","image"}:
                    if c["freeze"]:
                        chain += [f"trim=start={source_start}:end={source_start+1/fps}","setpts=PTS-STARTPTS",f"tpad=stop_mode=clone:stop_duration={length}"]
                    else:
                        chain += [f"trim=start={source_start}:end={source_end}","setpts=PTS-STARTPTS"]
                        if c["reverse"]:
                            chain.append("reverse")
                        chain.append(f"setpts=(PTS-STARTPTS)/{c['speed']}")
                chain += [f"fps={project['fps']}",f"trim=duration={length}","setpts=PTS-STARTPTS","setsar=1"]
                left,top,right,bottom = p["crop"]
                chain.append(f"crop=iw*{1-left-right}:ih*{1-top-bottom}:iw*{left}:ih*{top}")
                if p["flip_x"]:
                    chain.append("hflip")
                if p["flip_y"]:
                    chain.append("vflip")
                chain.append(f"eq=brightness={p['brightness']}:contrast={p['contrast']}:saturation={p['saturation']}")
                # Uniform opacity commutes with spatial transforms. Evaluate
                # animation at source resolution, before enlarging/rotating,
                # rather than running expressions on every export-sized pixel.
                if c["keyframes"].get("opacity"):
                    opacity = expr(c,"opacity",fps,"T")
                    chain += ["format=rgba",f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({opacity})'"]
                elif p["opacity"] != 1:
                    chain += ["format=rgba",f"colorchannelmixer=aa={p['opacity']}"]
                if p["fit"] == "stretch":
                    chain.append(f"scale={width}:{height}")
                elif p["fit"] == "fill":
                    chain += [f"scale={width}:{height}:force_original_aspect_ratio=increase",f"crop={width}:{height}"]
                else:
                    chain += [f"scale={width}:{height}:force_original_aspect_ratio=decrease","format=rgba",f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black@0"]
                chain += ["format=rgba",f"scale=w='max(2,trunc({width}*({expr(c,'scale',fps)})/2)*2)':h='max(2,trunc({height}*({expr(c,'scale',fps)})/2)*2)':eval=frame"]
                if p["rotation"] or c["keyframes"].get("rotation"):
                    chain.append(f"rotate=a='({expr(c,'rotation',fps)})*PI/180':ow='hypot(iw,ih)':oh='hypot(iw,ih)':c=none")
                for field,mode in (("transition_in","in"),("transition_out","out")):
                    if p[field]:
                        d = p[field]/fps
                        chain.append(f"fade=t={mode}:st={0 if mode=='in' else length-d}:d={d}:alpha=1")
                chain.append(f"setpts=PTS+{start}/TB")
                label = f"v{stream}"
                filters.append(f"[{stream}:v]"+",".join(chain)+f"[{label}]")
                visuals.append((label,c,start,length))
            if audible:
                chain = [f"atrim=start={c['source_in']/fps}:end={c['source_out']/fps}","asetpts=PTS-STARTPTS","aformat=sample_rates=48000:channel_layouts=stereo"]
                if c["reverse"]:
                    chain.append("areverse")
                rate = c["speed"]
                while rate < .5:
                    chain.append("atempo=0.5"); rate /= .5
                while rate > 2:
                    chain.append("atempo=2"); rate /= 2
                chain.append(f"atempo={rate}")
                volume,pan = expr(c,"volume",fps),expr(c,"pan",fps)
                duck = "1"
                if t.get("ducking"):
                    active = "+".join(f"between(t+{start},{x['start']/fps},{(x['start']+x['duration'])/fps})" for x in foreground) or "0"
                    duck = f"if(gt({active},0),0.25,1)"
                chain.append(f"aeval=exprs='val(0)*({volume})*min(1,1-({pan}))*({duck})|val(1)*({volume})*min(1,1+({pan}))*({duck})':channel_layout=stereo")
                for field,mode in (("fade_in","in"),("fade_out","out")):
                    if p[field]:
                        d=p[field]/fps
                        chain.append(f"afade=t={mode}:st={0 if mode=='in' else length-d}:d={d}")
                chain += ["aformat=channel_layouts=stereo",f"apad=whole_dur={length}",f"atrim=duration={length}",f"adelay={round(start*48000)}S:all=1"]
                label = f"a{stream}"
                filters.append(f"[{stream}:a]"+",".join(chain)+f"[{label}]")
                audio.append(label)
        previous="base"
        for i,(label,c,start,length) in enumerate(visuals):
            x = expr(c,"x",fps,f"(t-{start})"); y=expr(c,"y",fps,f"(t-{start})")
            target=f"o{i}"
            filters.append(f"[{previous}][{label}]overlay=x='(W-w)/2+({x})*{sx}':y='(H-h)/2+({y})*{sy}':enable='gte(t,{start})*lt(t,{start+length})':eof_action=pass:repeatlast=0:format=auto[{target}]")
            previous=target
        filters.append(f"[{previous}]format=yuv420p[outv]")
        if audio:
            filters.append("".join(f"[{x}]" for x in audio)+f"amix=inputs={len(audio)}:normalize=0:dropout_transition=0,apad,atrim=duration={seconds},alimiter=limit=0.95:level=false:latency=true,aformat=channel_layouts=stereo[outa]")
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={seconds}[outa]")
        graph=temp/"graph.txt"; graph.write_text(";\n".join(filters))
        temporary=output.with_name(output.stem+".partial.mp4")
        if temporary.exists():
            raise ValueError("Output is already being rendered")
        try:
            run_ffmpeg([*args,graph_option(),str(graph),"-map","[outv]","-map","[outa]","-t",str(seconds),"-c:v","libx264","-preset","ultrafast" if draft else "medium","-crf","28" if draft else "18","-c:a","aac","-b:a","192k","-movflags","+faststart","-n",str(temporary)])
            # Atomic publication only after FFmpeg succeeds.
            os.link(temporary,output)
        finally:
            temporary.unlink(missing_ok=True)
    return output
