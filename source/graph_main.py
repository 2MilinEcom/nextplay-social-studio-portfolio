# This will be what will be cutting up videos for us

import subprocess
import os
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END
from openai import OpenAI
import json

load_dotenv()

from extract_audio import extract_audio
from splice_videos import splice_videos
from transcribe_videos import transcribe_videos

# Set to True after adding OpenAI credits. False = local splice, no API.
USE_OPENAI = False

# Set up OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Videos already in this project (Archive/downloads)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# Prefer the latest SnapTik downloads; fall back to the project folder.
HOME_DOWNLOADS = os.path.expanduser("~/Downloads")
TIKTOK_VIDEOS_DIR = os.path.join(HOME_DOWNLOADS, "tiktok-videos")
PROJECT_DOWNLOADS = os.path.join(ROOT, "downloads")
OUTPUT_DIR = os.path.join(ROOT, "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def _list_videos(*folders):
    videos = []
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith(".mp4"):
                videos.append(os.path.join(folder, name))
    return videos


available = _list_videos(TIKTOK_VIDEOS_DIR, PROJECT_DOWNLOADS, HOME_DOWNLOADS)
if len(available) < 2:
    raise FileNotFoundError(
        "Need at least 2 .mp4 files. Looked in:\n"
        f"  {TIKTOK_VIDEOS_DIR}\n"
        f"  {PROJECT_DOWNLOADS}\n"
        f"  {HOME_DOWNLOADS}\n"
        f"Found {len(available)}."
    )

INPUT_VIDEO_PATH_0 = available[0]
INPUT_VIDEO_PATH_1 = available[1]
print(f"Using videos:\n  {INPUT_VIDEO_PATH_0}\n  {INPUT_VIDEO_PATH_1}")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "EDITED.mp4")

video_paths = [INPUT_VIDEO_PATH_0, INPUT_VIDEO_PATH_1]
missing = [p for p in video_paths if not os.path.exists(p)]
if missing:
    raise FileNotFoundError(
        "Video file(s) not found:\n" + "\n".join(missing)
    )

transcribed_audios = None
if USE_OPENAI:
    video_0 = extract_audio(INPUT_VIDEO_PATH_0, os.path.join(OUTPUT_DIR, "audio_0.mp3"))
    video_1 = extract_audio(INPUT_VIDEO_PATH_1, os.path.join(OUTPUT_DIR, "audio_1.mp3"))
    transcribed_audios = transcribe_videos([video_0, video_1], video_paths)
else:
    print(
        "⚠️  OpenAI credits are empty, so Whisper/GPT-4 are skipped.\n"
        "    Stitching a local cut instead (first seconds of each video)."
    )


def local_edit_plan():
    return {
        "segments": [
            {
                "video_path": INPUT_VIDEO_PATH_0,
                "start": 0,
                "end": 5,
                "role": "hook",
                "reason": "Local fallback: opening of first video.",
            },
            {
                "video_path": INPUT_VIDEO_PATH_1,
                "start": 0,
                "end": 8,
                "role": "bridge",
                "reason": "Local fallback: opening of second video.",
            },
        ],
        "output_path": OUTPUT_PATH,
    }


def plan_video_edit(state):
    if not USE_OPENAI:
        return {"edit_plan": local_edit_plan()}

    input_transcript = state["transcript"]
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {
                "role": "system",
                "content": "You are a video editor that selects segments from transcripts to assemble short, high-engaging videos that end with customer sales."
            },
            {
                "role": "user",
                "content": f"""
    You are given a transcript with timestamped segments from multiple videos. Choose which segments (with timestamps) to use. Provide start, end, and source video for each clip. Do not guess timestamps — only use those provided.

    Your job is to:
    - Identify and extract sections that best fit the structure:
    1. Hook (captures attention in the first 3–10 seconds)
    2. Bridge (transitions or features a solution or wow factor)
    3. Body (main content, benefits, emotional story, etc.)
    4. Call to Action or Ending (what the viewer should do or final punch)

    Return the results in JSON like:
    {{
    "segments": [
        {{
        "video_path": "{INPUT_VIDEO_PATH_0}",
        "start": 0,
        "end": 5,
        "role": "hook",
        "reason": "Grabs attention by showing a problem everyone has."
        }},
        {{
        "video_path": "{INPUT_VIDEO_PATH_1}",
        "start": 0,
        "end": 8,
        "role": "bridge",
        "reason": "Sets up a smooth transition to the solution."
        }}
    ],
    "output_path": "{OUTPUT_PATH}"
    }}

    Transcript:
    {input_transcript}
    """
                }
            ],
            temperature=0.3
        )
    plan = json.loads(response.choices[0].message.content or "{}")
    return {"edit_plan": plan}

# Run the splicing tool

def run_splice_tool(state):
    plan = state["edit_plan"]
    segments = plan["segments"]
    output_path = plan["output_path"]
    result = splice_videos(segments, output_path)
    return {"final_output": result}

# Build the graph

graph = StateGraph(dict)
graph.add_node("plan_edit", plan_video_edit)
graph.add_node("splice", run_splice_tool)

graph.set_entry_point("plan_edit")
graph.add_edge("plan_edit", "splice")
graph.add_edge("splice", END)

app = graph.compile()

# Sample run
if __name__ == "__main__":
    transcript = transcribed_audios
    result = app.invoke({"transcript": transcript})
    print("✅ Final stitched video saved to:", result["final_output"])
