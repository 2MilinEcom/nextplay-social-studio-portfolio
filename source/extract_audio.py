import subprocess
from ffmpeg_bin import FFMPEG

def extract_audio(input_video_path, output_audio_path):
    print(f"Extracting audio from {input_video_path} to {output_audio_path}...")
    subprocess.run([
        FFMPEG,
        "-y",
        "-i", input_video_path,
        "-vn",  # disable video
        "-acodec", "libmp3lame",  # encode to MP3
        output_audio_path
    ], check=True)
    print("EXTRACTION COMPLETE.")
    return output_audio_path
