import sys
import os
import base64
import tempfile
import argparse
from dotenv import load_dotenv
import yt_dlp
import assemblyai as aai
load_dotenv()

AUDIO_FILE = "audio.mp3"
TRANSCRIPT_FILE = "transcript.txt"

# mweb (mobile web) client mimics m.youtube.com — same auth context as a
# browser session, so cookies apply directly and bot signals are lower.
_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)


def _write_cookies_file() -> "str | None":
    """Materialise YouTube cookies as a Netscape-format temp file for yt-dlp.

    Checks YOUTUBE_COOKIES_B64 (base64-encoded Netscape text — recommended
    for Railway because it survives copy-paste and line-ending mangling) then
    falls back to YOUTUBE_COOKIES (raw Netscape text).

    Returns the temp file path, or None if neither variable is set.
    Caller is responsible for deleting the file when finished.
    """
    raw: str = ""

    b64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    if b64:
        try:
            raw = base64.b64decode(b64).decode("utf-8")
            print(f"[cookies] loaded from YOUTUBE_COOKIES_B64 ({len(raw)} chars)")
        except Exception as exc:
            print(f"[cookies] YOUTUBE_COOKIES_B64 decode error: {exc}")

    if not raw:
        raw = os.getenv("YOUTUBE_COOKIES", "").strip()
        if raw:
            print(f"[cookies] loaded from YOUTUBE_COOKIES ({len(raw)} chars)")

    if not raw:
        print("[cookies] no cookies configured — proceeding unauthenticated")
        return None

    print(f"[cookies] first 100 chars: {repr(raw[:100])}")

    fd, path = tempfile.mkstemp(suffix=".txt", prefix="yt_cookies_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        if not raw.startswith("# Netscape HTTP Cookie File"):
            f.write("# Netscape HTTP Cookie File\n")
        f.write(raw)
        if not raw.endswith("\n"):
            f.write("\n")

    print(f"[cookies] temp file: {path} ({os.path.getsize(path)} bytes)")
    return path


def _raise_if_bot_blocked(exc: Exception) -> None:
    """Re-raise with a clear message when YouTube returns a bot-detection error."""
    triggers = ("sign in", "bot", "confirm your", "not a robot", "403", "429")
    if any(t in str(exc).lower() for t in triggers):
        raise RuntimeError(
            "YouTube blocked the download (bot detection). "
            "Fix: export fresh cookies from Firefox on youtube.com, "
            "base64-encode the file (`base64 cookies.txt`), "
            "and set YOUTUBE_COOKIES_B64 in your Railway environment variables. "
            "Cookies expire roughly every two weeks and must be refreshed manually."
        ) from exc


def download_audio(url: str) -> str:
    cookies_path = _write_cookies_file()
    try:
        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "outtmpl": "audio.%(ext)s",
            "quiet": False,
            "no_warnings": False,
            # web → mweb → android priority; ios is excluded because it
            # silently drops cookies (uses its own OAuth internally).
            "extractor_args": {"youtube": {"player_client": ["web", "mweb", "android"]}},
            "http_headers": {
                "User-Agent": _UA,
                "Accept-Language": "en-US,en;q=0.9",
            },
            "sleep_interval_requests": 1,
            "sleep_interval": 2,
            **({"cookiefile": cookies_path} if cookies_path else {}),
        }
        print(f"Downloading audio from: {url}")
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as exc:
            _raise_if_bot_blocked(exc)
            raise
    finally:
        if cookies_path:
            try:
                os.unlink(cookies_path)
            except OSError:
                pass
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
