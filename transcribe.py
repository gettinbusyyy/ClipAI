import sys
import os
import base64
import shutil
import tempfile
import argparse
from dotenv import load_dotenv
import yt_dlp
import assemblyai as aai
load_dotenv()

_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
COOKIES_DIR = os.environ.get("COOKIES_DIR", os.path.join(_BASE_DIR, "cookies"))

_NIX_BINS = [
    "/nix/var/nix/profiles/default/bin",
    "/root/.nix-profile/bin",
    "/run/current-system/sw/bin",
    "/usr/local/bin",
    "/usr/bin",
]

def _find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for directory in _NIX_BINS:
        candidate = os.path.join(directory, "ffmpeg")
        if os.path.isfile(candidate):
            return candidate
    return r"C:\Users\Owner\ffmpeg\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe"

AUDIO_FILE = "audio.mp3"
TRANSCRIPT_FILE = "transcript.txt"

_UA_WEB = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)
_UA_ANDROID = "com.google.android.youtube/17.36.4 (Linux; U; Android 12; GB) gzip"
_UA_IOS     = "com.google.ios.youtube/19.29.1 (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X)"

# android/ios handle auth internally — cookies are ignored by those clients
# web (mweb) is the last resort and benefits from cookie auth
_PLAYER_ATTEMPTS = [
    ("android", _UA_ANDROID, False),
    ("ios",     _UA_IOS,     False),
    ("web",     _UA_WEB,     True),
]


def _cookies_to_netscape(raw: str) -> str:
    """Return raw as-is if already Netscape format, or convert from JSON.

    Handles the EditThisCookie / Chrome JSON export format:
      [ { "domain": ".youtube.com", "name": "SID", "value": "...", ... } ]
    and the wrapped variant { "cookies": [ ... ] }.
    """
    stripped = raw.strip()
    if stripped.startswith("# Netscape HTTP Cookie File") or "\t" in stripped:
        # Already Netscape — pass through unchanged
        return raw

    # Try JSON parse
    import json as _json
    try:
        data = _json.loads(stripped)
    except _json.JSONDecodeError:
        # Not JSON either; return as-is and let yt-dlp surface the error
        return raw

    # Unwrap { "cookies": [...] } envelope if present
    if isinstance(data, dict):
        data = data.get("cookies", [])

    lines = ["# Netscape HTTP Cookie File"]
    for c in data:
        domain  = c.get("domain", "")
        flag    = "TRUE" if domain.startswith(".") else "FALSE"
        path    = c.get("path", "/")
        secure  = "TRUE" if c.get("secure", False) else "FALSE"
        # expirationDate (Chrome) or expires (Firefox/generic); 0 = session
        expiry  = int(c.get("expirationDate", c.get("expires", 0)) or 0)
        name    = c.get("name", "")
        value   = c.get("value", "")
        lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}")

    print(f"[cookies] converted {len(data)} JSON cookies to Netscape format")
    return "\n".join(lines) + "\n"


def _write_cookies_file() -> "str | None":
    """Materialise YouTube cookies as a Netscape-format temp file for yt-dlp.

    Reads YOUTUBE_COOKIES (raw Netscape text or base64-encoded Netscape text).
    Automatically converts base64 or JSON cookie exports to Netscape format.

    Returns the temp file path, or None if the variable is not set.
    Caller is responsible for deleting the file when finished.
    """
    raw: str = ""

    raw = os.getenv("YOUTUBE_COOKIES", "").strip()
    if raw:
        # Auto-decode if it looks like base64 (no tabs, no Netscape header, no JSON)
        if "\t" not in raw and not raw.startswith(("# Netscape", "[", "{")):
            try:
                raw = base64.b64decode(raw).decode("utf-8")
                print(f"[cookies] YOUTUBE_COOKIES was base64-encoded, decoded ({len(raw)} chars)")
            except Exception:
                pass  # Not base64 — use raw value as-is
        print(f"[cookies] loaded from YOUTUBE_COOKIES ({len(raw)} chars)")

    if not raw:
        cookie_file = os.path.join(COOKIES_DIR, "cookies.txt")
        if os.path.isfile(cookie_file):
            with open(cookie_file, "r", encoding="utf-8") as _cf:
                raw = _cf.read().strip()
            if raw:
                print(f"[cookies] loaded from {cookie_file} ({len(raw)} chars)")

    if not raw:
        print("[cookies] no cookies configured — proceeding unauthenticated")
        return None

    print(f"[cookies] raw first 200 chars: {repr(raw[:200])}")
    is_json = raw.strip().startswith(("[", "{"))
    print(f"[cookies] detected format: {'JSON' if is_json else 'Netscape/unknown'}")

    netscape = _cookies_to_netscape(raw)

    print(f"[cookies] netscape first 200 chars: {repr(netscape[:200])}")

    fd, path = tempfile.mkstemp(suffix=".txt", prefix="yt_cookies_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(netscape)
        if not netscape.endswith("\n"):
            f.write("\n")

    print(f"[cookies] temp file: {path} ({os.path.getsize(path)} bytes)")
    return path


_BOT_TRIGGERS = ("sign in", "bot", "confirm your", "not a robot", "403", "429")
_BOT_FIX_MSG = (
    "YouTube blocked the download with all player clients (android → ios → web). "
    "Fix: export fresh cookies from Firefox on youtube.com, "
    "base64-encode the file (`base64 cookies.txt`), "
    "and set YOUTUBE_COOKIES in your Railway environment variables. "
    "Cookies expire roughly every two weeks and must be refreshed manually."
)

def _is_bot_blocked(exc: Exception) -> bool:
    return any(t in str(exc).lower() for t in _BOT_TRIGGERS)

def _raise_if_bot_blocked(exc: Exception) -> None:
    if _is_bot_blocked(exc):
        raise RuntimeError(_BOT_FIX_MSG) from exc


def download_audio(url: str) -> str:
    cookies_path = _write_cookies_file()
    last_exc = None
    try:
        for player_client, ua, use_cookies in _PLAYER_ATTEMPTS:
            print(f"[download] trying player_client={player_client}")
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
                "extractor_args": {"youtube": {"player_client": [player_client]}},
                "http_headers": {
                    "User-Agent": ua,
                    "Accept-Language": "en-US,en;q=0.9",
                },
                "sleep_interval_requests": 1,
                "sleep_interval": 2,
                "ffmpeg_location": _find_ffmpeg(),
                **({"cookiefile": cookies_path} if (use_cookies and cookies_path) else {}),
            }
            print(f"Downloading audio from: {url}")
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                return AUDIO_FILE
            except yt_dlp.utils.DownloadError as exc:
                last_exc = exc
                if _is_bot_blocked(exc):
                    print(f"[download] {player_client} blocked by bot detection, trying next...")
                    continue
                raise
        raise RuntimeError(_BOT_FIX_MSG) from last_exc
    finally:
        if cookies_path:
            try:
                os.unlink(cookies_path)
            except OSError:
                pass

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
