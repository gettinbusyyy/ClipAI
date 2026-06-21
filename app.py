import os
import sys
import re
import json
import uuid
import secrets
import datetime
import threading
from datetime import timedelta

import stripe
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin,
    login_user, logout_user, current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

from flask import (
    Flask, render_template, request, jsonify,
    send_from_directory, redirect, session as flask_session, url_for,
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=90)
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'clipai.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = ""

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

PLANS = {
    "basic": {
        "name": "Basic",
        "monthly_price": 19,
        "amount": 1900,
        "clips_per_day": 10,
        "features": [
            "10 clips per day",
            "Short, medium & long clips",
            "Auto-captions",
            "Thumbnail generation",
            "In-browser preview",
        ],
    },
    "pro": {
        "name": "Pro",
        "monthly_price": 49,
        "amount": 4900,
        "clips_per_day": None,
        "features": [
            "Unlimited clips per day",
            "Short, medium & long clips",
            "Auto-captions",
            "Thumbnail generation",
            "In-browser preview",
            "Priority processing",
        ],
    },
}
FREE_CLIPS_PER_DAY = 3


# ── Database model ────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    id                     = db.Column(db.Integer, primary_key=True)
    email                  = db.Column(db.String(255), unique=True, nullable=False)
    password_hash          = db.Column(db.String(255), nullable=False)
    plan                   = db.Column(db.String(20), default="free")
    stripe_customer_id     = db.Column(db.String(255))
    stripe_subscription_id = db.Column(db.String(255))
    clips_used_today       = db.Column(db.Integer, default=0)
    clips_date             = db.Column(db.String(10))
    created_at             = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    def set_password(self, pw: str):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))


with app.app_context():
    db.create_all()


@app.before_request
def _make_session_permanent():
    flask_session.permanent = True


# ── Plan / usage helpers ──────────────────────────────────────────────────────

def get_plan() -> str:
    if current_user.is_authenticated:
        return current_user.plan or "free"
    return flask_session.get("plan", "free")


def get_daily_limit():
    plan = get_plan()
    if plan == "pro":
        return None
    if plan == "basic":
        return PLANS["basic"]["clips_per_day"]
    return FREE_CLIPS_PER_DAY


def get_daily_used() -> int:
    today = datetime.date.today().isoformat()
    if current_user.is_authenticated:
        if current_user.clips_date != today:
            current_user.clips_used_today = 0
            current_user.clips_date = today
            db.session.commit()
        return current_user.clips_used_today or 0
    if flask_session.get("clip_date") != today:
        flask_session["clip_date"] = today
        flask_session["clip_count"] = 0
    return flask_session.get("clip_count", 0)


def consume_clips(n: int):
    today = datetime.date.today().isoformat()
    if current_user.is_authenticated:
        if current_user.clips_date != today:
            current_user.clips_used_today = 0
            current_user.clips_date = today
        current_user.clips_used_today = (current_user.clips_used_today or 0) + n
        db.session.commit()
    else:
        if flask_session.get("clip_date") != today:
            flask_session["clip_date"] = today
            flask_session["clip_count"] = 0
        flask_session["clip_count"] = flask_session.get("clip_count", 0) + n

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


def run_pipeline(job_id: str, url: str, niche: str, count: int = 3, clip_length: str = "medium"):
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
        from scorer import load_transcript, score_clips, get_video_duration_ms, validate_and_adjust
        transcript_text = load_transcript("transcript.txt")
        duration_ms = get_video_duration_ms(transcript_text)
        count, clip_length, adjustment_note = validate_and_adjust(duration_ms, count, clip_length)
        if adjustment_note:
            jobs[job_id]["adjustment_note"] = adjustment_note
            print(f"[pipeline] {adjustment_note}")
        clips = score_clips(transcript_text, niche, count, clip_length, duration_ms)
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
            "adjustment_note": jobs[job_id].get("adjustment_note"),
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
    limit = get_daily_limit()
    used  = get_daily_used()
    return render_template(
        "index.html",
        niches=NICHES,
        plan=get_plan(),
        daily_limit=limit,
        daily_used=used,
        daily_remaining=(limit - used) if limit is not None else None,
        current_user=current_user,
    )


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
    count = max(1, min(15, count))

    clip_length = request.form.get("clip_length", "medium").strip()
    if clip_length not in ("short", "medium", "long"):
        clip_length = "medium"

    # Enforce daily clip limit
    limit = get_daily_limit()
    used  = get_daily_used()
    if limit is not None:
        remaining = limit - used
        if remaining <= 0:
            return jsonify({
                "error": "limit_reached",
                "message": (
                    f"You've used all {limit} clips in your daily allowance "
                    f"({'free' if get_plan() == 'free' else get_plan()} plan). "
                    "Upgrade to generate more."
                ),
                "plan": get_plan(),
                "limit": limit,
            }), 429
        count = min(count, remaining)

    consume_clips(count)

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running", "step": 0, "message": "Starting...", "clips": []}

    t = threading.Thread(target=run_pipeline, args=(job_id, url, niche, count, clip_length), daemon=True)
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


@app.route("/stream/<path:filename>")
def stream_clip(filename):
    # Served without as_attachment so the browser can play it inline.
    # Flask's send_file supports HTTP range requests, enabling video seeking.
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


@app.route("/pricing")
def pricing():
    return render_template(
        "pricing.html",
        plans=PLANS,
        current_plan=get_plan(),
        current_user=current_user,
        publishable_key=STRIPE_PUBLISHABLE_KEY,
    )


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    if not current_user.is_authenticated:
        return redirect(url_for("login") + "?next=/pricing")
    plan_key = request.form.get("plan", "")
    if plan_key not in PLANS:
        return redirect("/pricing")
    plan = PLANS[plan_key]
    try:
        kwargs = dict(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": plan["amount"],
                    "recurring": {"interval": "month"},
                    "product_data": {
                        "name": f"ClipAI {plan['name']}",
                        "description": (
                            f"{plan['clips_per_day']} clips/day"
                            if plan["clips_per_day"] else "Unlimited clips/day"
                        ),
                    },
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url=(
                url_for("payment_success", _external=True)
                + "?session_id={CHECKOUT_SESSION_ID}&plan=" + plan_key
            ),
            cancel_url=url_for("pricing", _external=True),
        )
        if current_user.stripe_customer_id:
            kwargs["customer"] = current_user.stripe_customer_id
        else:
            kwargs["customer_email"] = current_user.email
        checkout = stripe.checkout.Session.create(**kwargs)
        return redirect(checkout.url, code=303)
    except stripe.StripeError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/payment/success")
def payment_success():
    session_id = request.args.get("session_id", "")
    plan_key   = request.args.get("plan", "")
    activated  = False
    if session_id and plan_key in PLANS:
        try:
            checkout = stripe.checkout.Session.retrieve(session_id)
            if checkout.payment_status == "paid":
                if current_user.is_authenticated:
                    current_user.plan = plan_key
                    current_user.stripe_subscription_id = checkout.subscription
                    current_user.stripe_customer_id = checkout.customer
                    current_user.clips_used_today = 0
                    db.session.commit()
                else:
                    flask_session["plan"] = plan_key
                    flask_session["subscription_id"] = checkout.subscription
                    flask_session["clip_count"] = 0
                activated = True
        except stripe.StripeError:
            pass
    return render_template(
        "success.html",
        plan=PLANS.get(plan_key, {}),
        plan_key=plan_key,
        activated=activated,
    )


@app.route("/payment/cancel")
def payment_cancel():
    return redirect(url_for("pricing"))


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    if not STRIPE_WEBHOOK_SECRET:
        return "", 400
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        return "", 400
    if event["type"] == "customer.subscription.deleted":
        sub  = event["data"]["object"]
        user = User.query.filter_by(stripe_subscription_id=sub["id"]).first()
        if user:
            user.plan = "free"
            user.stripe_subscription_id = None
            db.session.commit()
    return "", 200


@app.route("/api/plan")
def api_plan():
    limit = get_daily_limit()
    used  = get_daily_used()
    return jsonify({
        "plan": get_plan(),
        "limit": limit,
        "used": used,
        "remaining": (limit - used) if limit is not None else None,
    })


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect("/")
    error = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or not password:
            error = "Email and password are required."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif User.query.filter_by(email=email).first():
            error = "An account with this email already exists."
        else:
            user = User(email=email)
            user.set_password(password)
            # Carry over any plan purchased before signing up
            session_plan = flask_session.get("plan")
            if session_plan in PLANS:
                user.plan = session_plan
                user.stripe_subscription_id = flask_session.get("subscription_id")
            db.session.add(user)
            db.session.commit()
            login_user(user, remember=True)
            flask_session.clear()
            return redirect("/")
    return render_template("signup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect("/")
    error = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            error = "Invalid email or password."
        else:
            login_user(user, remember=True)
            return redirect(request.args.get("next") or "/")
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    logout_user()
    return redirect("/")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
