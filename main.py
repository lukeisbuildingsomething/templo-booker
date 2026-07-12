import base64
import os
import random
import re
import string
import secrets
from pathlib import Path
import psycopg2
import requests
from dotenv import load_dotenv
from markupsafe import escape
from psycopg2.extras import RealDictCursor
from datetime import date as _date, datetime, timedelta, timezone
from flask import Flask, g, has_app_context, render_template, request, redirect, url_for, session, jsonify, flash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET")
if not app.secret_key:
    raise RuntimeError("SESSION_SECRET environment variable is required")
app.permanent_session_lifetime = timedelta(days=30)

# Railway terminates TLS at its proxy; trust X-Forwarded-* so request.url_root
# and generated links (magic-link emails) use https and the public host.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1").lower() in ("1", "true", "yes"),
)

DATABASE_URL = os.environ.get("DATABASE_URL")

MAILERSEND_API_URL = "https://api.mailersend.com/v1/email"
MAILERSEND_DOMAINS_URL = "https://api.mailersend.com/v1/domains"

DEFAULT_MAIL_FROM_EMAIL = "noreply@templobooker.com"
DEFAULT_MAIL_FROM_NAME = "Templo"


def get_setting(key, default=None):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0] is not None:
            return row[0]
    except Exception as e:
        print(f"ERROR: get_setting({key}) failed: {e}")
    return default


def set_setting(key, value):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        '''INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, NOW())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()''',
        (key, value)
    )
    conn.commit()
    conn.close()


def get_mail_from():
    name = get_setting("mail_from_name") or DEFAULT_MAIL_FROM_NAME
    email = get_setting("mail_from_email") or DEFAULT_MAIL_FROM_EMAIL
    return name, email


def send_email(to_email, subject, html_body, reply_to=None, attachments=None):
    api_key = get_setting("mailersend_api_key")
    if not api_key:
        msg = "MailerSend API key not configured (set it in Admin → Email)"
        print(f"ERROR: {msg}")
        return False, msg

    from_name, from_email = get_mail_from()
    payload = {
        "from": {"email": from_email, "name": from_name},
        "to": [{"email": to_email}],
        "subject": subject,
        "html": html_body,
    }
    if reply_to:
        payload["reply_to"] = {"email": reply_to}
    if attachments:
        payload["attachments"] = attachments

    try:
        response = requests.post(
            MAILERSEND_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=10,
        )
        if response.status_code in (200, 202):
            return True, None
        error_summary = f"MailerSend rejected send (status={response.status_code}): {response.text[:300]}"
        print(f"ERROR: {error_summary}")
        return False, error_summary
    except requests.Timeout as e:
        print(f"ERROR: MailerSend request timed out: {e}")
        return False, f"Request to MailerSend timed out: {e}"
    except Exception as e:
        print(f"ERROR: Failed to send email via MailerSend: {e}")
        return False, str(e)


def classify_email_error(error):
    """Map a send_email() error string to (code, user_message).

    Codes are stable identifiers a user can quote to support; messages are
    safe to show end users (no API tokens, no full provider response bodies).
    """
    if not error:
        return "E000", "Unknown email error."
    err = error.lower()
    if "api key not configured" in err:
        return "E001", "Email service is not configured yet. Please contact the site admin."
    if "status=401" in err or "invalid api key" in err:
        return "E002", "Email service authentication failed. Please contact the site admin."
    if "ms42225" in err or "trial account unique recipients" in err:
        return "E003", "Email service trial limit reached. Please contact the site admin."
    if "ms42207" in err or "recipient" in err and "block" in err:
        return "E004", "This address is on the email service's block list. Try a different address or contact the site admin."
    if "status=422" in err:
        return "E005", "Email service rejected this address. Double-check it and try again."
    if "status=429" in err:
        return "E006", "Email service is rate-limiting us. Please try again in a minute."
    if "timed out" in err or "timeout" in err:
        return "E007", "Email service timed out. Please try again."
    if re.search(r"status=5\d\d", err):
        return "E008", "Email service is temporarily unavailable. Please try again in a few minutes."
    return "E099", "Email service returned an unexpected error."


def email_error_flash(error, fallback_action="Please try again in a minute."):
    code, msg = classify_email_error(error)
    return f"We couldn't send your sign-in email ({code}). {msg} {fallback_action}".strip()


def _ics_escape(value):
    if value is None:
        return ""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _parse_hhmm(value):
    if not value:
        return None
    m = re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", value.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def build_calendar_deeplinks(poll, date_row, attendees):
    """Return {google_url, outlook_url} for a confirmed date, prefilled with attendees.

    The deeplink opens the organizer's calendar with the event ready to save —
    after they click Save, the calendar provider sends the invite natively
    (so there's no email-auth/spoof issue and RSVPs route to the organizer).
    """
    from urllib.parse import urlencode

    title = poll.get("name") or "Confirmed event"
    description = f"Confirmed via Templo. {title}"
    attendee_csv = ",".join(a for a in attendees if a)

    parts = [int(p) for p in date_row["date"].split("-")]
    day = _date(parts[0], parts[1], parts[2])
    start_hhmm = _parse_hhmm(poll.get("event_start_time"))
    end_hhmm = _parse_hhmm(poll.get("event_end_time"))

    if start_hhmm and end_hhmm:
        sh, sm = start_hhmm
        eh, em = end_hhmm
        if (eh, em) <= (sh, sm):
            end_day = day + timedelta(days=1)
        else:
            end_day = day
        google_dates = (
            f"{day.strftime('%Y%m%d')}T{sh:02d}{sm:02d}00/"
            f"{end_day.strftime('%Y%m%d')}T{eh:02d}{em:02d}00"
        )
        outlook_params = {
            "path": "/calendar/action/compose",
            "rru": "addevent",
            "subject": title,
            "body": description,
            "startdt": f"{day.strftime('%Y-%m-%d')}T{sh:02d}:{sm:02d}:00",
            "enddt": f"{end_day.strftime('%Y-%m-%d')}T{eh:02d}:{em:02d}:00",
            "to": attendee_csv,
        }
    else:
        end_day = day + timedelta(days=1)
        google_dates = f"{day.strftime('%Y%m%d')}/{end_day.strftime('%Y%m%d')}"
        outlook_params = {
            "path": "/calendar/action/compose",
            "rru": "addevent",
            "subject": title,
            "body": description,
            "startdt": day.strftime("%Y-%m-%d"),
            "enddt": end_day.strftime("%Y-%m-%d"),
            "allday": "true",
            "to": attendee_csv,
        }

    google_params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": google_dates,
        "details": description,
        "add": attendee_csv,
    }

    return {
        "google_url": "https://calendar.google.com/calendar/render?" + urlencode(google_params),
        "outlook_url": "https://outlook.office.com/calendar/0/deeplink/compose?" + urlencode(outlook_params),
    }


def event_time_label(poll, date_row):
    """Human-readable time label, e.g. '2026-05-15 (all day)' or '2026-05-15, 14:00–15:30'."""
    date_str = date_row.get("date") or ""
    start = poll.get("event_start_time") if poll else None
    end = poll.get("event_end_time") if poll else None
    if start and end:
        return f"{date_str}, {start}–{end}"
    return f"{date_str} (all day)"


def generate_ics(poll, date_row, attendees, organizer_email, organizer_name, sent_by_email=None):
    """Build an RFC 5545 VCALENDAR string for a date row.

    Times are sourced from poll.event_start_time / poll.event_end_time
    (HH:MM). When both are set, emits a timed floating-time event;
    otherwise an all-day event.

    When ``sent_by_email`` is given and differs from organizer_email
    (the email-invites path), the calendar ORGANIZER is set to the
    actual sending address — this is the only thing Gmail consistently
    accepts; SENT-BY alone gets rejected as spoofed for personal Gmail
    organizers. The poll creator stays in the ATTENDEE list and is
    surfaced via DESCRIPTION + the email's Reply-To header.
    """
    parts = [int(p) for p in date_row["date"].split("-")]
    day = _date(parts[0], parts[1], parts[2])
    start_hhmm = _parse_hhmm(poll.get("event_start_time"))
    end_hhmm = _parse_hhmm(poll.get("event_end_time"))
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = f"{poll['id']}-{date_row['id']}@templobooker.com"

    if start_hhmm and end_hhmm:
        sh, sm = start_hhmm
        eh, em = end_hhmm
        dtstart = f"{day.strftime('%Y%m%d')}T{sh:02d}{sm:02d}00"
        # If end time is earlier or equal, treat as next-day end
        if (eh, em) <= (sh, sm):
            end_day = day + timedelta(days=1)
            dtend = f"{end_day.strftime('%Y%m%d')}T{eh:02d}{em:02d}00"
        else:
            dtend = f"{day.strftime('%Y%m%d')}T{eh:02d}{em:02d}00"
        dt_lines = [f"DTSTART:{dtstart}", f"DTEND:{dtend}"]
    else:
        end_day = day + timedelta(days=1)
        dt_lines = [
            f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{end_day.strftime('%Y%m%d')}",
        ]

    is_delegated_invite = bool(
        sent_by_email and normalize_email(sent_by_email) != normalize_email(organizer_email)
    )
    if is_delegated_invite:
        cal_organizer_email = sent_by_email
        cal_organizer_name = "Templo"
        description_text = (
            f"Confirmed by {organizer_name or organizer_email} via Templo. "
            f"{poll.get('name') or ''}"
        )
    else:
        cal_organizer_email = organizer_email
        cal_organizer_name = organizer_name or organizer_email
        description_text = "Confirmed via Templo. " + (poll.get('name') or '')

    organizer_line = (
        f"ORGANIZER;CN={_ics_escape(cal_organizer_name)}:mailto:{cal_organizer_email}"
    )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Templo//Poll//EN",
        "METHOD:REQUEST",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"SEQUENCE:0",
        f"STATUS:CONFIRMED",
        f"TRANSP:OPAQUE",
        *dt_lines,
        f"SUMMARY:{_ics_escape(poll.get('name'))}",
        f"DESCRIPTION:{_ics_escape(description_text)}",
        organizer_line,
    ]
    for attendee in attendees:
        if not attendee:
            continue
        cn = _ics_escape(get_name(attendee) or attendee)
        lines.append(
            f"ATTENDEE;CN={cn};RSVP=TRUE;PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT:mailto:{attendee}"
        )
    lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def build_ai_calendar_prompt(poll, final_dates, attendees, organizer_email):
    """Markdown the organizer can paste into a chatbot with calendar tools."""
    lines = [
        f"# Calendar invites for: {poll.get('name')}",
        "",
        "Please create the following event(s) on my calendar and send invites to every attendee listed.",
        "",
        f"**Organizer:** {organizer_email}",
        "",
        "**Attendees:**",
    ]
    for attendee in attendees:
        if attendee:
            lines.append(f"- {attendee}")
    lines.append("")
    lines.append("**Events:**")
    for d in final_dates:
        lines.append(f"- {event_time_label(poll, d)} — *{poll.get('name')}*")
    lines.append("")
    lines.append("Times are in the organizer's local timezone. Use my default calendar and send invitations so each attendee can RSVP.")
    return "\n".join(lines)


def check_mailersend_status(api_key=None):
    api_key = api_key or get_setting("mailersend_api_key")
    if not api_key:
        return {"ok": False, "configured": False, "message": "API key not set"}
    try:
        response = requests.get(
            MAILERSEND_DOMAINS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=10,
        )
        if response.status_code == 200:
            try:
                data = response.json()
                domains = data.get("data", [])
                verified = [d for d in domains if d.get("domain_settings", {}).get("verification_approved") or d.get("is_verified")]
                return {
                    "ok": True,
                    "configured": True,
                    "message": f"Connected. {len(verified)} verified domain(s) of {len(domains)}.",
                    "domain_count": len(domains),
                    "verified_count": len(verified),
                }
            except ValueError:
                return {"ok": True, "configured": True, "message": "Connected."}
        if response.status_code == 401:
            return {"ok": False, "configured": True, "message": "Invalid API key (401)."}
        return {"ok": False, "configured": True, "message": f"MailerSend returned {response.status_code}: {response.text[:200]}"}
    except Exception as e:
        return {"ok": False, "configured": True, "message": f"Connection error: {e}"}

UPLOAD_DIR = Path(app.static_folder) / "uploads" / "profile-photos"
UPLOAD_URL_PREFIX = "uploads/profile-photos"
MAX_UPLOAD_SIZE_BYTES = 5 * 1024 * 1024
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EMAILS = [
    "luke.david.reimer@gmail.com",
    "forster.graham@gmail.com",
    "clockwerks77@gmail.com",
    "gavyn.mcleod@gmail.com"
]

EMAIL_TO_NAME = {
    "clockwerks77@gmail.com": "Adam",
    "luke.david.reimer@gmail.com": "Luke",
    "forster.graham@gmail.com": "Graham",
    "gavyn.mcleod@gmail.com": "Gavyn"
}

FREE_POLL_LIMIT = 1
FREE_DATE_LIMIT = 15
VALID_ROLES = {"user", "admin"}
VALID_TIERS = {"free", "paid"}
ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.environ.get("ADMIN_EMAILS", "luke.david.reimer@gmail.com").split(",")
    if email.strip()
}


def normalize_email(email):
    return (email or "").strip().lower()


def parse_invite_emails(raw_emails):
    if not raw_emails:
        return []

    invites = set()
    for entry in raw_emails.replace(",", "\n").splitlines():
        email = normalize_email(entry)
        if email and "@" in email:
            invites.add(email)
    return sorted(invites)


def serialize_invite_emails(emails):
    normalized = {
        normalize_email(email)
        for email in emails
        if normalize_email(email) and "@" in normalize_email(email)
    }
    return "\n".join(sorted(normalized))


def default_role_for_email(email):
    return "admin" if normalize_email(email) in ADMIN_EMAILS else "user"


def default_tier_for_email(email):
    return "paid" if default_role_for_email(email) == "admin" else "free"


def is_valid_date_string(value):
    # Strict zero-padded YYYY-MM-DD so lexicographic date comparisons are safe
    # (strptime alone accepts e.g. "2026-1-1").
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def get_name(email):
    if email:
        normalized_email = normalize_email(email)
        # First check if user has a custom display_name in database
        try:
            conn = get_db()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT display_name FROM users WHERE email = %s", (normalized_email,))
            user = cursor.fetchone()
            conn.close()
            if user and user.get("display_name"):
                return user["display_name"]
        except:
            pass
        return EMAIL_TO_NAME.get(normalized_email, normalized_email.split('@')[0])
    return ""


def get_user_profile(email):
    """Get full user profile including profile picture"""
    if not email:
        return None
    try:
        normalized_email = normalize_email(email)
        conn = get_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT email, display_name, profile_picture, role, tier, is_verified FROM users WHERE email = %s",
            (normalized_email,)
        )
        user = cursor.fetchone()
        conn.close()
        return user
    except:
        return None


def is_allowed_email(email):
    normalized = normalize_email(email)
    # Admin emails are always allowed
    if normalized in ADMIN_EMAILS:
        return True
    # Legacy allowlist
    if normalized in [normalize_email(e) for e in ALLOWED_EMAILS]:
        return True
    # Check if user exists in database (approved via account request flow)
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE LOWER(email) = %s", (normalized,))
        user_exists = cursor.fetchone() is not None
        conn.close()
        return user_exists
    except:
        return False


def generate_short_id(length=5):
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choice(chars) for _ in range(length))


def generate_token():
    return secrets.token_urlsafe(32)


MAGIC_LINKS_PER_HOUR = 5


def magic_link_rate_limited(cursor, email):
    """True if this email has already been sent too many sign-in links recently.

    Guards the public login/poll-access forms against being used to spam
    someone's inbox. Also opportunistically prunes long-expired tokens.
    """
    cursor.execute("DELETE FROM magic_links WHERE expires_at < NOW() - INTERVAL '7 days'")
    cursor.execute(
        "SELECT COUNT(*) FROM magic_links WHERE LOWER(email) = %s AND created_at > NOW() - INTERVAL '1 hour'",
        (normalize_email(email),)
    )
    row = cursor.fetchone()
    count = row[0] if not isinstance(row, dict) else row["count"]
    return count >= MAGIC_LINKS_PER_HOUR


def send_admin_request_notification(requester_email, requester_name, reason, approval_token, request_url_root):
    approve_url = request_url_root.rstrip("/") + url_for("approve_request_via_email", token=approval_token)
    admin_url = request_url_root.rstrip("/") + url_for("admin_panel")
    display_name = requester_name or requester_email.split("@")[0]
    reason_html = f"<p><strong>Reason:</strong> {escape(reason)}</p>" if reason else ""

    html_body = f"""
        <h2>New Account Request</h2>
        <p><strong>Email:</strong> {escape(requester_email)}</p>
        <p><strong>Name:</strong> {escape(display_name)}</p>
        {reason_html}
        <p style="margin-top: 20px;">
            <a href="{approve_url}" style="background-color: #16a34a; color: white; padding: 12px 24px; text-decoration: none; border-radius: 8px; display: inline-block; font-weight: bold;">Approve Request</a>
        </p>
        <p style="margin-top: 12px;">Or manage all requests in the <a href="{admin_url}">Admin Panel</a>.</p>
        <p style="color: #6b7280; font-size: 14px; margin-top: 20px;">Templo: templobooker.com</p>
    """

    for admin_email in ADMIN_EMAILS:
        sent, error = send_email(admin_email, f"New account request from {display_name}", html_body)
        if not sent:
            print(f"ERROR: Failed to notify admin {admin_email}: {error}")

    return True


def send_magic_link_email(to_email, token, poll, request_url_root):
    magic_url = request_url_root.rstrip("/") + url_for("magic_login", token=token)

    if poll:
        poll_name = poll.get("name") or "your poll"
        inviter_name = get_name(poll.get("admin_email"))
        inviter_line = (
            f"<p>{escape(inviter_name)} invited you to vote on dates for <strong>{escape(poll_name)}</strong>.</p>"
            if inviter_name
            else f"<p>You've been invited to vote on dates for <strong>{escape(poll_name)}</strong>.</p>"
        )
        cta = "Open Poll"
        subject = f"Your sign-in link for {poll_name}"
    else:
        inviter_line = "<p>An admin sent you a one-click sign-in link for Templo. No password needed.</p>"
        cta = "Sign in to Templo"
        subject = "Your sign-in link for Templo"

    html_body = f"""
        <h2>Sign in to Templo</h2>
        {inviter_line}
        <p><a href="{magic_url}" style="background-color: #4F46E5; color: white; padding: 12px 24px; text-decoration: none; border-radius: 8px; display: inline-block;">{cta}</a></p>
        <p>Or copy this link: {magic_url}</p>
        <p>This link expires in 24 hours and can only be used once.</p>
        <p style="color: #6b7280; font-size: 14px; margin-top: 20px;">Templo: templobooker.com</p>
    """
    return send_email(to_email, subject, html_body)


def get_current_user():
    user_email = normalize_email(session.get("user_email"))
    if not user_email:
        return None

    user = get_user_profile(user_email)
    if not user:
        return {
            "email": user_email,
            "display_name": None,
            "profile_picture": None,
            "role": default_role_for_email(user_email),
            "tier": default_tier_for_email(user_email),
            "is_verified": False
        }

    user["email"] = normalize_email(user["email"])
    user["role"] = (user.get("role") or default_role_for_email(user["email"])).lower()
    user["tier"] = (user.get("tier") or default_tier_for_email(user["email"])).lower()
    return user


def is_admin_user(user):
    return bool(user and user.get("role") == "admin")


def user_can_manage_poll(user, poll):
    if not user or not poll:
        return False
    if is_admin_user(user):
        return True
    return normalize_email(poll.get("admin_email")) == normalize_email(user.get("email"))


def user_can_access_poll(user, poll):
    if not user or not poll:
        return False
    if user_can_manage_poll(user, poll):
        return True

    invited_emails = parse_invite_emails(poll.get("invite_emails"))
    return normalize_email(user.get("email")) in invited_emails


def poll_is_invite_only(poll):
    return (poll.get("access_mode") or "public_link") == "invite_only"


def is_user_paid(user):
    if not user:
        return False
    if (user.get("role") or "").lower() == "admin":
        return True
    return (user.get("tier") or "").lower() == "paid"


def can_view_poll(user, poll):
    if not poll:
        return False
    if not poll_is_invite_only(poll):
        return True
    return user_can_access_poll(user, poll)


def can_vote_on_poll(user, poll):
    if not user or not poll:
        return False
    if not poll_is_invite_only(poll):
        return True
    return user_can_access_poll(user, poll)


def get_owned_poll_count(email):
    """Count *active* (not closed) polls — the free-tier limit is per active poll."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM polls WHERE LOWER(admin_email) = %s AND closed_at IS NULL",
        (normalize_email(email),)
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count


def delete_poll_records(cursor, poll_id):
    cursor.execute("SELECT id FROM dates WHERE poll_id = %s", (poll_id,))
    date_rows = cursor.fetchall()
    date_ids = [
        row["id"] if isinstance(row, dict) else row[0]
        for row in date_rows
    ]

    for date_id in date_ids:
        cursor.execute("DELETE FROM votes WHERE date_id = %s", (date_id,))

    cursor.execute("DELETE FROM dates WHERE poll_id = %s", (poll_id,))
    cursor.execute("DELETE FROM polls WHERE id = %s", (poll_id,))


def sync_admin_account(conn, email):
    normalized_email = normalize_email(email)
    if normalized_email not in ADMIN_EMAILS:
        return
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET role = 'admin', tier = 'paid' WHERE LOWER(email) = %s",
        (normalized_email,)
    )


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    # Track connections per-request so any left open when a route raises
    # (routes don't use try/finally) are closed instead of leaking.
    if has_app_context():
        conns = getattr(g, "_db_conns", None)
        if conns is None:
            conns = []
            g._db_conns = conns
        conns.append(conn)
    return conn


@app.teardown_appcontext
def _close_db_connections(exc):
    for conn in getattr(g, "_db_conns", []):
        try:
            conn.close()
        except Exception:
            pass


def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS polls (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            admin_email TEXT NOT NULL,
            invite_emails TEXT,
            access_mode TEXT DEFAULT 'public_link' CHECK(access_mode IN ('public_link', 'invite_only')),
            slug TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    try:
        cursor.execute("ALTER TABLE polls ADD COLUMN IF NOT EXISTS access_mode TEXT DEFAULT 'public_link'")
        cursor.execute("ALTER TABLE polls ADD COLUMN IF NOT EXISTS slug TEXT")
        cursor.execute("ALTER TABLE polls ADD COLUMN IF NOT EXISTS closed_at TIMESTAMP")
        cursor.execute("ALTER TABLE polls ADD COLUMN IF NOT EXISTS closed_by TEXT")
        cursor.execute("ALTER TABLE polls ADD COLUMN IF NOT EXISTS event_start_time TEXT")
        cursor.execute("ALTER TABLE polls ADD COLUMN IF NOT EXISTS event_end_time TEXT")
        cursor.execute("UPDATE polls SET access_mode = 'public_link' WHERE access_mode IS NULL")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS polls_slug_unique ON polls(slug) WHERE slug IS NOT NULL")
    except Exception as e:
        print(f"WARNING: polls migration: {e}")
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS dates (
            id SERIAL PRIMARY KEY,
            poll_id TEXT NOT NULL REFERENCES polls(id),
            date TEXT NOT NULL
        )
    ''')

    try:
        cursor.execute("ALTER TABLE dates ADD COLUMN IF NOT EXISTS is_final BOOLEAN DEFAULT FALSE")
        cursor.execute("ALTER TABLE dates ADD COLUMN IF NOT EXISTS start_time TEXT")
        cursor.execute("ALTER TABLE dates ADD COLUMN IF NOT EXISTS end_time TEXT")
    except Exception as e:
        print(f"WARNING: dates migration: {e}")
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS votes (
            id SERIAL PRIMARY KEY,
            date_id INTEGER NOT NULL REFERENCES dates(id),
            user_email TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('yes', 'no', 'maybe')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date_id, user_email)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            is_verified BOOLEAN DEFAULT FALSE,
            role TEXT DEFAULT 'user',
            tier TEXT DEFAULT 'free',
            display_name TEXT,
            profile_picture TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Add columns if they don't exist (for existing tables)
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_picture TEXT")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'user'")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tier TEXT DEFAULT 'free'")
        cursor.execute("UPDATE users SET role = 'user' WHERE role IS NULL OR role = ''")
        cursor.execute("UPDATE users SET tier = 'free' WHERE tier IS NULL OR tier = ''")
        for admin_email in ADMIN_EMAILS:
            cursor.execute(
                "UPDATE users SET role = 'admin', tier = 'paid' WHERE LOWER(email) = %s",
                (admin_email,)
            )
    except:
        pass
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS verification_tokens (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS account_requests (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            name TEXT,
            reason TEXT,
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
            approval_token TEXT UNIQUE,
            reviewed_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS magic_links (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            poll_id TEXT,
            expires_at TIMESTAMP NOT NULL,
            used_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()


init_db()


@app.context_processor
def utility_processor():
    current_user = get_current_user()
    pending_request_count = 0
    if is_admin_user(current_user):
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM account_requests WHERE status = 'pending'")
            pending_request_count = cursor.fetchone()[0]
            conn.close()
        except:
            pass
    return dict(
        get_name=get_name,
        current_user=current_user,
        is_admin=is_admin_user(current_user),
        pending_request_count=pending_request_count
    )


@app.route("/")
def home():
    if not session.get("user_email"):
        return render_template("marketing_home.html")
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = normalize_email(request.form.get("email", ""))

        if not email or "@" not in email:
            flash("Please enter a valid email address.", "error")
            return redirect(url_for("login"))

        if not is_allowed_email(email):
            flash("No account found for this email. Please request access first.", "error")
            return redirect(url_for("request_account", email=email))

        conn = get_db()
        cursor = conn.cursor()

        if magic_link_rate_limited(cursor, email):
            conn.commit()
            conn.close()
            flash("Too many sign-in links requested for this email. Check your inbox (and spam), or try again in an hour.", "error")
            return redirect(url_for("login"))

        cursor.execute(
            "INSERT INTO users (email, role, tier) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (email, default_role_for_email(email), default_tier_for_email(email))
        )
        sync_admin_account(conn, email)

        token = generate_token()
        cursor.execute(
            "INSERT INTO magic_links (email, token, poll_id, expires_at) VALUES (%s, %s, NULL, NOW() + INTERVAL '24 hours')",
            (email, token)
        )
        conn.commit()
        conn.close()

        sent, error = send_magic_link_email(email, token, None, request.url_root)
        if sent:
            flash(f"Check your inbox — we've sent a sign-in link to {email}.", "success")
        else:
            flash(email_error_flash(error), "error")
            if error:
                print(f"ERROR: Login magic-link send failed for {email}: {error}")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/verify/<token>")
def verify_email(token):
    flash("Templo no longer uses passwords — sign in with a one-click email link instead.", "success")
    return redirect(url_for("login"))


@app.route("/create")
def create_poll():
    current_user = get_current_user()

    if not current_user:
        flash("Please log in first", "error")
        return redirect(url_for("login"))

    owned_poll_count = get_owned_poll_count(current_user["email"])
    free_limit_reached = current_user["tier"] == "free" and owned_poll_count >= FREE_POLL_LIMIT

    return render_template(
        "home.html",
        user_email=current_user["email"],
        owned_poll_count=owned_poll_count,
        free_poll_limit=FREE_POLL_LIMIT,
        free_date_limit=FREE_DATE_LIMIT,
        can_create_poll=not free_limit_reached
    )


@app.route("/create", methods=["POST"])
def create_poll_step1():
    current_user = get_current_user()
    if not current_user:
        flash("Please log in first", "error")
        return redirect(url_for("login"))

    poll_name = request.form.get("poll_name", "").strip()
    
    if not poll_name:
        flash("Please fill in all fields", "error")
        return redirect(url_for("create_poll"))
    if len(poll_name) > 120:
        flash("Poll name is too long (max 120 characters).", "error")
        return redirect(url_for("create_poll"))

    if current_user["tier"] == "free":
        owned_poll_count = get_owned_poll_count(current_user["email"])
        if owned_poll_count >= FREE_POLL_LIMIT:
            flash("Free tier allows 1 active poll at a time. Close or delete your existing poll, or upgrade to Pro.", "error")
            return redirect(url_for("dashboard"))

    session["poll_name"] = poll_name
    session["poll_creator_email"] = current_user["email"]
    
    return redirect(url_for("calendar_view"))


@app.route("/calendar")
def calendar_view():
    current_user = get_current_user()
    if not current_user:
        flash("Please log in first", "error")
        return redirect(url_for("login"))

    if "poll_name" not in session:
        flash("Please start by creating a poll", "error")
        return redirect(url_for("create_poll"))

    if normalize_email(session.get("poll_creator_email")) != normalize_email(current_user["email"]):
        flash("Poll creation session expired. Please start again.", "error")
        session.pop("poll_name", None)
        session.pop("poll_creator_email", None)
        return redirect(url_for("create_poll"))

    max_dates = FREE_DATE_LIMIT if current_user["tier"] == "free" else None
    return render_template(
        "calendar.html",
        poll_name=session["poll_name"],
        max_dates=max_dates
    )


@app.route("/finalize", methods=["POST"])
def finalize_poll():
    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Please log in first"}), 401

    if "poll_name" not in session:
        return jsonify({"error": "Session expired"}), 400

    if normalize_email(session.get("poll_creator_email")) != normalize_email(current_user["email"]):
        return jsonify({"error": "Session expired"}), 400
    
    data = request.get_json(silent=True) or {}
    selected_dates = sorted(set(data.get("dates", [])))
    
    if not selected_dates:
        return jsonify({"error": "Please select at least one date"}), 400

    if any(not is_valid_date_string(date_str) for date_str in selected_dates):
        return jsonify({"error": "One or more selected dates are invalid."}), 400

    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if any(date_str < today_iso for date_str in selected_dates):
        return jsonify({"error": "Past dates are not allowed."}), 400

    if current_user["tier"] == "free":
        if len(selected_dates) > FREE_DATE_LIMIT:
            return jsonify({"error": f"Free tier allows up to {FREE_DATE_LIMIT} dates per poll."}), 400
        owned_poll_count = get_owned_poll_count(current_user["email"])
        if owned_poll_count >= FREE_POLL_LIMIT:
            return jsonify({"error": "Free tier allows 1 active poll at a time. Close or delete your existing poll, or upgrade."}), 400
    
    conn = get_db()
    cursor = conn.cursor()

    poll_id = generate_short_id()
    cursor.execute("SELECT 1 FROM polls WHERE id = %s", (poll_id,))
    while cursor.fetchone():
        poll_id = generate_short_id()
        cursor.execute("SELECT 1 FROM polls WHERE id = %s", (poll_id,))
    
    cursor.execute(
        "INSERT INTO polls (id, name, admin_email) VALUES (%s, %s, %s)",
        (poll_id, session["poll_name"], current_user["email"])
    )
    
    date_ids = []
    for date_str in selected_dates:
        cursor.execute(
            "INSERT INTO dates (poll_id, date) VALUES (%s, %s) RETURNING id",
            (poll_id, date_str)
        )
        date_ids.append(cursor.fetchone()[0])
    
    for date_id in date_ids:
        cursor.execute(
            "INSERT INTO votes (date_id, user_email, status) VALUES (%s, %s, 'yes')",
            (date_id, current_user["email"])
        )
    
    conn.commit()
    conn.close()
    
    session.pop("poll_name", None)
    session.pop("poll_creator_email", None)
    
    return jsonify({"poll_id": poll_id})


@app.route("/share/<poll_id>")
def share_poll(poll_id):
    current_user = get_current_user()
    if not current_user:
        flash("Please log in first", "error")
        return redirect(url_for("login"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    
    if not poll:
        flash("Poll not found", "error")
        conn.close()
        return redirect(url_for("home"))

    if not user_can_manage_poll(current_user, poll):
        flash("Only the poll creator or an admin can manage sharing settings.", "error")
        conn.close()
        return redirect(url_for("view_poll", poll_id=poll_id))

    invited = parse_invite_emails(poll.get("invite_emails"))
    poll["invite_emails"] = serialize_invite_emails(invited)
    poll["access_mode"] = poll.get("access_mode") or "public_link"

    invitee_status = []
    if invited:
        cursor.execute(
            '''SELECT v.user_email, v.status, v.created_at
               FROM votes v
               JOIN dates d ON d.id = v.date_id
               WHERE d.poll_id = %s''',
            (poll_id,)
        )
        rows = cursor.fetchall()
        latest = {}
        for row in rows:
            email_norm = normalize_email(row["user_email"])
            existing = latest.get(email_norm)
            if not existing or (row["created_at"] and row["created_at"] > existing["created_at"]):
                latest[email_norm] = {"status": row["status"], "created_at": row["created_at"]}

        for email in invited:
            v = latest.get(email)
            invitee_status.append({
                "email": email,
                "voted": v is not None,
                "last_status": v["status"] if v else None,
                "last_voted_at": v["created_at"] if v else None,
            })

    conn.close()

    base_url = request.url_root.rstrip("/")
    if poll.get("slug"):
        poll_url = base_url + "/p/" + poll["slug"]
    else:
        poll_url = base_url + url_for("view_poll", poll_id=poll_id)

    return render_template(
        "share.html",
        poll=poll,
        poll_url=poll_url,
        invitee_status=invitee_status,
        is_paid=is_user_paid(current_user),
        base_url=base_url,
    )


@app.route("/share/<poll_id>/update-emails", methods=["POST"])
def update_invite_emails(poll_id):
    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Please log in first"}), 401

    data = request.get_json(silent=True) or {}
    emails = data.get("emails", "")
    poll_name = (data.get("name") or "").strip()
    access_mode = (data.get("access_mode") or "").strip().lower()
    slug = (data.get("slug") or "").strip().lower() or None

    if poll_name and len(poll_name) > 120:
        return jsonify({"error": "Poll name is too long (max 120 characters)."}), 400
    if access_mode and access_mode not in ("public_link", "invite_only"):
        return jsonify({"error": "Invalid access mode."}), 400

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()

    if not poll:
        conn.close()
        return jsonify({"error": "Poll not found"}), 404

    if not user_can_manage_poll(current_user, poll):
        conn.close()
        return jsonify({"error": "You do not have permission to edit this poll"}), 403

    invite_text = serialize_invite_emails(parse_invite_emails(emails))

    if slug is not None:
        if not is_user_paid(current_user):
            conn.close()
            return jsonify({"error": "Custom URLs are a Pro feature."}), 403
        if slug:
            if not re.match(r"^[a-z0-9][a-z0-9-]{2,39}$", slug):
                conn.close()
                return jsonify({"error": "Slug must be 3-40 lowercase letters/numbers/dashes, starting with a letter or digit."}), 400
            cursor.execute("SELECT id FROM polls WHERE slug = %s AND id != %s", (slug, poll_id))
            if cursor.fetchone():
                conn.close()
                return jsonify({"error": "That custom URL is already taken."}), 400

    fields = ["invite_emails = %s"]
    params = [invite_text]
    if poll_name:
        fields.append("name = %s")
        params.append(poll_name)
    if access_mode:
        fields.append("access_mode = %s")
        params.append(access_mode)
    if slug is not None:
        fields.append("slug = %s")
        params.append(slug or None)
    params.append(poll_id)
    cursor.execute(f"UPDATE polls SET {', '.join(fields)} WHERE id = %s", tuple(params))

    conn.commit()
    conn.close()

    return jsonify({"success": True, "invite_count": len(parse_invite_emails(invite_text))})


@app.route("/p/<slug>")
def view_poll_by_slug(slug):
    slug = (slug or "").strip().lower()
    if not slug:
        return redirect(url_for("home"))
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT id FROM polls WHERE slug = %s", (slug,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        flash("Poll not found", "error")
        return redirect(url_for("home"))
    return redirect(url_for("view_poll", poll_id=row["id"]))


@app.route("/poll/<poll_id>")
def view_poll(poll_id):
    current_user = get_current_user()

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()

    if not poll:
        conn.close()
        flash("Poll not found", "error")
        return redirect(url_for("home"))

    if not can_view_poll(current_user, poll):
        conn.close()
        if not current_user:
            return redirect(url_for("poll_access", poll_id=poll_id))
        flash("This poll is invite-only and you're not on the list.", "error")
        return redirect(url_for("dashboard"))

    cursor.execute("SELECT * FROM dates WHERE poll_id = %s ORDER BY date", (poll_id,))
    dates = cursor.fetchall()

    cursor.execute(
        '''SELECT v.date_id, v.user_email, v.status
           FROM votes v JOIN dates d ON d.id = v.date_id
           WHERE d.poll_id = %s''',
        (poll_id,)
    )
    all_votes = cursor.fetchall()
    conn.close()

    votes_dict = {date["id"]: {} for date in dates}
    participants = set()
    yes_counts = {date["id"]: 0 for date in dates}
    possible_dates = []

    for v in all_votes:
        votes_dict.setdefault(v["date_id"], {})[v["user_email"]] = v["status"]
        if v["status"] == "yes":
            yes_counts[v["date_id"]] = yes_counts.get(v["date_id"], 0) + 1
        participants.add(v["user_email"])

    for date in dates:
        statuses = set(votes_dict.get(date["id"], {}).values())
        if statuses and "no" not in statuses:
            possible_dates.append(date["id"])

    max_yes = max(yes_counts.values()) if yes_counts else 0
    best_dates = [d_id for d_id, count in yes_counts.items() if count == max_yes and max_yes > 0]

    poll["can_manage"] = user_can_manage_poll(current_user, poll)
    is_closed = bool(poll.get("closed_at"))
    final_dates = [d for d in dates if d.get("is_final")]
    sorted_participants = sorted(participants)

    ai_prompt = ""
    if is_closed and final_dates:
        ai_prompt = build_ai_calendar_prompt(
            poll, final_dates, sorted_participants, normalize_email(poll.get("admin_email"))
        )
        for d in final_dates:
            links = build_calendar_deeplinks(poll, d, sorted_participants)
            d["google_url"] = links["google_url"]
            d["outlook_url"] = links["outlook_url"]

    return render_template(
        "vote.html",
        poll=poll,
        dates=dates,
        votes_dict=votes_dict,
        participants=sorted_participants,
        user_email=current_user["email"] if current_user else None,
        best_dates=best_dates,
        possible_dates=possible_dates,
        is_closed=is_closed,
        final_dates=final_dates,
        ai_prompt=ai_prompt,
    )


@app.route("/poll/<poll_id>/access", methods=["GET", "POST"])
def poll_access(poll_id):
    if get_current_user():
        return redirect(url_for("view_poll", poll_id=poll_id))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    conn.close()

    if not poll:
        flash("That poll link doesn't exist or has been deleted.", "error")
        return redirect(url_for("home"))

    if request.method == "POST":
        email = normalize_email(request.form.get("email", ""))
        if not email or "@" not in email:
            flash("Please enter a valid email address.", "error")
            return redirect(url_for("poll_access", poll_id=poll_id))

        if poll_is_invite_only(poll):
            invited = parse_invite_emails(poll.get("invite_emails"))
            if email != normalize_email(poll.get("admin_email")) and email not in invited:
                flash("This poll is invite-only. Ask the poll creator to add your email.", "error")
                return redirect(url_for("poll_access", poll_id=poll_id))

        token = generate_token()
        conn = get_db()
        cursor = conn.cursor()

        if magic_link_rate_limited(cursor, email):
            conn.commit()
            conn.close()
            flash("Too many sign-in links requested for this email. Check your inbox (and spam), or try again in an hour.", "error")
            return redirect(url_for("poll_access", poll_id=poll_id))

        cursor.execute(
            "INSERT INTO magic_links (email, token, poll_id, expires_at) VALUES (%s, %s, %s, NOW() + INTERVAL '24 hours')",
            (email, token, poll_id)
        )
        conn.commit()
        conn.close()

        sent, error = send_magic_link_email(email, token, poll, request.url_root)
        if sent:
            flash(f"Check your inbox — we've sent a sign-in link to {email}.", "success")
        else:
            flash(email_error_flash(error), "error")
            if error:
                print(f"ERROR: Magic link send failed for {email}: {error}")
        return redirect(url_for("poll_access", poll_id=poll_id))

    return render_template(
        "poll_access.html",
        poll=poll,
        admin_name=get_name(poll.get("admin_email"))
    )


@app.route("/magic/<token>")
def magic_login(token):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        '''SELECT * FROM magic_links
           WHERE token = %s
             AND expires_at > NOW()
             AND (used_at IS NULL OR used_at > NOW() - INTERVAL '10 minutes')''',
        (token,)
    )
    link = cursor.fetchone()

    if not link:
        conn.close()
        existing_email = normalize_email(session.get("user_email"))
        if existing_email:
            return redirect(url_for("dashboard"))
        flash("This sign-in link has expired or already been used. Enter your email below to get a new one.", "error")
        return redirect(url_for("login"))

    email = normalize_email(link["email"])
    poll_id = link["poll_id"]

    if link["used_at"] is None:
        cursor.execute("UPDATE magic_links SET used_at = NOW() WHERE id = %s", (link["id"],))

    cursor.execute("SELECT id FROM users WHERE LOWER(email) = %s", (email,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO users (email, role, tier, is_verified) VALUES (%s, %s, %s, TRUE)",
            (email, default_role_for_email(email), default_tier_for_email(email))
        )
    else:
        cursor.execute("UPDATE users SET is_verified = TRUE WHERE LOWER(email) = %s", (email,))

    sync_admin_account(conn, email)
    conn.commit()
    conn.close()

    already_signed_in = normalize_email(session.get("user_email")) == email
    session.permanent = True
    session["user_email"] = email
    if not already_signed_in:
        flash(f"Signed in as {get_name(email)}.", "success")

    if poll_id:
        return redirect(url_for("view_poll", poll_id=poll_id))
    return redirect(url_for("dashboard"))


@app.route("/poll/<poll_id>/delete", methods=["POST"])
def delete_poll(poll_id):
    current_user = get_current_user()
    
    if not current_user:
        flash("Please log in first", "error")
        return redirect(url_for("view_poll", poll_id=poll_id))
    
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute("SELECT admin_email FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    
    if not poll:
        conn.close()
        flash("Poll not found", "error")
        return redirect(url_for("home"))
    
    if not user_can_manage_poll(current_user, poll):
        conn.close()
        flash("Only the poll creator or an admin can delete this poll", "error")
        return redirect(url_for("view_poll", poll_id=poll_id))
    
    delete_poll_records(cursor, poll_id)
    
    conn.commit()
    conn.close()
    
    flash("Poll deleted successfully", "success")
    return redirect(url_for("dashboard"))


def _participant_emails(cursor, poll_id):
    cursor.execute(
        '''SELECT DISTINCT user_email FROM votes v
           JOIN dates d ON v.date_id = d.id
           WHERE d.poll_id = %s''',
        (poll_id,)
    )
    return sorted({normalize_email(r["user_email"]) for r in cursor.fetchall() if r["user_email"]})


@app.route("/poll/<poll_id>/close", methods=["POST"])
def close_poll(poll_id):
    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Please sign in.", "needs_login": True}), 401

    data = request.get_json(silent=True) or {}
    raw_ids = data.get("date_ids") or []
    try:
        date_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid date IDs."}), 400
    if not date_ids:
        return jsonify({"error": "Pick at least one final date."}), 400

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    if not poll:
        conn.close()
        return jsonify({"error": "Poll not found."}), 404
    if not user_can_manage_poll(current_user, poll):
        conn.close()
        return jsonify({"error": "Only the poll organizer can close this poll."}), 403

    cursor.execute("SELECT id FROM dates WHERE poll_id = %s", (poll_id,))
    valid_ids = {r["id"] for r in cursor.fetchall()}
    final_ids = [d for d in date_ids if d in valid_ids]
    if not final_ids:
        conn.close()
        return jsonify({"error": "None of the selected dates belong to this poll."}), 400

    cursor.execute("UPDATE dates SET is_final = FALSE WHERE poll_id = %s", (poll_id,))
    cursor.execute(
        "UPDATE dates SET is_final = TRUE WHERE poll_id = %s AND id = ANY(%s)",
        (poll_id, final_ids)
    )
    cursor.execute(
        "UPDATE polls SET closed_at = NOW(), closed_by = %s WHERE id = %s",
        (current_user["email"], poll_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/poll/<poll_id>/reopen", methods=["POST"])
def reopen_poll(poll_id):
    current_user = get_current_user()
    if not current_user:
        flash("Please sign in.", "error")
        return redirect(url_for("login"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    if not poll:
        conn.close()
        flash("Poll not found.", "error")
        return redirect(url_for("dashboard"))
    if not user_can_manage_poll(current_user, poll):
        conn.close()
        flash("Only the poll organizer can reopen this poll.", "error")
        return redirect(url_for("view_poll", poll_id=poll_id))

    cursor.execute("UPDATE dates SET is_final = FALSE WHERE poll_id = %s", (poll_id,))
    cursor.execute("UPDATE polls SET closed_at = NULL, closed_by = NULL WHERE id = %s", (poll_id,))
    conn.commit()
    conn.close()
    flash("Poll reopened — voting is live again.", "success")
    return redirect(url_for("view_poll", poll_id=poll_id))


@app.route("/poll/<poll_id>/event-time", methods=["POST"])
def set_poll_event_time(poll_id):
    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Please sign in.", "needs_login": True}), 401

    data = request.get_json(silent=True) or {}
    all_day = bool(data.get("all_day"))
    start_time = (data.get("start_time") or "").strip() or None
    end_time = (data.get("end_time") or "").strip() or None

    if not all_day:
        if not start_time or not end_time:
            return jsonify({"error": "Provide both a start and an end time, or switch to all-day."}), 400
        if not _parse_hhmm(start_time) or not _parse_hhmm(end_time):
            return jsonify({"error": "Times must be in HH:MM (24-hour) format."}), 400

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    if not poll:
        conn.close()
        return jsonify({"error": "Poll not found."}), 404
    if not user_can_manage_poll(current_user, poll):
        conn.close()
        return jsonify({"error": "Only the poll organizer can set event times."}), 403

    if all_day:
        cursor.execute(
            "UPDATE polls SET event_start_time = NULL, event_end_time = NULL WHERE id = %s",
            (poll_id,)
        )
    else:
        cursor.execute(
            "UPDATE polls SET event_start_time = %s, event_end_time = %s WHERE id = %s",
            (start_time, end_time, poll_id)
        )
    conn.commit()

    # Re-fetch poll with new times so the AI prompt reflects them
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    cursor.execute(
        "SELECT * FROM dates WHERE poll_id = %s AND is_final = TRUE ORDER BY date",
        (poll_id,)
    )
    updated_finals = cursor.fetchall()
    attendees = _participant_emails(cursor, poll_id)
    conn.close()

    organizer_email = normalize_email(poll.get("admin_email"))
    ai_prompt = build_ai_calendar_prompt(poll, updated_finals, attendees, organizer_email)

    return jsonify({
        "success": True,
        "all_day": all_day,
        "start_time": poll.get("event_start_time"),
        "end_time": poll.get("event_end_time"),
        "ai_prompt": ai_prompt,
    })


@app.route("/poll/<poll_id>/event/<int:date_id>.ics")
def download_event_ics(poll_id, date_id):
    current_user = get_current_user()
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    if not poll or not poll.get("closed_at"):
        conn.close()
        flash("Calendar invites are only available for closed polls.", "error")
        return redirect(url_for("view_poll", poll_id=poll_id))
    if not can_view_poll(current_user, poll):
        conn.close()
        flash("You don't have access to this poll.", "error")
        return redirect(url_for("home"))

    cursor.execute(
        "SELECT * FROM dates WHERE id = %s AND poll_id = %s AND is_final = TRUE",
        (date_id, poll_id)
    )
    date_row = cursor.fetchone()
    if not date_row:
        conn.close()
        flash("That date isn't a confirmed date for this poll.", "error")
        return redirect(url_for("view_poll", poll_id=poll_id))

    attendees = _participant_emails(cursor, poll_id)
    organizer_email = normalize_email(poll.get("admin_email"))
    organizer_name = get_name(organizer_email) or organizer_email
    conn.close()

    _, sender_email = get_mail_from()
    ics = generate_ics(poll, date_row, attendees, organizer_email, organizer_name, sent_by_email=sender_email)
    filename = f"templo-{poll['id']}-{date_row['date']}.ics"
    response = app.make_response(ics)
    response.headers["Content-Type"] = "text/calendar; charset=utf-8"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


@app.route("/poll/<poll_id>/vote", methods=["POST"])
def submit_vote(poll_id):
    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Please sign in to vote", "needs_login": True}), 401

    data = request.get_json(silent=True) or {}
    date_id = data.get("date_id")
    status = data.get("status")

    if not date_id or status not in ["yes", "no", "maybe"]:
        return jsonify({"error": "Invalid vote data"}), 400

    try:
        date_id = int(date_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid date ID"}), 400

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    if not poll:
        conn.close()
        return jsonify({"error": "Poll not found"}), 404

    if not can_vote_on_poll(current_user, poll):
        conn.close()
        return jsonify({"error": "This poll is invite-only and you're not on the list."}), 403

    if poll.get("closed_at"):
        conn.close()
        return jsonify({"error": "This poll is closed and no longer accepting votes."}), 403

    cursor.execute("SELECT id FROM dates WHERE id = %s AND poll_id = %s", (date_id, poll_id))
    if not cursor.fetchone():
        conn.close()
        return jsonify({"error": "Invalid date for this poll"}), 400

    cursor.execute(
        '''INSERT INTO votes (date_id, user_email, status)
           VALUES (%s, %s, %s)
           ON CONFLICT(date_id, user_email)
           DO UPDATE SET status = EXCLUDED.status, created_at = NOW()''',
        (date_id, current_user["email"], status)
    )

    voter_email = normalize_email(current_user["email"])
    if voter_email and voter_email != normalize_email(poll.get("admin_email")) and not poll_is_invite_only(poll):
        invited = set(parse_invite_emails(poll.get("invite_emails")))
        if voter_email not in invited:
            invited.add(voter_email)
            cursor.execute(
                "UPDATE polls SET invite_emails = %s WHERE id = %s",
                (serialize_invite_emails(invited), poll_id)
            )

    conn.commit()
    conn.close()

    return jsonify({"success": True, "date_removed": False})


@app.route("/dashboard")
def dashboard():
    current_user = get_current_user()

    if not current_user:
        return redirect(url_for("login"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute('''
        SELECT p.*, COUNT(d.id) AS date_count
        FROM polls p
        LEFT JOIN dates d ON d.poll_id = p.id
        GROUP BY p.id
        ORDER BY p.created_at DESC
    ''')
    all_polls = cursor.fetchall()
    conn.close()

    visible_polls = []
    for poll in all_polls:
        if not user_can_access_poll(current_user, poll):
            continue

        is_owner = normalize_email(poll["admin_email"]) == normalize_email(current_user["email"])
        invited_set = set(parse_invite_emails(poll.get("invite_emails")))
        poll["is_owner"] = is_owner
        poll["is_invited"] = normalize_email(current_user["email"]) in invited_set and not is_owner
        poll["can_manage"] = user_can_manage_poll(current_user, poll)
        poll["invite_count"] = len(invited_set)
        visible_polls.append(poll)

    owned_poll_count = get_owned_poll_count(current_user["email"])
    can_create_poll = not (current_user["tier"] == "free" and owned_poll_count >= FREE_POLL_LIMIT)

    return render_template(
        "dashboard.html",
        polls=visible_polls,
        user_email=current_user["email"],
        owned_poll_count=owned_poll_count,
        free_poll_limit=FREE_POLL_LIMIT,
        free_date_limit=FREE_DATE_LIMIT,
        can_create_poll=can_create_poll
    )


@app.route("/upgrade")
def upgrade():
    current_user = get_current_user()
    if not current_user:
        flash("Please log in first", "error")
        return redirect(url_for("login"))

    if current_user["tier"] == "paid":
        flash("You're already on the Pro tier.", "success")
        return redirect(url_for("dashboard"))

    return render_template("upgrade.html")


@app.route("/admin")
def admin_panel():
    current_user = get_current_user()
    if not is_admin_user(current_user):
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute(
        '''
        SELECT
            u.email,
            u.display_name,
            u.role,
            u.tier,
            u.is_verified,
            u.created_at,
            COUNT(p.id) AS owned_poll_count
        FROM users u
        LEFT JOIN polls p ON LOWER(p.admin_email) = LOWER(u.email)
        GROUP BY u.email, u.display_name, u.role, u.tier, u.is_verified, u.created_at
        ORDER BY u.created_at ASC
        '''
    )
    users = cursor.fetchall()

    cursor.execute(
        '''
        SELECT p.*, COUNT(d.id) AS date_count
        FROM polls p
        LEFT JOIN dates d ON d.poll_id = p.id
        GROUP BY p.id
        ORDER BY p.created_at DESC
        '''
    )
    polls = cursor.fetchall()

    cursor.execute(
        "SELECT * FROM account_requests ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END, created_at DESC"
    )
    account_requests = cursor.fetchall()
    conn.close()

    for poll in polls:
        poll["invite_count"] = len(parse_invite_emails(poll.get("invite_emails")))

    pending_count = sum(1 for r in account_requests if r["status"] == "pending")

    mailersend_key = get_setting("mailersend_api_key") or ""
    mailersend_key_masked = (mailersend_key[:6] + "…" + mailersend_key[-4:]) if len(mailersend_key) > 12 else ""
    mail_from_name, mail_from_email = get_mail_from()
    mail_status = check_mailersend_status(mailersend_key) if mailersend_key else {"ok": False, "configured": False, "message": "Not configured"}

    return render_template(
        "admin.html",
        users=users,
        polls=polls,
        account_requests=account_requests,
        pending_count=pending_count,
        mail_status=mail_status,
        mailersend_key_masked=mailersend_key_masked,
        mailersend_key_set=bool(mailersend_key),
        mail_from_name=mail_from_name,
        mail_from_email=mail_from_email,
    )


@app.route("/admin/users/<path:target_email>/update", methods=["POST"])
def admin_update_user(target_email):
    current_user = get_current_user()
    if not is_admin_user(current_user):
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))

    target_email = normalize_email(target_email)
    role = (request.form.get("role") or "").strip().lower()
    tier = (request.form.get("tier") or "").strip().lower()
    is_verified = request.form.get("is_verified") == "on"

    if role not in VALID_ROLES or tier not in VALID_TIERS:
        flash("Invalid role or tier selection.", "error")
        return redirect(url_for("admin_panel"))

    if role == "admin":
        tier = "paid"
        is_verified = True

    if target_email in ADMIN_EMAILS:
        role = "admin"
        tier = "paid"
        is_verified = True

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    if role != "admin":
        cursor.execute("SELECT COUNT(*) AS admin_count FROM users WHERE role = 'admin'")
        admin_count = cursor.fetchone()["admin_count"]
        cursor.execute("SELECT role FROM users WHERE LOWER(email) = %s", (target_email,))
        current_target = cursor.fetchone()
        if current_target and current_target["role"] == "admin" and admin_count <= 1:
            conn.close()
            flash("At least one admin must remain in the system.", "error")
            return redirect(url_for("admin_panel"))

    cursor.execute(
        "UPDATE users SET role = %s, tier = %s, is_verified = %s WHERE LOWER(email) = %s",
        (role, tier, is_verified, target_email)
    )
    if cursor.rowcount == 0:
        conn.close()
        flash("User not found.", "error")
        return redirect(url_for("admin_panel"))

    conn.commit()
    conn.close()

    flash("User updated successfully.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/polls/<poll_id>/delete", methods=["POST"])
def admin_delete_poll(poll_id):
    current_user = get_current_user()
    if not is_admin_user(current_user):
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT id FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()

    if not poll:
        conn.close()
        flash("Poll not found.", "error")
        return redirect(url_for("admin_panel"))

    delete_poll_records(cursor, poll_id)
    conn.commit()
    conn.close()

    flash("Poll deleted.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/<path:target_email>/send-magic-link", methods=["POST"])
def admin_send_magic_link(target_email):
    current_user = get_current_user()
    if not is_admin_user(current_user):
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))

    email = normalize_email(target_email)
    if not email or "@" not in email:
        flash("Invalid email.", "error")
        return redirect(url_for("admin_panel"))

    poll_id = (request.form.get("poll_id") or "").strip() or None
    poll = None

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("SELECT id FROM users WHERE LOWER(email) = %s", (email,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO users (email, role, tier, is_verified) VALUES (%s, %s, %s, FALSE)",
            (email, default_role_for_email(email), default_tier_for_email(email))
        )

    if poll_id:
        cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
        poll = cursor.fetchone()

    token = generate_token()
    cursor.execute(
        "INSERT INTO magic_links (email, token, poll_id, expires_at) VALUES (%s, %s, %s, NOW() + INTERVAL '24 hours')",
        (email, token, poll_id)
    )
    conn.commit()
    conn.close()

    sent, error = send_magic_link_email(email, token, poll, request.url_root)
    if sent:
        flash(f"Sign-in link sent to {email}.", "success")
    else:
        flash(f"Could not send sign-in link to {email}: {error}", "error")
    return redirect(url_for("admin_panel"))


@app.route("/admin/email/save", methods=["POST"])
def admin_save_email_settings():
    current_user = get_current_user()
    if not is_admin_user(current_user):
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))

    api_key = (request.form.get("mailersend_api_key") or "").strip()
    from_name = (request.form.get("mail_from_name") or "").strip()
    from_email = normalize_email(request.form.get("mail_from_email", ""))

    if api_key:
        set_setting("mailersend_api_key", api_key)
    if from_name:
        set_setting("mail_from_name", from_name)
    if from_email:
        set_setting("mail_from_email", from_email)

    flash("Email settings saved.", "success")
    return redirect(url_for("admin_panel") + "#email-settings")


@app.route("/admin/email/clear", methods=["POST"])
def admin_clear_email_key():
    current_user = get_current_user()
    if not is_admin_user(current_user):
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))
    set_setting("mailersend_api_key", "")
    flash("MailerSend API key cleared.", "success")
    return redirect(url_for("admin_panel") + "#email-settings")


@app.route("/admin/email/test", methods=["POST"])
def admin_test_email():
    current_user = get_current_user()
    if not is_admin_user(current_user):
        return jsonify({"ok": False, "message": "Admin access required."}), 403
    status = check_mailersend_status()
    return jsonify(status)


@app.route("/request-account", methods=["GET", "POST"])
def request_account():
    if session.get("user_email"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = normalize_email(request.form.get("email", ""))
        name = request.form.get("name", "").strip()
        reason = request.form.get("reason", "").strip()

        if not email or "@" not in email:
            flash("Please enter a valid email address.", "error")
            return redirect(url_for("request_account"))

        if len(name) > 100:
            name = name[:100]
        if len(reason) > 500:
            reason = reason[:500]

        conn = get_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # Check if already an approved user
        cursor.execute("SELECT id FROM users WHERE LOWER(email) = %s AND is_verified = TRUE", (email,))
        if cursor.fetchone():
            conn.close()
            flash("An account with this email already exists. Please log in.", "success")
            return redirect(url_for("login"))

        # Check if there's already a pending request
        cursor.execute(
            "SELECT id FROM account_requests WHERE LOWER(email) = %s AND status = 'pending'",
            (email,)
        )
        if cursor.fetchone():
            conn.close()
            flash("You already have a pending request. You'll receive an email once it's reviewed.", "success")
            return redirect(url_for("request_account"))

        approval_token = generate_token()
        cursor.execute(
            "INSERT INTO account_requests (email, name, reason, approval_token) VALUES (%s, %s, %s, %s)",
            (email, name if name else None, reason if reason else None, approval_token)
        )
        conn.commit()
        conn.close()

        send_admin_request_notification(email, name, reason, approval_token, request.url_root)

        flash("Your request has been submitted! You'll receive an email once an admin approves your account.", "success")
        return redirect(url_for("request_account"))

    prefill_email = normalize_email(request.args.get("email", ""))
    requested_poll = None
    poll_id_arg = request.args.get("poll", "").strip()
    if poll_id_arg:
        conn = get_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT id, name FROM polls WHERE id = %s", (poll_id_arg,))
        requested_poll = cursor.fetchone()
        conn.close()

    return render_template(
        "request_account.html",
        prefill_email=prefill_email,
        requested_poll=requested_poll
    )


@app.route("/approve-request/<token>")
def approve_request_via_email(token):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute(
        "SELECT * FROM account_requests WHERE approval_token = %s AND status = 'pending'",
        (token,)
    )
    req = cursor.fetchone()

    if not req:
        conn.close()
        flash("This request has already been processed or the link is invalid.", "error")
        return redirect(url_for("login"))

    email = normalize_email(req["email"])

    # Mark request as approved
    cursor.execute(
        "UPDATE account_requests SET status = 'approved', reviewed_at = NOW(), reviewed_by = 'email' WHERE id = %s",
        (req["id"],)
    )

    cursor.execute("SELECT id FROM users WHERE LOWER(email) = %s", (email,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO users (email, display_name, role, tier, is_verified) VALUES (%s, %s, %s, %s, TRUE)",
            (email, req.get("name"), default_role_for_email(email), default_tier_for_email(email))
        )
    else:
        cursor.execute("UPDATE users SET is_verified = TRUE WHERE LOWER(email) = %s", (email,))

    magic_token = generate_token()
    cursor.execute(
        "INSERT INTO magic_links (email, token, poll_id, expires_at) VALUES (%s, %s, NULL, NOW() + INTERVAL '24 hours')",
        (email, magic_token)
    )
    conn.commit()
    conn.close()

    sent, error = send_magic_link_email(email, magic_token, None, request.url_root)

    if sent:
        flash(f"Account request for {email} approved. They've been sent a one-click sign-in link.", "success")
    else:
        flash(f"Account approved for {email}, but the sign-in email failed to send: {error}", "error")

    if session.get("user_email"):
        return redirect(url_for("admin_panel"))
    return redirect(url_for("login"))


@app.route("/admin/requests/<int:request_id>/approve", methods=["POST"])
def admin_approve_request(request_id):
    current_user = get_current_user()
    if not is_admin_user(current_user):
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("SELECT * FROM account_requests WHERE id = %s AND status = 'pending'", (request_id,))
    req = cursor.fetchone()

    if not req:
        conn.close()
        flash("Request not found or already processed.", "error")
        return redirect(url_for("admin_panel"))

    email = normalize_email(req["email"])

    cursor.execute(
        "UPDATE account_requests SET status = 'approved', reviewed_at = NOW(), reviewed_by = %s WHERE id = %s",
        (current_user["email"], request_id)
    )

    cursor.execute("SELECT id FROM users WHERE LOWER(email) = %s", (email,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO users (email, display_name, role, tier, is_verified) VALUES (%s, %s, %s, %s, TRUE)",
            (email, req.get("name"), default_role_for_email(email), default_tier_for_email(email))
        )
    else:
        cursor.execute("UPDATE users SET is_verified = TRUE WHERE LOWER(email) = %s", (email,))

    magic_token = generate_token()
    cursor.execute(
        "INSERT INTO magic_links (email, token, poll_id, expires_at) VALUES (%s, %s, NULL, NOW() + INTERVAL '24 hours')",
        (email, magic_token)
    )
    conn.commit()
    conn.close()

    sent, error = send_magic_link_email(email, magic_token, None, request.url_root)

    if sent:
        flash(f"Approved! Sign-in link sent to {email}.", "success")
    else:
        flash(f"Approved {email}, but the sign-in email failed to send: {error}", "error")

    return redirect(url_for("admin_panel"))


@app.route("/admin/requests/<int:request_id>/reject", methods=["POST"])
def admin_reject_request(request_id):
    current_user = get_current_user()
    if not is_admin_user(current_user):
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("SELECT id FROM account_requests WHERE id = %s AND status = 'pending'", (request_id,))
    if not cursor.fetchone():
        conn.close()
        flash("Request not found or already processed.", "error")
        return redirect(url_for("admin_panel"))

    cursor.execute(
        "UPDATE account_requests SET status = 'rejected', reviewed_at = NOW(), reviewed_by = %s WHERE id = %s",
        (current_user["email"], request_id)
    )
    conn.commit()
    conn.close()

    flash("Request rejected.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    user_email = session.get("user_email")
    
    if not user_email:
        flash("Please log in first", "error")
        return redirect(url_for("login"))
    
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()[:100]
        new_email = normalize_email(request.form.get("email", user_email))
        profile_picture = request.form.get("profile_picture", "").strip()
        current_email = normalize_email(user_email)

        if profile_picture and not re.match(r"^https?://", profile_picture, re.IGNORECASE):
            conn.close()
            flash("Profile picture must be an http(s) URL.", "error")
            return redirect(url_for("profile"))

        if not new_email:
            conn.close()
            flash("Email is required", "error")
            return redirect(url_for("profile"))

        if not is_allowed_email(new_email) and new_email != current_email:
            conn.close()
            flash("That email is not associated with an approved account.", "error")
            return redirect(url_for("profile"))

        if new_email != current_email:
            cursor.execute(
                "SELECT 1 FROM users WHERE LOWER(email) = %s AND LOWER(email) != %s",
                (new_email, current_email)
            )
            if cursor.fetchone():
                conn.close()
                flash("That email is already in use.", "error")
                return redirect(url_for("profile"))

            token = generate_token()

            cursor.execute(
                '''
                UPDATE users
                SET email = %s, display_name = %s, profile_picture = %s, is_verified = FALSE
                WHERE LOWER(email) = %s
                ''',
                (
                    new_email,
                    display_name if display_name else None,
                    profile_picture if profile_picture else None,
                    current_email
                )
            )
            cursor.execute(
                "UPDATE polls SET admin_email = %s WHERE LOWER(admin_email) = %s",
                (new_email, current_email)
            )
            cursor.execute(
                "UPDATE votes SET user_email = %s WHERE LOWER(user_email) = %s",
                (new_email, current_email)
            )
            cursor.execute(
                "INSERT INTO magic_links (email, token, poll_id, expires_at) VALUES (%s, %s, NULL, NOW() + INTERVAL '24 hours')",
                (new_email, token)
            )
            conn.commit()
            conn.close()

            sent, error = send_magic_link_email(new_email, token, None, request.url_root)
            session.pop("user_email", None)
            session.pop("poll_name", None)
            session.pop("poll_creator_email", None)

            if sent:
                flash("Email updated. Check your new inbox for a sign-in link.", "success")
            else:
                code, msg = classify_email_error(error)
                flash(f"Email updated, but we couldn't send a sign-in link ({code}). {msg} Try signing in.", "error")
                if error:
                    print(f"ERROR: Magic-link send failed for new email {new_email}: {error}")
            return redirect(url_for("login"))

        cursor.execute(
            "UPDATE users SET display_name = %s, profile_picture = %s WHERE LOWER(email) = %s",
            (
                display_name if display_name else None,
                profile_picture if profile_picture else None,
                current_email
            )
        )
        conn.commit()
        flash("Profile updated successfully!", "success")
    
    cursor.execute("SELECT email, display_name, profile_picture FROM users WHERE email = %s", (normalize_email(user_email),))
    user = cursor.fetchone()
    conn.close()
    
    normalized_email = normalize_email(user_email)
    default_name = EMAIL_TO_NAME.get(normalized_email, normalized_email.split('@')[0])
    
    return render_template("profile.html", user=user, default_name=default_name)


@app.route("/profile/upload-photo", methods=["POST"])
def upload_profile_photo():
    user_email = session.get("user_email")
    
    if not user_email:
        return jsonify({"error": "Not logged in"}), 401
    
    if "photo" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files["photo"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    original_filename = secure_filename(file.filename)
    if not original_filename:
        return jsonify({"error": "Invalid file name"}), 400
    
    allowed_extensions = {"png", "jpg", "jpeg", "gif", "webp"}
    ext = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else ""
    if ext not in allowed_extensions:
        return jsonify({"error": "Invalid file type. Please upload an image."}), 400
    
    try:
        file_data = file.read()
        if len(file_data) > MAX_UPLOAD_SIZE_BYTES:
            return jsonify({"error": "File is too large. Max size is 5MB."}), 400

        user_slug = secure_filename(user_email.replace("@", "_"))
        saved_filename = f"{user_slug}_{secrets.token_hex(8)}.{ext}"
        output_path = UPLOAD_DIR / saved_filename

        with open(output_path, "wb") as out_file:
            out_file.write(file_data)

        photo_url = url_for("static", filename=f"{UPLOAD_URL_PREFIX}/{saved_filename}", _external=True)
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET profile_picture = %s WHERE email = %s",
            (photo_url, user_email)
        )
        conn.commit()
        conn.close()
        
        return jsonify({"success": True, "url": photo_url})
    except Exception as e:
        print(f"ERROR uploading photo: {e}")
        return jsonify({"error": "Failed to upload photo. Please try again."}), 500


@app.route("/logout")
def logout():
    session.pop("user_email", None)
    session.pop("poll_name", None)
    session.pop("poll_creator_email", None)
    return redirect(url_for("login"))


# ── Marketing pages ──────────────────────────────────────────

@app.route("/how-it-works")
def marketing_how_it_works():
    return render_template("marketing_how_it_works.html")


@app.route("/who-its-for")
def marketing_who_its_for():
    return render_template("marketing_who_its_for.html")


@app.route("/pricing")
def marketing_pricing():
    return render_template("marketing_pricing.html")


@app.route("/why-us")
def marketing_why_us():
    return render_template("marketing_why_us.html")


@app.route("/contact", methods=["GET", "POST"])
def marketing_contact():
    if request.method == "POST":
        name = re.sub(r"[\r\n]+", " ", request.form.get("name", "")).strip()[:100]
        email = normalize_email(request.form.get("email", ""))
        subject = re.sub(r"[\r\n]+", " ", request.form.get("subject", "general")).strip()[:150]
        message = request.form.get("message", "").strip()[:5000]

        if not name or not email or "@" not in email or not message:
            flash("Please fill in all required fields.", "error")
            return redirect(url_for("marketing_contact"))

        html_body = f"""
            <h2>New Contact Form Submission</h2>
            <p><strong>Name:</strong> {escape(name)}</p>
            <p><strong>Email:</strong> {escape(email)}</p>
            <p><strong>Subject:</strong> {escape(subject)}</p>
            <hr>
            <p>{str(escape(message)).replace(chr(10), '<br>')}</p>
            <hr>
            <p style="color: #6b7280; font-size: 14px;">Sent from the Templo contact form at templobooker.com</p>
        """
        for admin_email in ADMIN_EMAILS:
            sent, error = send_email(
                admin_email,
                f"[Templo Contact] {subject} — from {name}",
                html_body,
                reply_to=email,
            )
            if not sent:
                print(f"ERROR: Failed to send contact form email to {admin_email}: {error}")

        flash("Message sent! We'll get back to you soon.", "success")
        return redirect(url_for("marketing_contact"))

    return render_template("marketing_contact.html")


@app.errorhandler(404)
def handle_not_found(e):
    return render_template(
        "error.html",
        code=404,
        title="Page not found",
        message="That page doesn't exist — the link may be old or mistyped.",
    ), 404


@app.errorhandler(500)
def handle_server_error(e):
    return render_template(
        "error.html",
        code=500,
        title="Something went wrong",
        message="We hit an unexpected error. Please try again in a moment.",
    ), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        debug=os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    )
