import os
import sys
import re
import json
import uuid
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

from flask import Flask, render_template, request, jsonify, send_from_directory

app = Flask(__name__)

NICHES = {
    "1": "Tech & Startups",
    "2": "Finance & Business",
    "3": "Health & Fitness",
    "4": "Self-Improvement & Motivation",
    "5": "Entertainment & Humor",
    "6": "Education & Explainer",
    "7": "News & Current Events",
    "8": "Science & Nature",
    "9": "Food & Lifestyle",
    "10": "Sports & Gaming",
}

jobs = {}       # job_id  -> pipeline state dict
burn_jobs = {}  # burn_id -> burn state dict


def parse_srt(srt_content):
    """Return list of {index, start, end, text} dicts from SRT text."""
    blocks = []
    if not srt_content:
        return blocks
    for entry in re.split(r'\n\s*\n', srt_content.strip()):
        lines = entry.strip().splitlines()
        if len(lines) < 3:
            continue
        try:
            parts = lines[1].strip().split(' --> ')
            blocks.append({
                "index": int(lines[0].strip()),
                "start": parts[0].strip(),
                "end":   parts[1].strip(),
                "text":  " ".join(l.strip() for l in lines[2:]),
            })
        except (ValueError, IndexError):
            continue
    return blocks


def run_pipeline(job_id: str, url: str, niche: str, count: int = 3):
    def update(step, message):
        jobs[job_id].update({"step": step, "message": message})

    try:
        update(1, "Downloading audio from YouTube...")
        from transcribe import download_audio, transcribe_audio, save_transcript
        audio_path = download_audio(url)

        update(1, "Transcribing audio — this takes a minute...")
        transcript_obj = transcribe_audio(audio_path)
        save_transcript(transcript_obj)

        update(2, "Analyzing transcript with Claude AI...")
        from scorer import load_transcript, score_clips
        transcript_text = load_transcript("transcript.txt")
        clips = score_clips(transcript_text, niche, count)
        with open("clips.json", "w") as f:
            json.dump(clips, f, indent=2)

        update(3, "Downloading video from YouTube...")
        from clipper import download_video, cut_clip, load_transcript_words, generate_srt, burn_captions, generate_thumbnail
        os.makedirs("output_clips", exist_ok=True)
        video_path = download_video(url)
        words = load_transcript_words("transcript.txt")
        clip_dir = os.path.abspath("output_clips")

        clip_files = []
        for i, clip in enumerate(clips):
            update(3, f"Cutting clip {i + 1} of {len(clips)}: {clip.get('title', '')}")
            safe_title = "".join(
                c if c.isalnum() or c in "-_" else "_"
                for c in clip.get("title", f"clip_{i+1}")
            )[:30]
            filename = f"clip_{i + 1}_{safe_title}.mp4"
            output_path = os.path.join("output_clips", filename)
            raw_filename = f"raw_{i + 1}_{safe_title}.mp4"
            raw_path = os.path.join("output_clips", raw_filename)
            srt_filename = f"{i + 1}_{safe_title}.srt"
            srt_path = os.path.join("output_clips", srt_filename)
            corrupt_reason = None
            srt_content = ""
            thumb_filename = None
            try:
                cut_clip(video_path, clip["start_time"], clip["end_time"], raw_path)
                srt_content = generate_srt(words, clip["start_time"], clip["end_time"])
                if srt_content:
                    update(3, f"Burning captions into clip {i + 1} of {len(clips)}: {clip.get('title', '')}")
                    with open(srt_path, "w", encoding="utf-8") as f:
                        f.write(srt_content)
                    burn_captions(clip_dir, raw_filename, srt_filename, filename)
                    os.remove(srt_path)
                else:
                    os.rename(raw_path, output_path)
                if os.path.exists(raw_path):
                    os.remove(raw_path)
                valid = True
                try:
                    update(3, f"Generating thumbnail for clip {i + 1} of {len(clips)}...")
                    thumb_base = os.path.splitext(filename)[0] + ".jpg"
                    generate_thumbnail(
                        output_path,
                        clip.get("title", ""),
                        os.path.join("output_clips", thumb_base),
                    )
                    thumb_filename = thumb_base
                except Exception:
                    pass  # thumbnail failure is non-fatal
            except Exception as clip_err:
                valid = False
                corrupt_reason = str(clip_err)
                for temp in [raw_path, srt_path]:
                    if os.path.exists(temp):
                        os.remove(temp)
            clip_files.append({
                "filename": filename,
                "title": clip.get("title", f"Clip {i + 1}"),
                "score": clip.get("score", "N/A"),
                "hook": clip.get("hook", ""),
                "reason": clip.get("reason", ""),
                "valid": valid,
                "corrupt_reason": corrupt_reason,
                "srt_content": srt_content,
                "start_time": clip["start_time"],
                "end_time": clip["end_time"],
                "thumbnail": thumb_filename,
            })

        jobs[job_id].update({
            "status": "done",
            "step": 4,
            "message": f"Done! {len(clip_files)} clips ready.",
            "clips": clip_files,
        })

    except Exception as e:
        jobs[job_id].update({
            "status": "error",
            "step": jobs[job_id].get("step", 0),
            "message": str(e),
        })


@app.route("/debug-ffmpeg")
def debug_ffmpeg():
    import subprocess as sp
    def run(cmd):
        try:
            r = sp.run(cmd, capture_output=True, text=True, timeout=15)
            return {"stdout": r.stdout.strip(), "stderr": r.stderr.strip(), "returncode": r.returncode}
        except Exception as e:
            return {"error": str(e)}

    return jsonify({
        "PATH":              os.environ.get("PATH", ""),
        "which_ffmpeg":      run(["which", "ffmpeg"]),
        "which_ffprobe":     run(["which", "ffprobe"]),
        "find_nix_ffmpeg":   run(["find", "/nix", "-name", "ffmpeg",  "-type", "f"]),
        "find_nix_ffprobe":  run(["find", "/nix", "-name", "ffprobe", "-type", "f"]),
        "find_usr_ffmpeg":   run(["find", "/usr", "-name", "ffmpeg",  "-type", "f"]),
        "shutil_which":      __import__("shutil").which("ffmpeg"),
    })


@app.route("/")
def index():
    return render_template("index.html", niches=NICHES)


@app.route("/process", methods=["POST"])
def process():
    url = request.form.get("url", "").strip()
    niche = request.form.get("niche", "").strip()
    if not url or not niche:
        return jsonify({"error": "YouTube URL and niche are required"}), 400

    try:
        count = int(request.form.get("count", "3"))
    except ValueError:
        count = 3
    count = max(1, min(15, count))  # clamp to sane range

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running", "step": 0, "message": "Starting...", "clips": []}

    t = threading.Thread(target=run_pipeline, args=(job_id, url, niche, count), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/download/<path:filename>")
def download(filename):
    return send_from_directory(
        os.path.join(BASE_DIR, "output_clips"),
        filename,
        as_attachment=True,
    )


@app.route("/thumbnail/<path:filename>")
def thumbnail(filename):
    return send_from_directory(os.path.join(BASE_DIR, "output_clips"), filename)


def do_burn(burn_id, job_id, clip_index, clip_info, srt_content):
    from clipper import cut_clip, burn_captions
    raw_path = None
    srt_path = None
    try:
        filename   = clip_info["filename"]
        start_time = clip_info["start_time"]
        end_time   = clip_info["end_time"]
        base       = os.path.splitext(filename)[0]
        clip_dir   = os.path.abspath("output_clips")

        raw_filename = f"reburn_raw_{base}.mp4"
        srt_filename = f"reburn_{base}.srt"
        raw_path = os.path.join("output_clips", raw_filename)
        srt_path = os.path.join("output_clips", srt_filename)

        burn_jobs[burn_id]["message"] = "Re-cutting clip..."
        cut_clip("full_video.mp4", start_time, end_time, raw_path)

        burn_jobs[burn_id]["message"] = "Burning captions..."
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)
        burn_captions(clip_dir, raw_filename, srt_filename, filename)

        # Persist the edited SRT back into the job so re-opens show updated text
        if job_id in jobs:
            clips = jobs[job_id].get("clips", [])
            if clip_index < len(clips):
                clips[clip_index]["srt_content"] = srt_content

        burn_jobs[burn_id].update({
            "status": "done",
            "message": "Captions burned!",
            "filename": filename,
        })
    except Exception as e:
        burn_jobs[burn_id].update({"status": "error", "message": str(e)})
    finally:
        for p in [raw_path, srt_path]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


@app.route("/burn_captions", methods=["POST"])
def burn_captions_route():
    data = request.get_json(silent=True) or {}
    job_id     = data.get("job_id")
    clip_index = data.get("clip_index")
    srt_content = data.get("srt_content", "")

    if job_id is None or clip_index is None:
        return jsonify({"error": "job_id and clip_index are required"}), 400

    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    clip_list = job.get("clips", [])
    if clip_index >= len(clip_list):
        return jsonify({"error": "Clip index out of range"}), 400

    burn_id = str(uuid.uuid4())
    burn_jobs[burn_id] = {"status": "running", "message": "Starting..."}

    threading.Thread(
        target=do_burn,
        args=(burn_id, job_id, clip_index, clip_list[clip_index], srt_content),
        daemon=True,
    ).start()

    return jsonify({"burn_id": burn_id})


@app.route("/burn_status/<burn_id>")
def burn_status(burn_id):
    job = burn_jobs.get(burn_id)
    if not job:
        return jsonify({"error": "Burn job not found"}), 404
    return jsonify(job)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
