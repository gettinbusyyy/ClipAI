"""
One-off diagnostic + password-reset script for ClipAI.
Usage: python reset_password.py
"""
import os
import sqlite3
from werkzeug.security import generate_password_hash

TARGET_EMAIL = "jamessanchezd@yahoo.com"
NEW_PASSWORD = "Test1234!"

# ── same BASE_DIR logic as app.py line 19 ────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "clipai.db")

print(f"DB path : {DB_PATH}")
print(f"DB exists: {os.path.exists(DB_PATH)}\n")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# ── auto-detect user table name ───────────────────────────────────────────────
cur.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
)
tables = [r["name"] for r in cur.fetchall()]
print(f"Tables found: {tables}\n")

USER_TABLE = None
for candidate in ("user", "users", "account", "accounts"):
    if candidate in tables:
        USER_TABLE = candidate
        break

if USER_TABLE is None:
    print("ERROR: could not find a user table. Exiting.")
    conn.close()
    raise SystemExit(1)

print(f"Using table: {USER_TABLE}\n")

# ── print every stored email so we can see exactly what's there ───────────────
cur.execute(f"SELECT id, email FROM \"{USER_TABLE}\" ORDER BY id")
rows = cur.fetchall()
print(f"All emails in '{USER_TABLE}' ({len(rows)} row(s)):")
for r in rows:
    print(f"  id={r['id']}  email={repr(r['email'])}")
print()

# ── case-insensitive, trimmed match ──────────────────────────────────────────
cur.execute(
    f"SELECT id, email FROM \"{USER_TABLE}\" "
    "WHERE LOWER(TRIM(email)) = LOWER(TRIM(?))",
    (TARGET_EMAIL,),
)
match = cur.fetchone()

if not match:
    print(f"No match for {TARGET_EMAIL!r} (case-insensitive). Check the list above.")
    conn.close()
    raise SystemExit(1)

new_hash = generate_password_hash(NEW_PASSWORD)
cur.execute(
    f"UPDATE \"{USER_TABLE}\" SET password_hash = ? WHERE id = ?",
    (new_hash, match["id"]),
)
conn.commit()
conn.close()

print(f"SUCCESS — password reset for id={match['id']} email={match['email']!r}")
print(f"You can now log in as {match['email']} with the new password.")
