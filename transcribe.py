import sys
import os
import shutil
import argparse
from dotenv import load_dotenv
import yt_dlp
import assemblyai as aai
load_dotenv()

AUDIO_FILE = "audio.mp3"
TRANSCRIPT_FILE = "transcript.txt"

def download_audio(url: str) -> str:
    # web client uses Node.js for nsig deciphering; android skips JS entirely.
    # Ordering depends on whether node is available so the warning never appears.
    if shutil.which("node"):
        player_clients = ["web", "android"]
    else:
        player_clients = ["android", "web"]

    ydl_opts = {
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "outtmpl": "audio.%(ext)s",
        "quiet": False,
        "no_warnings": False,
        "extractor_args": {"youtube": {"player_client": player_clients}},
    }
    print(f"Downloading audio from: {url}")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return AUDIO_FILE

def transcribe_audio(audio_path: str):
    api_key = os.getenv("ASSEMBLYAI_API_KEY")
    if not api_key:
        raise ValueError("ASSEMBLYAI_API_KEY environment variable not set")
    aai.settings.api_key = api_key
    transcriber = aai.Transcriber(config=aai.TranscriptionConfig(speech_models=[aai.SpeechModel.universal]))
    print("Uploading and transcribing audio (this may take a minute)...")
    transcript = transcriber.transcribe(audio_path)
    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"Transcription failed: {transcript.error}")
    return transcript

def save_transcript(transcript):
    with open(TRANSCRIPT_FILE, "w") as f:
        for word in transcript.words:
            f.write(f"[{word.start}-{word.end}] {word.text}\n")
    print(f"Transcript saved to {TRANSCRIPT_FILE}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="YouTube URL")
    args = parser.parse_args()
    audio_path = download_audio(args.url)
    transcript = transcribe_audio(audio_path)
    save_transcript(transcript)

if __name__ == "__main__":
    main()
