"""
One-off script: reset a user's password directly in clipai.db.
Usage: python reset_password.py
"""
import os
import sqlite3
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clipai.db")
EMAIL    = "jamessanchezd@yahoo.com"
NEW_PASSWORD = "415487.Cba"   # ← set your new password here

conn = sqlite3.connect(DB_PATH)
cur  = conn.cursor()

cur.execute("SELECT id, email FROM user WHERE email = ?", (EMAIL,))
row = cur.fetchone()
if not row:
    print(f"No user found with email: {EMAIL}")
    conn.close()
    raise SystemExit(1)

new_hash = generate_password_hash(NEW_PASSWORD)
cur.execute("UPDATE user SET password_hash = ? WHERE id = ?", (new_hash, row[0]))
conn.commit()
conn.close()

print(f"Password reset for {row[1]} (id={row[0]}).")
