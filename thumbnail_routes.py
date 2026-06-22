"""
ClipAI — Thumbnail Creator
==========================
A self-contained Flask Blueprint. Drop this file next to app.py and register it:

    from thumbnail_routes import thumbnail_bp
    app.register_blueprint(thumbnail_bp)

Routes
------
GET  /thumbnail               -> editor page (login required)
GET  /thumbnail/clips         -> JSON list of generated clips for the dropdown
POST /thumbnail/extract-frames-> {clip, count} -> candidate frames as data URLs
POST /thumbnail/remove-bg     -> {image: dataURL} -> transparent PNG data URL

Config (set in Railway Variables or app.config; sensible defaults below):
    CLIPS_DIR        absolute path where ClipAI writes finished clips
    REMBG_MODEL      rembg model name (default: u2net_human_seg, best for people)
"""

import os
import base64
import shutil
import subprocess
import tempfile
import traceback
from pathlib import Path

from flask import Blueprint, render_template, request, jsonify, current_app

try:
    from flask_login import login_required
except Exception:  # keep importable even if auth isn't wired yet
    def login_required(f):
        return f

thumbnail_bp = Blueprint("thumbnail", __name__)

# --- configuration helpers ---------------------------------------------------

def _clips_dir() -> Path:
    # Order: app.config -> env -> common defaults. ADAPT this to where ClipAI
    # actually writes finished clips.
    configured = current_app.config.get("CLIPS_DIR") or os.environ.get("CLIPS_DIR")
    if configured:
        return Path(configured)
    for candidate in ("static/clips", "clips", "static/output", "output"):
        p = Path(current_app.root_path) / candidate
        if p.is_dir():
            return p
    return Path(current_app.root_path) / "clips"


def _rembg_model() -> str:
    return (current_app.config.get("REMBG_MODEL")
            or os.environ.get("REMBG_MODEL")
            or "u2net_human_seg")


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def _safe_clip_path(name: str) -> Path:
    """Resolve a user-supplied clip name to a real file inside CLIPS_DIR only."""
    base = _clips_dir().resolve()
    # strip any path components — only a bare filename is allowed
    target = (base / Path(name).name).resolve()
    if base not in target.parents and target != base:
        raise ValueError("Invalid clip path")
    if not target.is_file():
        raise FileNotFoundError(name)
    return target


def _run(cmd: list) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _probe_duration(path: Path) -> float:
    cp = _run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    try:
        return float(cp.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def _frame_to_data_url(path: Path) -> str:
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


# --- routes ------------------------------------------------------------------

@thumbnail_bp.route("/thumbnail")
@login_required
def thumbnail_page():
    return render_template("thumbnail.html")


@thumbnail_bp.route("/thumbnail/clips")
@login_required
def list_clips():
    """Return clips for the dropdown. ADAPT to your model if you track clips
    in SQLite per user — here we just scan CLIPS_DIR newest-first."""
    d = _clips_dir()
    items = []
    if d.is_dir():
        files = [f for f in d.iterdir()
                 if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        items = [{"name": f.name} for f in files[:50]]
    return jsonify({"clips": items})


@thumbnail_bp.route("/thumbnail/extract-frames", methods=["POST"])
@login_required
def extract_frames():
    data = request.get_json(silent=True) or {}
    clip_name = data.get("clip", "")
    count = max(2, min(int(data.get("count", 6)), 8))

    try:
        clip = _safe_clip_path(clip_name)
    except (ValueError, FileNotFoundError):
        return jsonify({"error": "Clip not found"}), 404

    if not shutil.which("ffmpeg"):
        return jsonify({"error": "ffmpeg not available on the server"}), 500

    duration = _probe_duration(clip)
    frames = []
    tmp = Path(tempfile.mkdtemp(prefix="clipai_thumbs_"))
    try:
        # 1) Auto "best" frame via ffmpeg's thumbnail filter (most representative)
        best = tmp / "auto.jpg"
        cp = _run([
            "ffmpeg", "-y", "-i", str(clip),
            "-vf", "thumbnail", "-frames:v", "1",
            "-q:v", "2", str(best),
        ])
        if best.is_file():
            frames.append({"label": "Auto", "image": _frame_to_data_url(best)})

        # 2) Evenly spaced candidates across the clip
        if duration > 0:
            n = count - 1 if frames else count
            for i in range(n):
                # spread between 8% and 92% of the clip
                pct = 0.08 + (0.84 * (i / max(1, n - 1)))
                ts = duration * pct
                out = tmp / f"f{i}.jpg"
                _run([
                    "ffmpeg", "-y", "-ss", f"{ts:.2f}", "-i", str(clip),
                    "-frames:v", "1", "-q:v", "2", str(out),
                ])
                if out.is_file():
                    frames.append({
                        "label": f"{int(pct * 100)}%",
                        "image": _frame_to_data_url(out),
                    })

        if not frames:
            return jsonify({"error": "Could not extract frames"}), 500
        return jsonify({"frames": frames})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# rembg is heavy — import lazily and cache the session so the model is only
# loaded into memory on first use (not at app boot).
_REMBG_SESSION = None


def _get_rembg_session():
    global _REMBG_SESSION
    if _REMBG_SESSION is None:
        from rembg import new_session
        _REMBG_SESSION = new_session(_rembg_model())
    return _REMBG_SESSION


@thumbnail_bp.route("/thumbnail/remove-bg", methods=["POST"])
@login_required
def remove_bg():
    data = request.get_json(silent=True) or {}
    image_url = data.get("image", "")
    if not image_url.startswith("data:image"):
        return jsonify({"error": "Send an image as a data URL"}), 400

    try:
        header, b64 = image_url.split(",", 1)
        input_bytes = base64.b64decode(b64)
    except Exception:
        return jsonify({"error": "Malformed image data"}), 400

    try:
        from rembg import remove
        session = _get_rembg_session()
        output_bytes = remove(input_bytes, session=session)
    except Exception as e:
        tb = traceback.format_exc()
        current_app.logger.error("rembg failed — %s: %s\n%s", type(e).__name__, e, tb)
        return jsonify({
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb,
        }), 500

    out_b64 = base64.b64encode(output_bytes).decode("ascii")
    return jsonify({"image": f"data:image/png;base64,{out_b64}"})
