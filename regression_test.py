"""Regression suite. Resets the throwaway Postgres database used by smoke_test.py."""
import os
import re
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

os.environ["SESSION_SECRET"] = "test-secret"
os.environ["DATABASE_URL"] = "postgresql://postgres:test@127.0.0.1:55432/templo"
os.environ["SESSION_COOKIE_SECURE"] = "0"
os.environ["ADMIN_EMAILS"] = "admin@example.com"
os.environ["PUBLIC_BASE_URL"] = "https://templo.example"

import main  # noqa: E402


def sql(query, params=()):
    conn = main.get_db()
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall() if cur.description else []
        conn.commit()
        return rows
    finally:
        conn.close()


class AppRegressionTests(unittest.TestCase):
    def setUp(self):
        main.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False,
                               PUBLIC_BASE_URL="https://templo.example")
        sql("TRUNCATE users, polls, dates, votes, magic_links, account_requests, verification_tokens, email_changes, contact_messages, app_settings RESTART IDENTITY CASCADE")
        sql("""INSERT INTO users (email, role, tier, is_verified, is_approved)
               VALUES ('admin@example.com', 'admin', 'paid', TRUE, TRUE),
                      ('owner@example.com', 'user', 'free', TRUE, TRUE)""")
        self.mail = self.enterContext(patch.object(main, "send_email", return_value=(True, None)))
        self.enterContext(patch.object(main, "check_mailersend_status", return_value={"ok": False, "configured": False, "message": "Not configured"}))
        self.owner = self.client("owner@example.com")
        self.admin = self.client("admin@example.com")

    def client(self, email=None, draft=None):
        client = main.app.test_client()
        if email:
            with client.session_transaction() as session:
                session["user_email"] = email
                if draft:
                    session["poll_name"] = draft
                    session["poll_creator_email"] = email
        return client

    def poll(self, poll_id="test-poll", owner="owner@example.com", closed=False, invites="", mode="public_link"):
        sql("""INSERT INTO polls (id, name, admin_email, closed_at, invite_emails, access_mode)
               VALUES (%s, %s, %s, CASE WHEN %s THEN NOW() ELSE NULL END, %s, %s)""",
            (poll_id, "Test Poll", owner, closed, invites, mode))
        ids = sql("INSERT INTO dates (poll_id, date, is_final) VALUES (%s, '2030-01-10', %s), (%s, '2030-01-11', FALSE) RETURNING id", (poll_id, closed, poll_id))
        for row in ids:
            sql("INSERT INTO votes (date_id, user_email, status) VALUES (%s, %s, 'yes')", (row[0], owner))
        return [row[0] for row in ids]

    def token_for(self, email):
        return sql("SELECT token FROM magic_links WHERE email = %s ORDER BY id DESC LIMIT 1", (email,))[0][0]

    def pending_token(self):
        return sql("SELECT token FROM email_changes")[0][0]

    def test_auth_and_approval_links_ignore_host_headers_with_csrf(self):
        main.app.config["WTF_CSRF_ENABLED"] = True
        client = self.client()
        base = "http://untrusted.example"
        headers = {"X-Forwarded-Host": "attacker.example", "X-Forwarded-Proto": "https"}
        html = client.get("/login", base_url=base, headers=headers).get_data(as_text=True)
        token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        response = client.post("/login", base_url=base, headers=headers,
                               data={"email": "owner@example.com", "csrf_token": token})
        self.assertEqual(response.status_code, 302)
        body = self.mail.call_args.args[2]
        self.assertIn("https://templo.example/magic/", body)
        self.assertNotIn("attacker.example", body)
        self.assertNotIn("untrusted.example", body)
        with main.app.test_request_context("/", headers=headers):
            main.send_admin_request_notification("new@example.com", "New", "Please", "approval-token")
        self.assertIn("https://templo.example/approve-request/approval-token", self.mail.call_args.args[2])

    def test_voter_needs_organizer_approval_but_can_vote_and_request_it(self):
        dates = self.poll()
        guest = self.client()
        guest.post("/poll/test-poll/access", data={"email": "guest@example.com"})
        guest.get("/magic/" + self.token_for("guest@example.com"))
        self.assertEqual(sql("SELECT is_verified, is_approved FROM users WHERE email='guest@example.com'"), [(True, False)])
        self.assertEqual(guest.post("/poll/test-poll/vote", json={"date_id": dates[0], "status": "yes"}).status_code, 200)
        for path in ("/create", "/calendar", "/upgrade"):
            self.assertEqual(guest.get(path).headers["Location"], "/request-account")
        self.assertEqual(guest.post("/create", data={"poll_name": "Not approved"}).headers["Location"], "/request-account")
        with guest.session_transaction() as session:
            session["poll_name"] = "Forged draft"
            session["poll_creator_email"] = "guest@example.com"
        self.assertEqual(guest.post("/finalize", json={"dates": ["2030-02-01"]}).status_code, 403)
        self.assertIn(b"Request Organizer Access", guest.get("/dashboard").data)
        self.assertEqual(guest.get("/request-account").status_code, 200)
        guest.post("/request-account", data={"email": "guest@example.com", "name": "Guest", "reason": "Organize"})
        request_id = sql("SELECT id FROM account_requests WHERE email='guest@example.com'")[0][0]
        self.admin.post(f"/admin/requests/{request_id}/approve")
        self.assertEqual(guest.post("/create", data={"poll_name": "Approved"}).headers["Location"], "/calendar")
        self.assertEqual(guest.post("/finalize", json={"dates": ["2030-02-01"]}).status_code, 200)

    def test_voter_can_sign_in_again_without_becoming_approved(self):
        sql("INSERT INTO users (email, is_verified) VALUES ('guest@example.com', TRUE)")
        client = self.client()
        client.post("/login", data={"email": "guest@example.com"})
        client.get("/magic/" + self.token_for("guest@example.com"))
        self.assertFalse(sql("SELECT is_approved FROM users WHERE email='guest@example.com'")[0][0])
        self.assertEqual(client.get("/dashboard").status_code, 200)

    def test_email_and_admin_approval_grant_organizer_access(self):
        sql("INSERT INTO account_requests (email, approval_token) VALUES ('new@example.com', 'approve-me')")
        self.client().get("/approve-request/approve-me")
        self.assertTrue(sql("SELECT is_approved FROM users WHERE email='new@example.com'")[0][0])
        sql("INSERT INTO users (email) VALUES ('voter@example.com')")
        self.admin.post("/admin/users/voter@example.com/update", data={"role": "user", "tier": "free", "is_approved": "on"})
        self.assertTrue(sql("SELECT is_approved FROM users WHERE email='voter@example.com'")[0][0])
        self.admin.post("/admin/users/voter@example.com/update", data={"role": "user", "tier": "free"})
        self.assertFalse(sql("SELECT is_approved FROM users WHERE email='voter@example.com'")[0][0])

    def test_approval_migration_preserves_evidence_not_email_verification(self):
        self.poll()
        sql("INSERT INTO users (email, is_verified) VALUES ('voter@example.com', TRUE), ('approved@example.com', TRUE), ('clockwerks77@gmail.com', FALSE)")
        sql("INSERT INTO account_requests (email, status) VALUES ('approved@example.com', 'approved')")
        sql("ALTER TABLE users DROP COLUMN is_approved")
        main.init_db()
        flags = dict(sql("SELECT email, is_approved FROM users"))
        self.assertTrue(flags["owner@example.com"])
        self.assertTrue(flags["admin@example.com"])
        self.assertTrue(flags["approved@example.com"])
        self.assertTrue(flags["clockwerks77@gmail.com"])
        self.assertFalse(flags["voter@example.com"])
        sql("UPDATE users SET is_approved=FALSE WHERE email='owner@example.com'")
        main.init_db()
        self.assertFalse(sql("SELECT is_approved FROM users WHERE email='owner@example.com'")[0][0])

    def test_event_time_returns_updated_calendar_links_for_every_final_date(self):
        self.poll(closed=True)
        sql("UPDATE dates SET is_final=TRUE")
        response = self.owner.post("/poll/test-poll/event-time", json={"all_day": False, "start_time": "18:00", "end_time": "20:00", "timezone": "America/Toronto"})
        events = response.json["calendar_events"]
        self.assertEqual(len(events), 2)
        for event in events:
            google = parse_qs(urlsplit(event["google_url"]).query)
            outlook = parse_qs(urlsplit(event["outlook_url"]).query)
            self.assertIn("T180000/", google["dates"][0])
            self.assertEqual(google["ctz"], ["America/Toronto"])
            self.assertIn("T18:00:00-05:00", outlook["startdt"][0])
            self.assertNotIn("allday", outlook)
        response = self.owner.post("/poll/test-poll/event-time", json={"all_day": True})
        for event in response.json["calendar_events"]:
            self.assertEqual(parse_qs(urlsplit(event["outlook_url"]).query)["allday"], ["true"])

    def test_vote_response_recomputes_rankings_filters_and_participants(self):
        dates = self.poll()
        response = self.owner.post("/poll/test-poll/vote", json={"date_id": dates[0], "status": "no"})
        rows = response.json["date_rows_html"]
        first = re.search(r'data-date-id="' + str(dates[0]) + r'"[^>]+', rows).group(0)
        second = re.search(r'data-date-id="' + str(dates[1]) + r'"[^>]+', rows).group(0)
        self.assertIn('data-votes="0"', first)
        self.assertIn('data-is-best="false"', first)
        self.assertIn('data-is-possible="false"', first)
        self.assertIn('data-is-best="true"', second)
        self.assertIn('title="owner: no"', rows)
        sql("INSERT INTO users (email, display_name) VALUES ('new-voter@example.com', '<New Voter>')")
        response = self.client("new-voter@example.com").post("/poll/test-poll/vote", json={"date_id": dates[1], "status": "yes"})
        self.assertIn("&lt;New Voter&gt;", response.json["participants_html"])
        self.assertNotIn("<New Voter>", response.json["date_rows_html"])
        self.assertIn("new-voter", self.owner.get("/share/test-poll").get_data(as_text=True))

    def test_contact_delivery_failure_is_persisted_and_visible_to_admin(self):
        self.mail.return_value = (False, "provider unavailable")
        response = self.client().post("/contact", data={"name": "Customer", "email": "customer@example.com", "subject": "Need help", "message": "My saved message"}, follow_redirects=True)
        self.assertIn(b"Your message has been saved", response.data)
        self.assertNotIn(b"Message sent!", response.data)
        self.assertEqual(sql("SELECT message, delivered_at FROM contact_messages"), [("My saved message", None)])
        self.assertIn(b"My saved message", self.admin.get("/admin").data)

    def test_contact_success_and_honeypot_delivery(self):
        client = self.client()
        response = client.post("/contact", data={"name": "Customer", "email": "customer@example.com", "message": "Hello"}, follow_redirects=True)
        self.assertIn(b"Message sent!", response.data)
        self.assertIsNotNone(sql("SELECT delivered_at FROM contact_messages")[0][0])
        calls = self.mail.call_count
        client.post("/contact", data={"name": "Bot", "email": "bot@example.com", "message": "Spam", "website": "spam"})
        self.assertEqual(sql("SELECT COUNT(*) FROM contact_messages"), [(1,)])
        self.assertEqual(self.mail.call_count, calls)

    def test_email_change_requires_confirmation_and_preserves_account_data(self):
        dates = self.poll()
        self.poll("invited-poll", owner="admin@example.com", mode="invite_only", invites="owner@example.com\nfriend@example.com")
        old_session = self.client("owner@example.com")
        sql("INSERT INTO magic_links (email, token, expires_at) VALUES ('owner@example.com','old-link',NOW()+INTERVAL '1 day')")
        response = self.owner.post("/profile", data={"email": "brand-new@example.com", "display_name": "Owner"}, follow_redirects=True)
        self.assertIn(b"Confirmation pending for brand-new@example.com", response.data)
        self.assertEqual(sql("SELECT email FROM users WHERE email='owner@example.com'"), [("owner@example.com",)])
        self.assertEqual(sql("SELECT admin_email FROM polls WHERE id='test-poll'"), [("owner@example.com",)])
        self.assertIn("https://templo.example/confirm-email/", self.mail.call_args.args[2])
        token = self.pending_token()
        self.owner.get("/confirm-email/" + token)
        self.assertEqual(sql("SELECT role, tier, is_verified, is_approved FROM users WHERE email='brand-new@example.com'"), [("user", "free", True, True)])
        self.assertEqual(sql("SELECT admin_email FROM polls WHERE id='test-poll'"), [("brand-new@example.com",)])
        self.assertEqual(sql("SELECT DISTINCT user_email FROM votes WHERE date_id=ANY(%s)", (dates,)), [("brand-new@example.com",)])
        self.assertEqual(sql("SELECT invite_emails FROM polls WHERE id='invited-poll'"), [("brand-new@example.com\nfriend@example.com",)])
        self.assertEqual(sql("SELECT COUNT(*) FROM email_changes"), [(0,)])
        self.assertEqual(old_session.get("/profile").headers["Location"], "/login")
        self.client().get("/magic/old-link")
        self.assertEqual(sql("SELECT COUNT(*) FROM users WHERE email='owner@example.com'"), [(0,)])
        self.assertEqual(self.owner.get("/confirm-email/" + token).status_code, 302)

    def test_failed_email_change_delivery_keeps_old_address_and_session(self):
        self.mail.return_value = (False, "provider unavailable")
        response = self.owner.post("/profile", data={"email": "new@example.com"}, follow_redirects=True)
        self.assertIn(b"Your email address is unchanged", response.data)
        self.assertEqual(sql("SELECT email FROM users WHERE email='owner@example.com'"), [("owner@example.com",)])
        self.assertEqual(sql("SELECT COUNT(*) FROM email_changes"), [(0,)])
        self.assertEqual(self.owner.get("/dashboard").status_code, 200)

    def test_email_change_rejects_taken_expired_and_replaced_links(self):
        response = self.owner.post("/profile", data={"email": "admin@example.com"}, follow_redirects=True)
        self.assertIn(b"already in use", response.data)
        self.owner.post("/profile", data={"email": "new@example.com"})
        old_token = self.pending_token()
        self.owner.post("/profile", data={"email": "latest@example.com"})
        self.owner.get("/confirm-email/" + old_token)
        self.assertEqual(sql("SELECT email FROM users WHERE email='owner@example.com'"), [("owner@example.com",)])
        token = self.pending_token()
        sql("UPDATE email_changes SET expires_at=NOW()-INTERVAL '1 minute'")
        self.owner.get("/confirm-email/" + token)
        self.assertEqual(sql("SELECT email FROM users WHERE email='owner@example.com'"), [("owner@example.com",)])
        self.owner.post("/profile", data={"email": "taken-later@example.com"})
        token = self.pending_token()
        sql("INSERT INTO users (email) VALUES ('taken-later@example.com')")
        response = self.owner.get("/confirm-email/" + token, follow_redirects=True)
        self.assertIn(b"already in use", response.data)

    def test_reopening_cannot_exceed_free_quota_or_clear_final_dates(self):
        self.poll("closed-poll", closed=True)
        self.poll("open-poll")
        response = self.owner.post("/poll/closed-poll/reopen", follow_redirects=True)
        self.assertIn(b"Free tier allows 1 active poll", response.data)
        self.assertIsNotNone(sql("SELECT closed_at FROM polls WHERE id='closed-poll'")[0][0])
        self.assertTrue(sql("SELECT is_final FROM dates WHERE poll_id='closed-poll' ORDER BY id")[0][0])
        self.assertEqual(main.get_owned_poll_count("owner@example.com"), 1)
        sql("UPDATE polls SET closed_at=NOW() WHERE id='open-poll'")
        self.owner.post("/poll/closed-poll/reopen")
        self.assertIsNone(sql("SELECT closed_at FROM polls WHERE id='closed-poll'")[0][0])

    def test_concurrent_create_and_reopen_share_one_quota_lock(self):
        self.poll("closed-poll", closed=True)
        barrier = threading.Barrier(2)
        original = main.active_poll_limit_reached

        def synchronized_check(cursor, email):
            barrier.wait(timeout=10)
            return original(cursor, email)

        def create():
            return self.client("owner@example.com", draft="New poll").post("/finalize", json={"dates": ["2030-02-01"]}).status_code

        def reopen():
            return self.client("owner@example.com").post("/poll/closed-poll/reopen").status_code

        with patch.object(main, "active_poll_limit_reached", side_effect=synchronized_check):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(create), pool.submit(reopen)]
                statuses = [future.result(timeout=15) for future in futures]
        self.assertIn(statuses[0], [200, 400])
        self.assertEqual(statuses[1], 302)
        self.assertEqual(main.get_owned_poll_count("owner@example.com"), 1)

    def test_paid_organizer_can_reopen_with_another_active_poll(self):
        self.poll("closed-poll", owner="admin@example.com", closed=True)
        self.poll("open-poll", owner="admin@example.com")
        self.admin.post("/poll/closed-poll/reopen")
        self.assertEqual(main.get_owned_poll_count("admin@example.com"), 2)

    def test_custom_slug_can_be_omitted_or_explicitly_cleared(self):
        self.poll(owner="admin@example.com")
        self.admin.post("/share/test-poll/update-emails", json={"emails": "", "slug": "summer-party"})
        self.admin.post("/share/test-poll/update-emails", json={"emails": "", "name": "Renamed"})
        self.assertEqual(sql("SELECT slug FROM polls WHERE id='test-poll'"), [("summer-party",)])
        response = self.admin.post("/share/test-poll/update-emails", json={"emails": "", "slug": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sql("SELECT slug FROM polls WHERE id='test-poll'"), [(None,)])
        self.assertNotIn(b"/p/summer-party", self.admin.get("/share/test-poll").data)
        self.poll("free-poll")
        self.assertEqual(self.owner.post("/share/free-poll/update-emails", json={"emails": "", "slug": "free-slug"}).status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
