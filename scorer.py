import os
import json
import re
import anthropic
from dotenv import load_dotenv

load_dotenv()

TRANSCRIPT_FILE = "transcript.txt"
CLIPS_FILE = "clips.json"
MODEL = "claude-sonnet-4-5"

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

def load_transcript(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def select_niche() -> str:
    print("\nSelect a niche for clip scoring:")
    print("-" * 35)
    for key, name in NICHES.items():
        print(f"  {key:>2}. {name}")
    print("-" * 35)
    while True:
        choice = input("Enter number (1-10): ").strip()
        if choice in NICHES:
            return NICHES[choice]
        print("Invalid choice, try again.")

def score_clips(transcript: str, niche: str, count: int = 3) -> list:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = f"""You are a viral content expert for {niche} content on TikTok and YouTube Shorts.

Analyze this transcript and identify the TOP {count} most viral-worthy clip segments.

For each clip return a JSON object with:
- start_time: start timestamp in milliseconds
- end_time: end timestamp in milliseconds
- title: punchy clip title (max 8 words)
- hook: the opening hook line
- score: virality score 1-100
- reason: why this clip will go viral

Return ONLY a JSON array with exactly {count} objects, no other text.

TRANSCRIPT:
{transcript}"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=max(1024, count * 250),
        messages=[{"role": "user", "content": prompt}]
    )
    
    response_text = message.content[0].text
    clean = response_text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"```json|```", "", clean).strip()
    
    clips = json.loads(clean)
    return clips

def main():
    if not os.path.exists(TRANSCRIPT_FILE):
        print(f"Error: {TRANSCRIPT_FILE} not found. Run transcribe.py first.")
        return
    
    transcript = load_transcript(TRANSCRIPT_FILE)
    if not transcript.strip():
        print("Error: transcript.txt is empty.")
        return
    
    niche = select_niche()
    print(f"\nScoring clips for niche: {niche}")
    print("Sending to Claude for analysis...")
    
    clips = score_clips(transcript, niche)

    with open(CLIPS_FILE, "w") as f:
        json.dump(clips, f, indent=2)

    print(f"\nTop {len(clips)} clips saved to {CLIPS_FILE}:")
    for i, clip in enumerate(clips, 1):
        print(f"\n#{i}: {clip.get('title', 'N/A')}")
        print(f"  Score: {clip.get('score', 'N/A')}/100")
        print(f"  Hook: {clip.get('hook', 'N/A')}")
        print(f"  Why viral: {clip.get('reason', 'N/A')}")

if __name__ == "__main__":
    main()
