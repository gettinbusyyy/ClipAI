import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dotenv import load_dotenv

load_dotenv()

CLIPS_FILE = "clips.json"
OUTPUT_DIR = "output_clips"
# Use system ffmpeg when available (Railway/Linux); fall back to local Windows install.
FFMPEG  = shutil.which("ffmpeg")  or r"C:\Users\Owner\ffmpeg\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe"
FFPROBE = shutil.which("ffprobe") or r"C:\Users\Owner\ffmpeg\ffmpeg-master-latest-win64-gpl\bin\ffprobe.exe"

_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)


def _write_cookies_file() -> "str | None":
    """Materialise YouTube cookies as a Netscape-format temp file for yt-dlp.

    Checks YOUTUBE_COOKIES_B64 (base64-encoded, recommended) then falls back
    to YOUTUBE_COOKIES (raw Netscape text).  Returns None if neither is set.
    Caller must delete the file when finished.
    """
    raw: str = ""

    b64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    if b64:
        try:
            raw = base64.b64decode(b64).decode("utf-8")
        except Exception as exc:
            print(f"[cookies] YOUTUBE_COOKIES_B64 decode error: {exc}")

    if not raw:
        raw = os.getenv("YOUTUBE_COOKIES", "").strip()

    if not raw:
        return None

    fd, path = tempfile.mkstemp(suffix=".txt", prefix="yt_cookies_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        if not raw.startswith("# Netscape HTTP Cookie File"):
            f.write("# Netscape HTTP Cookie File\n")
        f.write(raw)
        if not raw.endswith("\n"):
            f.write("\n")
    return path

def load_clips():
    with open(CLIPS_FILE, "r") as f:
        return json.load(f)

def download_video(url: str) -> str:
    print(f"Downloading video from {url}...")
    output_path = "full_video.mp4"
    cookies_path = _write_cookies_file()
    try:
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--extractor-args", "youtube:player_client=web,mweb,android",
            "--user-agent", _UA,
            "--add-header", "Accept-Language:en-US,en;q=0.9",
            "--sleep-requests", "1",
            "--sleep-interval", "2",
            "-f", "bestvideo+bestaudio/best",
            "-o", output_path,
            "--merge-output-format", "mp4",
        ]
        if cookies_path:
            cmd += ["--cookies", cookies_path]
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            combined = (result.stderr + result.stdout).lower()
            triggers = ("sign in", "bot", "confirm your", "not a robot", "403", "429")
            if any(t in combined for t in triggers):
                raise RuntimeError(
                    "YouTube blocked the download (bot detection). "
                    "Fix: export fresh cookies from Firefox on youtube.com, "
                    "base64-encode the file (`base64 cookies.txt`), "
                    "and set YOUTUBE_COOKIES_B64 in your Railway environment variables. "
                    "Cookies expire roughly every two weeks and must be refreshed manually."
                )
            # Surface the raw yt-dlp stderr for other errors
            raise subprocess.CalledProcessError(
                result.returncode, cmd[0], result.stdout, result.stderr
            )
    finally:
        if cookies_path:
            try:
                os.unlink(cookies_path)
            except OSError:
                pass
    return output_path

def cut_clip(video_path: str, start_ms: int, end_ms: int, output_path: str):
    start_sec = start_ms / 1000
    duration_sec = (end_ms - start_ms) / 1000
    cmd = [
        FFMPEG,
        "-ss", str(start_sec),        # fast seek before -i
        "-i", video_path,
        "-t", str(duration_sec),
        "-c:v", "libx264",
        "-c:a", "aac",
        "-avoid_negative_ts", "make_zero",  # normalise PTS so non-zero starts don't corrupt
        "-movflags", "+faststart",           # moov atom at front — required for valid MP4
        "-y",
        output_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    if not validate_clip(output_path):
        raise RuntimeError(f"ffprobe rejected output file: {output_path}")

    print(f"Saved: {output_path}")


def validate_clip(path: str) -> bool:
    """Return True only if ffprobe can read at least one video stream from the file."""
    if not os.path.exists(path) or os.path.getsize(path) < 1024:
        return False
    result = subprocess.run(
        [
            FFPROBE,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            path,
        ],
        capture_output=True,
    )
    return result.returncode == 0 and b"video" in result.stdout

def load_transcript_words(transcript_path: str) -> list:
    """Parse transcript.txt into [{start, end, text}] dicts.

    Supports both the new [start-end] format and the legacy [start] format.
    """
    words = []
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = re.match(r'\[(\d+)-(\d+)\]\s+(.*)', line)
            if m:
                words.append({"start": int(m.group(1)), "end": int(m.group(2)), "text": m.group(3)})
                continue
            m = re.match(r'\[(\d+)\]\s+(.*)', line)
            if m:
                start = int(m.group(1))
                words.append({"start": start, "end": start + 400, "text": m.group(2)})
    return words


def _ms_to_srt(ms: int) -> str:
    ms = max(0, ms)
    h = ms // 3_600_000; ms %= 3_600_000
    m = ms // 60_000;    ms %= 60_000
    s = ms // 1000;      ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_srt(words: list, clip_start_ms: int, clip_end_ms: int) -> str:
    """Return SRT content for the clip's time window, 4 words per block."""
    clip_words = [w for w in words if clip_start_ms <= w["start"] < clip_end_ms]
    if not clip_words:
        return ""

    WORDS_PER_BLOCK = 4
    blocks = [clip_words[i:i + WORDS_PER_BLOCK] for i in range(0, len(clip_words), WORDS_PER_BLOCK)]
    clip_dur = clip_end_ms - clip_start_ms

    lines = []
    for idx, block in enumerate(blocks):
        t0 = block[0]["start"] - clip_start_ms
        t1 = min(block[-1]["end"] - clip_start_ms, clip_dur)
        if t1 <= t0:
            t1 = min(t0 + 500, clip_dur)
        text = " ".join(w["text"] for w in block)
        lines += [str(idx + 1), f"{_ms_to_srt(t0)} --> {_ms_to_srt(t1)}", text, ""]

    return "\n".join(lines)


def burn_captions(clip_dir: str, raw_filename: str, srt_filename: str, output_filename: str):
    """Burn SRT captions into video.

    Runs ffmpeg from clip_dir so filenames stay relative — avoids Windows
    drive-letter colon escaping inside the subtitles filter string.
    """
    force_style = (
        "FontName=Arial,"
        "FontSize=20,"
        "Bold=1,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "BorderStyle=1,"
        "Outline=3,"
        "Shadow=0,"
        "Alignment=2,"
        "MarginV=50"
    )
    cmd = [
        FFMPEG,
        "-i", raw_filename,
        "-vf", f"subtitles={srt_filename}:force_style='{force_style}'",
        "-c:v", "libx264",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-y",
        output_filename,
    ]
    subprocess.run(cmd, check=True, capture_output=True, cwd=clip_dir)


_STOPWORDS = frozenset({
    'a', 'an', 'the', 'in', 'on', 'at', 'to', 'for', 'of', 'and', 'or', 'but',
    'is', 'are', 'was', 'were', 'be', 'been', 'it', 'this', 'that', 'with',
    'as', 'by', 'i', 'my', 'me', 'we', 'you', 'he', 'she', 'they', 'his',
    'her', 'its', 'our', 'up', 'so', 'do', 'how', 'what', 'why', 'when', 'get',
})


def _pick_keyword(title: str) -> str:
    """Return the longest non-stopword in the title — highlighted yellow in thumbnail."""
    words = [w.strip(".,!?:;'\"—-") for w in title.split()]
    candidates = [w for w in words if w.lower() not in _STOPWORDS and len(w) > 2]
    return max(candidates, key=len) if candidates else ""


def generate_thumbnail(clip_path: str, title: str, output_path: str):
    """Extract frame at 1 s, resize to 1280×720, gradient overlay, title text."""
    from PIL import Image, ImageDraw, ImageFont
    import tempfile

    W, H = 1280, 720
    FONT_SIZE = 58
    PAD_X, PAD_B = 72, 55
    LINE_GAP = 12

    # Extract a single frame at 1 s
    fd, frame_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        subprocess.run(
            [FFMPEG, "-ss", "1", "-i", clip_path,
             "-frames:v", "1", "-q:v", "2", "-y", frame_path],
            check=True, capture_output=True,
        )
        img = Image.open(frame_path).convert("RGB")
    finally:
        try:
            os.unlink(frame_path)
        except OSError:
            pass

    img = img.resize((W, H), Image.LANCZOS)

    # Dark gradient over the bottom third
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ov = ImageDraw.Draw(overlay)
    grad_h = H // 3
    for y in range(grad_h):
        alpha = int(215 * y / grad_h)
        ov.line([(0, H - grad_h + y), (W, H - grad_h + y)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    # Bold font — try Windows paths first, then common Linux locations, then default
    font = None
    for font_path in [
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    ]:
        try:
            font = ImageFont.truetype(font_path, FONT_SIZE)
            break
        except OSError:
            pass
    if font is None:
        font = ImageFont.load_default()

    keyword = _pick_keyword(title).lower()
    draw = ImageDraw.Draw(img)
    max_w = W - 2 * PAD_X

    # Word-wrap title
    lines: list[list[str]] = []
    current: list[str] = []
    current_w = 0.0
    for word in title.split():
        ww = draw.textlength(word + " ", font=font)
        if current and current_w + ww > max_w:
            lines.append(current)
            current, current_w = [word], ww
        else:
            current.append(word)
            current_w += ww
    if current:
        lines.append(current)

    # Render word-by-word so the keyword gets a different colour
    line_h = FONT_SIZE + LINE_GAP
    y = float(H - PAD_B - len(lines) * line_h)
    for line in lines:
        x = float(PAD_X)
        for idx, word in enumerate(line):
            spacing = " " if idx < len(line) - 1 else ""
            clean = word.strip(".,!?:;'\"—-").lower()
            color = (255, 215, 0) if (keyword and clean == keyword) else (255, 255, 255)
            draw.text(
                (int(x), int(y)), word + spacing,
                font=font, fill=color,
                stroke_width=3, stroke_fill=(0, 0, 0),
            )
            x += draw.textlength(word + spacing, font=font)
        y += line_h

    img.save(output_path, "JPEG", quality=90)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="YouTube URL")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    video_path = download_video(args.url)
    clips = load_clips()

    for i, clip in enumerate(clips):
        output_path = os.path.join(OUTPUT_DIR, f"clip_{i+1}_{clip['title'].replace(' ', '_')[:30]}.mp4")
        print(f"\nCutting clip {i+1}: {clip['title']}")
        print(f"Score: {clip.get('score', 'N/A')} | Hook: {clip.get('hook', 'N/A')}")
        cut_clip(video_path, clip["start_time"], clip["end_time"], output_path)

    print(f"\nDone! {len(clips)} clips saved to /{OUTPUT_DIR}")

if __name__ == "__main__":
    main()