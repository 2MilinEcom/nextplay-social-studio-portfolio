from openai import OpenAI
import os
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def transcribe_videos(list_of_audios: list[str], video_paths: list[str]):
    transcribed_audios = {}
    for i, audio in enumerate(list_of_audios):
        print(f"Processing audio file: {audio}")
        with open(audio, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="verbose_json"
            )
            
            transcribed_audios[f"VIDEO: {i + 1}"] = {
                "path": video_paths[i],
                "segments": transcript.model_dump()["segments"]
            }
    return transcribed_audios
    