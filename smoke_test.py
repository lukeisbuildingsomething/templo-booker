"""End-to-end smoke test against a Postgres instance.

Run locally against a dev database, or automatically in CI (see
.github/workflows/ci.yml, which provisions a postgres:16 service). Exits
non-zero if any check fails.
"""
import os

os.environ["SESSION_SECRET"] = "test-secret"
os.environ["DATABASE_URL"] = "postgresql://postgres:test@localhost:55432/templo"
os.environ["SESSION_COOKIE_SECURE"] = "0"
os.environ["ADMIN_EMAILS"] = "admin@example.com"

import main  # noqa: E402  (init_db runs on import)

app = main.app
app.config["TESTING"] = True

failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")
    if not cond:
        failures.append(name)


c = app.test_client()

# Marketing + auth pages
check("home (marketing)", c.get("/").status_code == 200)
for path in ["/login", "/pricing", "/how-it-works", "/who-its-for", "/why-us", "/contact", "/request-account"]:
    check(f"GET {path}", c.get(path).status_code == 200)

# 404 handler
r = c.get("/definitely-not-a-page")
check("404 page", r.status_code == 404 and b"Page not found" in r.data)

# Log in by simulating magic-link flow directly
conn = main.get_db()
cur = conn.cursor()
token = main.generate_token()
cur.execute(
    "INSERT INTO magic_links (email, token, poll_id, expires_at) VALUES (%s, %s, NULL, NOW() + INTERVAL '24 hours')",
    ("admin@example.com", token),
)
conn.commit()
conn.close()
r = c.get(f"/magic/{token}", follow_redirects=False)
check("magic login redirects", r.status_code == 302)

r = c.get("/dashboard")
check("dashboard after login", r.status_code == 200)

# Create poll flow
r = c.post("/create", data={"poll_name": "Smoke Test Poll"}, follow_redirects=False)
check("create step1", r.status_code == 302 and "/calendar" in r.headers["Location"])
check("calendar page", c.get("/calendar").status_code == 200)

r = c.post("/finalize", json={"dates": ["2030-01-10", "2030-01-11"]})
check("finalize poll", r.status_code == 200, r.get_data(as_text=True))
poll_id = r.get_json()["poll_id"]

# Reject bad dates
c.post("/create", data={"poll_name": "Bad"}, follow_redirects=False)
r = c.post("/finalize", json={"dates": ["2030-1-1"]})
check("reject non-padded date", r.status_code == 400)
r = c.post("/finalize", json={"dates": ["2020-01-01"]})
check("reject past date", r.status_code == 400)

# View + vote
r = c.get(f"/poll/{poll_id}")
check("view poll", r.status_code == 200 and b"Smoke Test Poll" in r.data)
conn = main.get_db()
cur = conn.cursor()
cur.execute("SELECT id FROM dates WHERE poll_id = %s ORDER BY date", (poll_id,))
date_ids = [row[0] for row in cur.fetchall()]
conn.close()
r = c.post(f"/poll/{poll_id}/vote", json={"date_id": date_ids[0], "status": "maybe"})
check("vote", r.status_code == 200)

# Event time + close + ics + deeplinks
r = c.post(f"/poll/{poll_id}/event-time", json={"all_day": False, "start_time": "18:00", "end_time": "20:00", "timezone": "America/New_York"})
check("set event time", r.status_code == 200)
r = c.post(f"/poll/{poll_id}/close", json={"date_ids": [date_ids[0]]})
check("close poll", r.status_code == 200)
r = c.get(f"/poll/{poll_id}")
check("closed poll shows calendar view", r.status_code == 200 and b"calendar.google.com" in r.data)
r = c.get(f"/poll/{poll_id}/event/{date_ids[0]}.ics")
check("ics download", r.status_code == 200 and b"BEGIN:VCALENDAR" in r.data)
# 18:00 on 2030-01-10 in America/New_York (EST, UTC-5) must be emitted as
# 23:00 UTC — proves wall-clock times are converted to an absolute moment.
check("ics converts local time to UTC", b"DTSTART:20300110T230000Z" in r.data and b"DTEND:20300111T010000Z" in r.data,
      r.get_data(as_text=True))

# Share page + settings update
check("share page", c.get(f"/share/{poll_id}").status_code == 200)
r = c.post(f"/share/{poll_id}/update-emails", json={"emails": "friend@example.com", "name": "Renamed Poll", "access_mode": "invite_only"})
check("update share settings", r.status_code == 200)

# Free-limit: closed polls shouldn't count. Downgrade self to free & try create.
conn = main.get_db()
cur = conn.cursor()
cur.execute("UPDATE users SET role='user', tier='free' WHERE email='admin@example.com'")
conn.commit()
conn.close()
c.post("/create", data={"poll_name": "Second"}, follow_redirects=False)
r = c.post("/finalize", json={"dates": ["2030-02-01"]})
check("closed poll doesn't count toward free limit", r.status_code == 200, r.get_data(as_text=True))
r = c.post("/finalize", json={"dates": ["2030-03-01"]})  # no session; expect 400
check("second finalize without session fails", r.status_code == 400)

# Rate limit on magic links
c2 = app.test_client()
codes = []
for i in range(7):
    rr = c2.post("/login", data={"email": "admin@example.com"}, follow_redirects=True)
    codes.append(rr.status_code)
    body = rr.get_data(as_text=True)
check("rate limit message appears", "Too many sign-in links" in body)

# Admin panel (restore admin first)
conn = main.get_db()
cur = conn.cursor()
cur.execute("UPDATE users SET role='admin', tier='paid' WHERE email='admin@example.com'")
conn.commit()
conn.close()
check("admin panel", c.get("/admin").status_code == 200)

# Profile: reject javascript: picture URL
r = c.post("/profile", data={"display_name": "Admin", "email": "admin@example.com", "profile_picture": "javascript:alert(1)"}, follow_redirects=True)
check("reject javascript: picture", b"must be an http(s) URL" in r.data)

# Contact form: a genuine message (no timing token) flows through validation
# and the send path (which fails gracefully without an API key) → success.
r = c.post("/contact", data={"name": "Bob", "email": "bob@example.com", "subject": "hi", "message": "hello"}, follow_redirects=True)
check("contact form (genuine message delivered)", r.status_code == 200)
# Honeypot-filled submission is silently accepted (bot) — still 200, never errors.
r = c.post("/contact", data={"name": "Bot", "email": "bot@example.com", "message": "spam", "website": "http://spam.example"}, follow_redirects=True)
check("contact form (honeypot silently accepted)", r.status_code == 200)

# Account request: honeypot silently accepted; genuine request is recorded.
c3 = app.test_client()
r = c3.post("/request-account", data={"email": "newbie@example.com", "name": "Newbie", "reason": "please", "website": "x"}, follow_redirects=True)
check("request-account (honeypot silently accepted)", r.status_code == 200)
conn = main.get_db()
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM account_requests WHERE LOWER(email) = 'newbie@example.com'")
honeypot_count = cur.fetchone()[0]
conn.close()
check("request-account honeypot created no request row", honeypot_count == 0)
r = c3.post("/request-account", data={"email": "realuser@example.com", "name": "Real", "reason": "please"}, follow_redirects=True)
check("request-account (genuine request accepted)", r.status_code == 200)
conn = main.get_db()
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM account_requests WHERE LOWER(email) = 'realuser@example.com' AND status = 'pending'")
real_count = cur.fetchone()[0]
conn.close()
check("request-account genuine request recorded", real_count == 1)

# Delete poll
r = c.post(f"/poll/{poll_id}/delete", follow_redirects=False)
check("delete poll", r.status_code == 302)

print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
