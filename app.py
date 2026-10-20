import csv
import io
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from moss_ai import MossAIError, MossAssistant


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("BIMS_DATABASE", BASE_DIR / "instance" / "bims.sqlite3"))
ROLES = {"admin", "official", "resident"}
REQUEST_TYPES = {
    "Barangay clearance",
    "Certificate of residency",
    "Certificate of indigency",
    "Other",
}
REQUEST_STATUSES = {"Pending", "Processing", "Ready for pickup", "Completed", "Declined"}
INCIDENT_CATEGORIES = {
    "streetlights",
    "flooding",
    "garbage",
    "water",
    "electricity",
    "roads",
    "security",
    "other",
}
INCIDENT_STATUSES = {"open", "in_progress", "resolved", "closed"}
REQUEST_TYPE_ALIASES = {
    "Barangay clearance": ("barangay clearance", "clearance"),
    "Certificate of residency": (
        "certificate of residency",
        "certificate of residence",
        "residency certificate",
        "proof of residency",
        "proof of residence",
    ),
    "Certificate of indigency": (
        "certificate of indigency",
        "indigency certificate",
        "certificate of poverty",
    ),
}
REQUEST_INTENTS = (
    "request",
    "apply",
    "need",
    "want",
    "get me",
    "get a",
    "get an",
    "help me get",
    "submit",
    "i'd like",
    "i would like",
    "can i get",
    "can you get",
    "kuha",
    "kailangan",
)
CONFIRMATION_WORDS = {"yes", "yes please", "confirm", "confirmed", "submit", "sure", "oo"}
CANCELLATION_WORDS = {"no", "no thanks", "cancel", "stop", "never mind", "nevermind", "hindi"}
CERTIFICATE_PURPOSE_SUGGESTIONS = (
    "For work",
    "For school",
    "For financial assistance",
    "For personal use",
)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("BIMS_SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("BIMS_COOKIE_SECURE") == "1",
)


@contextmanager
def get_db():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db():
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'official', 'resident')),
                age INTEGER,
                phone TEXT NOT NULL DEFAULT '',
                purok TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS service_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resident_id INTEGER NOT NULL REFERENCES users(id),
                request_type TEXT NOT NULL,
                purpose TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending',
                official_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                is_pinned INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS request_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL REFERENCES service_requests(id) ON DELETE CASCADE,
                actor_id INTEGER REFERENCES users(id),
                old_status TEXT NOT NULL DEFAULT '',
                new_status TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER REFERENCES users(id),
                action TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        # Lightweight migration for databases created before these tables existed.
        for stmt in (
            "ALTER TABLE service_requests ADD COLUMN pickup_note TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE users ADD COLUMN birthday TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE users ADD COLUMN sex TEXT NOT NULL DEFAULT ''",
        ):
            try:
                db.execute(stmt)
            except sqlite3.OperationalError:
                pass

        # New tables for Moss AI features
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS faqs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'other',
                status TEXT NOT NULL DEFAULT 'open',
                resident_id INTEGER REFERENCES users(id),
                location TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                type TEXT NOT NULL DEFAULT 'info',
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                link TEXT NOT NULL DEFAULT '',
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS blotters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blotter_number TEXT NOT NULL UNIQUE,
                complainant_name TEXT NOT NULL,
                complainant_address TEXT NOT NULL DEFAULT '',
                person_involved TEXT NOT NULL DEFAULT '',
                nature_of_complaint TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                date_time_incident TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                action_taken TEXT NOT NULL DEFAULT '',
                reported_by INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS moss_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def log_activity(actor_id, action, details=""):
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO activity_log (actor_id, action, details) VALUES (?, ?, ?)",
                (actor_id, action, (details or "")[:500]),
            )
    except sqlite3.Error:
        pass


def create_notification(user_id, notif_type, title, message, link=""):
    try:
        with get_db() as db:
            db.execute(
                """INSERT INTO notifications (user_id, type, title, message, link)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, notif_type, title, message, link),
            )
    except sqlite3.Error:
        pass


def notify_all_residents(title, message, link=""):
    """Send a notification to all active residents."""
    try:
        with get_db() as db:
            resident_ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM users WHERE role = 'resident' AND active = 1"
                ).fetchall()
            ]
            for rid in resident_ids:
                db.execute(
                    """INSERT INTO notifications (user_id, type, title, message, link)
                       VALUES (?, 'announcement', ?, ?, ?)""",
                    (rid, title, message, link),
                )
    except sqlite3.Error:
        pass


def log_request_event(request_id, actor_id, old_status, new_status, note=""):
    with get_db() as db:
        db.execute(
            """INSERT INTO request_events (request_id, actor_id, old_status, new_status, note)
               VALUES (?, ?, ?, ?, ?)""",
            (request_id, actor_id, old_status or "", new_status or "", (note or "")[:500]),
        )


# Moss keeps its conversation in the database rather than in the session cookie,
# so the AI gets a usable context window without bloating the cookie.
MOSS_HISTORY_ROWS = 40
MOSS_RATE_LIMIT = {"per_minute": 10, "per_hour": 60}


def append_assistant_exchange(user_id, user_message, reply):
    """Store one Moss chat exchange and keep only the most recent turns."""
    with get_db() as db:
        db.execute(
            "INSERT INTO moss_messages (user_id, role, content) VALUES (?, 'user', ?)",
            (user_id, (user_message or "")[:1000]),
        )
        db.execute(
            "INSERT INTO moss_messages (user_id, role, content) VALUES (?, 'assistant', ?)",
            (user_id, (reply or "")[:4000]),
        )
        db.execute(
            """DELETE FROM moss_messages WHERE user_id = ? AND id NOT IN (
                   SELECT id FROM moss_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?)""",
            (user_id, user_id, MOSS_HISTORY_ROWS),
        )


def get_moss_history(user_id, limit=16):
    """Return the recent Moss conversation in chat-completion message format."""
    with get_db() as db:
        rows = db.execute(
            "SELECT role, content FROM moss_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    history = []
    for row in reversed(rows):
        content = (row["content"] or "").strip()
        if not content:
            continue
        history.append(
            {"role": "user" if row["role"] == "user" else "assistant", "content": content}
        )
    return history


def get_moss_display_messages(user_id, limit=12):
    """Return the recent Moss conversation shaped for the chat widget."""
    return [
        {
            "speaker": "You" if entry["role"] == "user" else "Moss",
            "text": entry["content"],
        }
        for entry in get_moss_history(user_id, limit)
    ]


def clear_assistant_history(user_id):
    with get_db() as db:
        db.execute("DELETE FROM moss_messages WHERE user_id = ?", (user_id,))


def moss_rate_limited():
    """Throttle assistant messages so a single user cannot burn through the API."""
    now = time.time()
    counters = session.get("moss_rate") or {}
    minute = counters.get("minute") or {}
    hour = counters.get("hour") or {}
    if now - minute.get("start", 0) > 60:
        minute = {"start": now, "count": 0}
    if now - hour.get("start", 0) > 3600:
        hour = {"start": now, "count": 0}
    if (
        minute.get("count", 0) >= MOSS_RATE_LIMIT["per_minute"]
        or hour.get("count", 0) >= MOSS_RATE_LIMIT["per_hour"]
    ):
        session["moss_rate"] = {"minute": minute, "hour": hour}
        return True
    minute["count"] = minute.get("count", 0) + 1
    hour["count"] = hour.get("count", 0) + 1
    session["moss_rate"] = {"minute": minute, "hour": hour}
    return False


def latest_announcements(limit=5):
    with get_db() as db:
        return db.execute(
            """SELECT announcements.*, users.full_name AS author_name
               FROM announcements LEFT JOIN users ON users.id = announcements.created_by
               ORDER BY is_pinned DESC, created_at DESC, id DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def request_status_counts(resident_id=None):
    with get_db() as db:
        if resident_id is not None:
            rows = db.execute(
                """SELECT status, COUNT(*) AS total FROM service_requests
                   WHERE resident_id = ? GROUP BY status""",
                (resident_id,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT status, COUNT(*) AS total FROM service_requests GROUP BY status"
            ).fetchall()
    counts = {status: 0 for status in REQUEST_STATUSES}
    for row in rows:
        counts[row["status"]] = row["total"]
    return counts


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


def get_notification_count(user):
    if not user:
        return 0
    with get_db() as db:
        if user["role"] == "resident":
            count = db.execute(
                """SELECT COUNT(*) FROM notifications
                   WHERE user_id = ? AND is_read = 0""",
                (user["id"],),
            ).fetchone()[0]
        else:
            # Officials and admins see new requests and incidents needing attention
            count = db.execute(
                """SELECT COUNT(*) FROM service_requests WHERE status = 'Pending'
                   UNION ALL
                   SELECT COUNT(*) FROM incidents WHERE status = 'open'"""
            ).fetchone()[0]
            # The UNION ALL returns one column with two rows, so sum them
            # Actually, simpler: count pending requests + open incidents
            count_req = db.execute(
                "SELECT COUNT(*) FROM service_requests WHERE status = 'Pending'"
            ).fetchone()[0]
            count_inc = db.execute(
                "SELECT COUNT(*) FROM incidents WHERE status = 'open'"
            ).fetchone()[0]
            count = count_req + count_inc
    return count


def get_recent_notifications(user, limit=5):
    if not user:
        return []
    with get_db() as db:
        if user["role"] == "resident":
            return db.execute(
                """SELECT * FROM notifications
                   WHERE user_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (user["id"], limit),
            ).fetchall()
        else:
            # Officials/admins see pending requests and open incidents
            items = []
            reqs = db.execute(
                """SELECT id, request_type AS title, created_at, resident_id
                   FROM service_requests
                   WHERE status = 'Pending'
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            for r in reqs:
                items.append({
                    "id": r["id"],
                    "type": "request",
                    "title": f"New request: {r['request_type']}",
                    "message": "A resident submitted a document request.",
                    "link": url_for("request_detail", request_id=r["id"]),
                    "created_at": r["created_at"],
                    "is_read": 0,
                    "resident_name": None,
                })
            incs = db.execute(
                """SELECT id, title, created_at, resident_id
                   FROM incidents
                   WHERE status = 'open'
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            for r in incs:
                items.append({
                    "id": r["id"],
                    "type": "incident",
                    "title": f"New concern: {r['title'][:60]}",
                    "message": "A resident reported an incident.",
                    "link": url_for("incident_detail", incident_id=r["id"]),
                    "created_at": r["created_at"],
                    "is_read": 0,
                    "resident_name": None,
                })
            items.sort(key=lambda x: x["created_at"] or "", reverse=True)
            return items[:limit]


@app.context_processor
def navigation_context():
    pages = {
        "dashboard": (
            "Overview",
            "Your community services at a glance.",
        ),
        "requests_page": (
            "Service requests",
            "Review and manage barangay service requests.",
        ),
        "request_detail": (
            "Request details",
            "Track the progress and history of a service request.",
        ),
        "residents_page": (
            "Residents",
            "Browse resident accounts and community contact details.",
        ),
        "accounts_page": (
            "User accounts",
            "Create and manage access to the barangay portal.",
        ),
        "announcements_page": (
            "Announcements",
            "Community notices and barangay updates.",
        ),
        "reports_page": (
            "Reports",
            "Service workload, request trends, and community stats.",
        ),
        "activity_page": (
            "Activity log",
            "Recent actions across the barangay portal.",
        ),
        "profile_page": (
            "My profile",
            "Review and update your personal details.",
        ),
        "change_password": (
            "Change password",
            "Update your account password to keep it secure.",
        ),
        "developers_page": (
            "Developers",
            "Meet the team behind the Barangay Portal.",
        ),
    }
    page_title, page_description = pages.get(
        request.endpoint,
        ("Barangay Portal", "Barangay Information Management System."),
    )
    user = current_user()
    pending = session.get("pending_service_request")
    moss_ai = moss.mode()
    moss_suggestions = []
    if moss_ai["mode"] == "ai":
        if session.get("moss_pending_action"):
            moss_suggestions = ["Yes, go ahead", "Cancel that"]
        elif user and user["role"] == "resident":
            if session.get("moss_awaiting_profile"):
                moss_suggestions = ["Complete my profile"]
            else:
                moss_suggestions = [
                    "I need a certificate",
                    "What is my request status?",
                    "I have a concern",
                ]
        elif user:
            moss_suggestions = [
                "How many pending requests are there?",
                "Any announcements?",
                "Show me the open concerns",
            ]
    elif isinstance(pending, dict) and pending.get("resident_id") == session.get("user_id"):
        if pending.get("stage") == "choose_type":
            moss_suggestions = sorted(REQUEST_TYPES - {"Other"})
        elif pending.get("stage") == "purpose":
            moss_suggestions = list(CERTIFICATE_PURPOSE_SUGGESTIONS)
        elif pending.get("stage") == "confirm":
            moss_suggestions = ["Yes, submit it", "Cancel request"]
    elif session.get("user_role") == "resident":
        if session.get("moss_awaiting_profile"):
            moss_suggestions = ["Complete my profile"]
        else:
            moss_suggestions = ["I need a certificate", "I have a concern"]
    elif session.get("user_role") in ("admin", "official"):
        moss_suggestions = ["View incidents", "Manage FAQs"]
    return {
        "page_title": page_title,
        "page_description": page_description,
        "notification_count": get_notification_count(user),
        "recent_notifications": get_recent_notifications(user) if user and user["role"] == "resident" else [],
        "moss_ai_mode": moss_ai["mode"],
        "moss_ai_detail": moss_ai["detail"],
        "assistant_messages": get_moss_display_messages(user["id"], 12) if user else [],
        "active_page": {
            "dashboard": "dashboard",
            "requests_page": "requests",
            "request_detail": "requests",
            "residents_page": "residents",
            "accounts_page": "accounts",
            "announcements_page": "announcements",
            "reports_page": "reports",
            "activity_page": "activity",
            "profile_page": "profile",
            "change_password": "profile",
            "developers_page": "profile",
            "incidents_page": "incidents",
            "incident_detail": "incidents",
            "faqs_page": "incidents",
            "blotters_page": "blotters",
            "blotter_detail": "blotters",
        }.get(request.endpoint, ""),
        "moss_suggestions": moss_suggestions,
    }


@app.before_request
def protect_forms():
    if request.method == "POST":
        submitted_token = request.form.get("csrf_token", "")
        if not submitted_token or not secrets.compare_digest(
            submitted_token, session.get("csrf_token", "")
        ):
            abort(400, description="The form session expired. Please go back and try again.")


def current_user():
    user_id = session.get("user_id")
    if user_id is None:
        return None
    with get_db() as db:
        user = db.execute(
            "SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)
        ).fetchone()
    if user is not None:
        session["user_role"] = user["role"]
    return user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            session.clear()
            flash("Please sign in to continue.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def roles_required(*roles):
    def decorate(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user()["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorate


def valid_profile(full_name, username, password, role):
    if not full_name or len(full_name) > 120:
        return "Enter a name of up to 120 characters."
    if not username or len(username) < 3 or len(username) > 40:
        return "Usernames must be between 3 and 40 characters."
    if role not in ROLES:
        return "Choose a valid account role."
    if password is not None and len(password) < 8:
        return "Passwords must be at least 8 characters."
    return None


def find_faq_answer(message):
    text = " ".join(message.casefold().split())
    with get_db() as db:
        faqs = db.execute(
            "SELECT question, answer FROM faqs WHERE is_active = 1"
        ).fetchall()
    for faq in faqs:
        q_words = set(faq["question"].casefold().split())
        if q_words and q_words.issubset(set(text.split())):
            return faq["answer"]
    return None


def handle_incident_report(message, user):
    if not profile_is_complete(user):
        return (
            "Before I can log an incident report, please complete your profile first. "
            "Go to My profile and fill in your age, phone, purok, address, birthday, and sex. "
            "Once your profile is complete, I'll be happy to help you report an incident."
        )
    text = " ".join(message.casefold().split())
    incident_keywords = ("report", "issue", "problem", "emergency", "incident", "concern",
                         "streetlight", "flood", "flooded", "road", "garbage",
                         "water", "electricity", "security", "light")
    if not any(kw in text for kw in incident_keywords):
        return None
    # Extract a simple title from the message
    title = text[:80]
    category = "other"
    for cat in ("streetlights", "flooding", "garbage", "water", "electricity", "roads", "security"):
        if cat in text:
            category = cat
            break
    with get_db() as db:
        db.execute(
            """INSERT INTO incidents (title, description, category, resident_id)
               VALUES (?, ?, ?, ?)""",
            (title, message[:1000], category, user["id"]),
        )
        incident_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    log_activity(user["id"], "create_incident_via_moss", f"Incident #{incident_id} via Moss")
    return (
        f"Thank you! I've logged your report (reference #{incident_id:04d}) "
        f"as a {category} issue. Barangay officials will review it and update the status. "
        "You can check your reports on the Incidents page."
    )


def profile_is_complete(user):
    """Return True if all required profile fields are filled in."""
    required = ("full_name", "age", "phone", "purok", "address", "birthday", "sex")
    try:
        for field in required:
            value = user[field]
            if value is None or str(value).strip() == "":
                return False
        return True
    except (KeyError, IndexError, sqlite3.OperationalError):
        # Columns may not exist yet if migration hasn't run
        return False


moss = MossAssistant(
    get_db=get_db,
    log_activity=log_activity,
    create_notification=create_notification,
    log_request_event=log_request_event,
    build_url=url_for,
    profile_is_complete=profile_is_complete,
    request_types=REQUEST_TYPES,
    request_statuses=REQUEST_STATUSES,
    incident_categories=INCIDENT_CATEGORIES,
    incident_statuses=INCIDENT_STATUSES,
    confirmation_words=CONFIRMATION_WORDS,
    cancellation_words=CANCELLATION_WORDS,
)


def assistant_reply(message, user):
    text = " ".join(message.casefold().split())
    # 1. Check FAQs first
    faq_answer = find_faq_answer(message)
    if faq_answer:
        return faq_answer
    # 2. Check incident reporting (residents only)
    if user["role"] == "resident":
        incident_reply = handle_incident_report(message, user)
        if incident_reply:
            return incident_reply
    if any(word in text for word in ("hello", "hi", "good morning", "good afternoon", "kumusta")):
        return f"Hello, {user['full_name'].split()[0]}! I can help with barangay services, document requests, announcements, reports, and portal questions."
    if any(word in text for word in ("announce", "notice", "update", "balita", "abiso")):
        with get_db() as db:
            row = db.execute(
                "SELECT title, created_at FROM announcements ORDER BY is_pinned DESC, created_at DESC, id DESC LIMIT 3"
            ).fetchall()
        if not row:
            return "There are no announcements right now. Check the Announcements page later for community notices."
        items = "; ".join(f"{r['title']} ({r['created_at'][:10]})" for r in row)
        return f"Latest announcements: {items}. Open the Announcements page to read the full details."
    if any(word in text for word in ("report", "statistic", "trend", "summary", "analytics")):
        if user["role"] == "resident":
            return "Reports are available to barangay staff. As a resident, you can track your own requests on the Service requests page or open a request to see its history."
        return "Open the Reports page for request totals by status, type, purok, and monthly trend, plus recent staff activity."
    if any(word in text for word in ("profile", "change password", "update my", "my details", "phone", "address")):
        return "Open My profile from the sidebar to update your phone, purok, address, and age, or to change your password."
    if any(word in text for word in ("cancel", "withdraw my request")):
        if user["role"] == "resident":
            return "You can cancel a Pending or Processing request from its detail page (open My requests, then click View). Cancelled requests are marked as Declined."
        return "Residents can cancel their own Pending or Processing requests. Staff can update or remove requests from the Service requests page."
    if any(word in text for word in ("print", "certificate copy", "download", "export", "csv")):
        if user["role"] == "resident":
            return "Open any of your requests and use Print / certificate preview to print a reference copy. The barangay office still issues the official signed certificate."
        return "Staff can export service requests or residents as CSV, and print a certificate preview from any request detail page."
    if any(word in text for word in ("clearance", "certificate", "document", "papel", "sertipiko")):
        services = ", ".join(sorted(REQUEST_TYPES))
        if user["role"] != "resident":
            return (
                f"Resident service requests include: {services}. "
                "Residents can ask Moss to prepare a request; Moss will ask for their "
                "permission before submitting it. Staff can review submitted requests "
                "on the Service requests page."
            )
        return (
            f"Available request types are: {services}. "
            "As a resident, you can tell me which certificate you need and its purpose. "
            "I'll show you the request details and ask your permission before submitting. "
            "The barangay office still reviews and issues certificates."
        )
    if any(word in text for word in ("status", "progress", "request ko", "my request", "follow up")):
        if user["role"] == "resident":
            with get_db() as db:
                items = db.execute(
                    """SELECT request_type, status, official_note, created_at
                       FROM service_requests WHERE resident_id = ?
                       ORDER BY created_at DESC, id DESC LIMIT 5""",
                    (user["id"],),
                ).fetchall()
            if not items:
                return "You don't have any document requests yet. Submit one from the Request a document form and its status will appear under My requests."
            summaries = [
                f"{item['request_type']}: {item['status']}"
                + (f" — {item['official_note']}" if item["official_note"] else "")
                for item in items
            ]
            return "Here are your latest requests: " + "; ".join(summaries) + "."
        return "Open the Service requests page to review current statuses and add an office note."
    if any(word in text for word in ("account", "password", "login", "sign in", "role", "access")):
        if user["role"] == "admin":
            return "Use User accounts to create resident or barangay official accounts, or deactivate/reactivate non-admin accounts. Residents can also register through the sign-in page."
        return "Sign in with your username and password. Resident self-registration is available from the sign-in page. For an account or password issue, contact a barangay administrator."
    if any(word in text for word in ("resident", "population", "residents")):
        if user["role"] == "resident":
            return "For privacy, resident directory information is available only to authorized barangay officials and administrators."
        with get_db() as db:
            total = db.execute(
                "SELECT COUNT(*) AS total FROM users WHERE role = 'resident' AND active = 1"
            ).fetchone()["total"]
        return f"There are {total} active resident accounts. Open the Residents page to view the directory."
    if any(word in text for word in ("help", "what can", "how do", "paano", "services")):
        return (
            "I can explain how to request barangay documents, check your request status, "
            "read announcements, update your profile, and use portal accounts. Try asking "
            "“What documents can I request?”, “What is my request status?”, “Any announcements?”, "
            "or “How do I update my profile?”"
        )
    return (
        "I don't have an answer for that yet. I can help with document requests, request status, "
        "announcements, profile updates, reports, resident accounts, and portal access. "
        "For official advice, please contact your barangay office."
    )


def find_request_type(message):
    normalized = message.casefold()
    matches = []
    for request_type, aliases in REQUEST_TYPE_ALIASES.items():
        for alias in aliases:
            if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized):
                matches.append((len(alias), request_type))
    if not matches:
        return None
    return max(matches)[1]


def extract_request_purpose(message):
    match = re.search(r"\bfor\s+(.+?)(?:[.!?]|$)", message, re.IGNORECASE)
    if not match:
        return ""
    purpose = match.group(1).strip(" \t,;:")
    purpose = re.sub(r"\bplease\s+submit\s*$", "", purpose, flags=re.IGNORECASE).strip()
    return purpose[:500]


def assistant_request_action(message, user):
    normalized = " ".join(message.casefold().split())
    pending = session.get("pending_service_request")
    if not isinstance(pending, dict) or pending.get("resident_id") != user["id"]:
        pending = None
        session.pop("pending_service_request", None)

    if pending:
        if pending.get("type") == "incident":
            if normalized in CANCELLATION_WORDS or normalized.startswith(("cancel ", "stop ")):
                session.pop("pending_service_request", None)
                return "Okay, I cancelled that. I haven't submitted a report."

            if pending.get("stage") == "describe":
                pending["description"] = message.strip()[:1000]
                pending["stage"] = "location"

            if pending.get("stage") == "location":
                pending["location"] = message.strip()[:200]
                pending["stage"] = "confirm"

            if pending.get("stage") == "confirm":
                if normalized in CONFIRMATION_WORDS or normalized.startswith(
                    ("yes, ", "yes ", "confirm ", "submit ", "oo ")
                ):
                    category = pending.get("category", "other")
                    with get_db() as db:
                        db.execute(
                            """INSERT INTO incidents (title, description, category, location, resident_id)
                               VALUES (?, ?, ?, ?, ?)""",
                            (
                                pending["description"][:80],
                                pending["description"],
                                category,
                                pending.get("location", ""),
                                user["id"],
                            ),
                        )
                        incident_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                    session.pop("pending_service_request", None)
                    log_activity(user["id"], "create_incident_via_moss", f"Incident #{incident_id} via Moss")
                    return (
                        f"Thank you! I've logged your concern (reference #{incident_id:04d}) "
                        f"as a {category} issue. Barangay officials will review it and update the status. "
                        "You can check your reports on the Incidents page."
                    )
                return (
                    f"Please confirm: may I submit this concern? "
                    f"Category: {pending.get('category', 'other').replace('_', ' ').title()}. "
                    f"Description: {pending['description'][:80]}. "
                    f"{'Location: ' + pending.get('location', '') if pending.get('location') else ''} "
                    "Reply “yes” to submit or “cancel” to stop."
                )

            session["pending_service_request"] = pending
            if pending["stage"] == "describe":
                return (
                    "Please describe your concern in detail (e.g., “Streetlight on Main Street "
                    "has been out for 3 days”)."
                )
            if pending["stage"] == "location":
                return (
                    "Where is this happening? (e.g., street name, purok, landmark)"
                )

        if normalized in CANCELLATION_WORDS or normalized.startswith(("cancel ", "stop ")):
            session.pop("pending_service_request", None)
            return "Okay, I cancelled that. I haven't submitted a request."

        if pending.get("stage") == "choose_type":
            request_type = find_request_type(message)
            if request_type is None:
                return (
                    "Please choose Barangay clearance, Certificate of residency, "
                    "or Certificate of indigency. I won't submit anything until you confirm."
                )
            pending["request_type"] = request_type
            pending["purpose"] = extract_request_purpose(message)
            pending["stage"] = "pickup" if pending["purpose"] else "purpose"

        elif pending.get("stage") == "purpose":
            purpose = extract_request_purpose(message) or message.strip()
            if len(purpose) > 500:
                return "Please keep the purpose to 500 characters or fewer."
            pending["purpose"] = purpose
            pending["stage"] = "pickup"

        elif pending.get("stage") == "pickup":
            pending["pickup_preference"] = message.strip()[:200]
            pending["stage"] = "confirm"

        if pending.get("stage") == "confirm":
            if normalized in CONFIRMATION_WORDS or normalized.startswith(
                ("yes, ", "yes ", "confirm ", "submit ", "oo ")
            ):
                with get_db() as db:
                    cursor = db.execute(
                        """INSERT INTO service_requests (resident_id, request_type, purpose, pickup_note)
                           VALUES (?, ?, ?, ?)""",
                        (
                            user["id"],
                            pending["request_type"],
                            pending["purpose"],
                            pending.get("pickup_preference", ""),
                        ),
                    )
                    request_id = cursor.lastrowid
                session.pop("pending_service_request", None)
                try:
                    log_request_event(request_id, user["id"], "", "Pending", "Submitted via Moss")
                except sqlite3.Error:
                    pass
                pickup_line = ""
                if pending.get("pickup_preference"):
                    pickup_line = f" Preferred pickup: {pending['pickup_preference']}."
                return (
                    f"Done. I submitted your {pending['request_type']} request "
                    f"(reference #{request_id:04d}) for {pending['purpose']}.{pickup_line} "
                    "It is now pending review by the barangay office. Moss cannot approve "
                    "or issue the certificate; the office will process it and update its status."
                )
            return (
                f"Please confirm: may I submit a {pending['request_type']} request "
                f"for {pending['purpose']}? Reply “yes” to submit or “cancel” to stop. "
                "The barangay office must still review and issue the certificate."
            )

        session["pending_service_request"] = pending
        if pending["stage"] == "purpose":
            return (
                f"What do you need the {pending['request_type']} for? "
                "I will ask your permission before submitting anything."
            )
        if pending["stage"] == "pickup":
            return (
                f"When would you like to pick up your {pending['request_type']}? "
                "Please tell me your preferred date or time (e.g., “tomorrow morning” or “next Friday”)."
            )

    if user["role"] != "resident":
        return None

    if not profile_is_complete(user):
        session["moss_awaiting_profile"] = True
        return (
            "Please complete your profile first before I can help you with requests or concerns. "
            "Fill in your age, phone, purok, address, birthday, and sex in My profile."
        )

    if normalized == "i have a concern":
        session["pending_service_request"] = {
            "resident_id": user["id"],
            "type": "incident",
            "stage": "describe",
        }
        return (
            "I can help you report a concern to the barangay office. "
            "Please describe your concern in detail (e.g., “Streetlight on Main Street "
            "has been out for 3 days”)."
        )

    has_request_intent = any(phrase in normalized for phrase in REQUEST_INTENTS)
    if not has_request_intent:
        return None

    request_type = find_request_type(message)
    if request_type is None:
        pending = {"resident_id": user["id"], "stage": "choose_type"}
        session["pending_service_request"] = pending
        return (
            "I can start a request for Barangay clearance, Certificate of residency, "
            "or Certificate of indigency. Which one would you like? I will ask your "
            "permission before submitting it."
        )

    purpose = extract_request_purpose(message)
    pending = {
        "resident_id": user["id"],
        "stage": "confirm" if purpose else "purpose",
        "request_type": request_type,
        "purpose": purpose,
    }
    session["pending_service_request"] = pending
    if not purpose:
        return (
            f"What do you need the {request_type} for? "
            "I will ask your permission before submitting anything."
        )
    return (
        f"I can submit a {request_type} request for {purpose}. "
        "May I submit it now? Reply “yes” to submit or “cancel” to stop. "
        "The barangay office must still review and issue the certificate."
    )


@app.route("/")
def home():
    if current_user():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with get_db() as db:
            user = db.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE AND active = 1",
                (username,),
            ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["moss_boot_id"] = secrets.token_urlsafe(16)
            flash(f"Welcome back, {user['full_name']}.", "success")
            log_activity(user["id"], "sign_in", f"{user['username']} signed in")
            return redirect(url_for("dashboard"))
        flash("That username and password could not be verified.", "error")
    return render_template("index.html", page="login", user=None)


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        age_text = request.form.get("age", "").strip()
        phone = request.form.get("phone", "").strip()
        purok = request.form.get("purok", "").strip()
        address = request.form.get("address", "").strip()

        error = valid_profile(full_name, username, password, "resident")
        age = None
        if age_text:
            try:
                age = int(age_text)
            except ValueError:
                error = "Age must be a whole number."
            if age is not None and not 1 <= age <= 120:
                error = "Enter an age between 1 and 120."
        if error:
            flash(error, "error")
        else:
            try:
                with get_db() as db:
                    db.execute(
                        """INSERT INTO users
                           (username, password_hash, full_name, role, age, phone, purok, address)
                           VALUES (?, ?, ?, 'resident', ?, ?, ?, ?)""",
                        (
                            username,
                            generate_password_hash(password),
                            full_name,
                            age,
                            phone[:40],
                            purok[:80],
                            address[:200],
                        ),
                    )
                flash("Your resident account is ready. Please sign in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("That username is already in use.", "error")
    return render_template("index.html", page="register", user=None)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    with get_db() as db:
        if user["role"] == "resident":
            counts = {
                "my_requests": db.execute(
                    "SELECT COUNT(*) FROM service_requests WHERE resident_id = ?",
                    (user["id"],),
                ).fetchone()[0],
                "pending": db.execute(
                    """SELECT COUNT(*) FROM service_requests
                       WHERE resident_id = ? AND status IN ('Pending', 'Processing')""",
                    (user["id"],),
                ).fetchone()[0],
                "ready": db.execute(
                    """SELECT COUNT(*) FROM service_requests
                       WHERE resident_id = ? AND status = 'Ready for pickup'""",
                    (user["id"],),
                ).fetchone()[0],
            }
            recent_requests = db.execute(
                """SELECT * FROM service_requests WHERE resident_id = ?
                   ORDER BY created_at DESC, id DESC LIMIT 5""",
                (user["id"],),
            ).fetchall()
            status_counts = request_status_counts(user["id"])
        else:
            counts = {
                "residents": db.execute(
                    "SELECT COUNT(*) FROM users WHERE role = 'resident' AND active = 1"
                ).fetchone()[0],
                "requests": db.execute(
                    "SELECT COUNT(*) FROM service_requests"
                ).fetchone()[0],
                "pending": db.execute(
                    "SELECT COUNT(*) FROM service_requests WHERE status = 'Pending'"
                ).fetchone()[0],
                "announcements": db.execute(
                    "SELECT COUNT(*) FROM announcements"
                ).fetchone()[0],
            }
            recent_requests = db.execute(
                """SELECT service_requests.*, users.full_name AS resident_name
                   FROM service_requests JOIN users ON users.id = service_requests.resident_id
                   ORDER BY service_requests.created_at DESC, service_requests.id DESC LIMIT 6"""
            ).fetchall()
            status_counts = request_status_counts()
        announcements = db.execute(
            """SELECT announcements.*, users.full_name AS author_name
               FROM announcements LEFT JOIN users ON users.id = announcements.created_by
               ORDER BY is_pinned DESC, created_at DESC, id DESC LIMIT 4"""
        ).fetchall()
    return render_template(
        "dashboard.html",
        user=user,
        counts=counts,
        recent_requests=recent_requests,
        status_counts=status_counts,
        announcements=announcements,
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/requests")
@roles_required("admin", "official", "resident")
def requests_page():
    user = current_user()
    search = request.args.get("search", "").strip()[:80]
    status_filter = request.args.get("status", "").strip()
    type_filter = request.args.get("type", "").strip()
    if status_filter not in REQUEST_STATUSES:
        status_filter = ""
    if type_filter not in REQUEST_TYPES:
        type_filter = ""
    with get_db() as db:
        if user["role"] == "resident":
            query = "SELECT * FROM service_requests WHERE resident_id = ?"
            params = [user["id"]]
            if search:
                query += " AND (request_type LIKE ? OR purpose LIKE ? OR official_note LIKE ?)"
                like = f"%{search}%"
                params.extend([like, like, like])
            if status_filter:
                query += " AND status = ?"
                params.append(status_filter)
            if type_filter:
                query += " AND request_type = ?"
                params.append(type_filter)
            query += " ORDER BY created_at DESC, id DESC"
            requests_list = db.execute(query, params).fetchall()
        else:
            query = """SELECT service_requests.*, users.full_name AS resident_name,
                              users.purok AS resident_purok
                       FROM service_requests JOIN users ON users.id = service_requests.resident_id
                       WHERE 1 = 1"""
            params = []
            if search:
                query += " AND (users.full_name LIKE ? OR service_requests.request_type LIKE ? OR service_requests.purpose LIKE ?)"
                like = f"%{search}%"
                params.extend([like, like, like])
            if status_filter:
                query += " AND service_requests.status = ?"
                params.append(status_filter)
            if type_filter:
                query += " AND service_requests.request_type = ?"
                params.append(type_filter)
            query += """ ORDER BY CASE service_requests.status WHEN 'Pending' THEN 0 WHEN 'Processing' THEN 1
                             WHEN 'Ready for pickup' THEN 2 ELSE 3 END,
                            service_requests.created_at DESC, service_requests.id DESC"""
            requests_list = db.execute(query, params).fetchall()
    return render_template(
        "requests.html",
        user=user,
        requests_list=requests_list,
        request_types=sorted(REQUEST_TYPES),
        statuses=sorted(REQUEST_STATUSES),
        search=search,
        status_filter=status_filter,
        type_filter=type_filter,
        today=date.today().strftime("%A, %B %d, %Y"),
        page_title="My requests" if user["role"] == "resident" else "Service requests",
        page_description=(
            "Submit a request and keep track of its progress."
            if user["role"] == "resident"
            else "Review submissions and update request statuses."
        ),
    )


@app.route("/requests/<int:request_id>")
@roles_required("admin", "official", "resident")
def request_detail(request_id):
    user = current_user()
    with get_db() as db:
        item = db.execute(
            """SELECT service_requests.*, users.full_name AS resident_name, users.purok AS resident_purok,
                      users.phone AS resident_phone, users.address AS resident_address
               FROM service_requests JOIN users ON users.id = service_requests.resident_id
               WHERE service_requests.id = ?""",
            (request_id,),
        ).fetchone()
        if item is None:
            abort(404)
        if user["role"] == "resident" and item["resident_id"] != user["id"]:
            abort(403)
        try:
            events = db.execute(
                """SELECT request_events.*, users.full_name AS actor_name
                   FROM request_events LEFT JOIN users ON users.id = request_events.actor_id
                   WHERE request_id = ? ORDER BY created_at ASC, id ASC""",
                (request_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            events = []
    return render_template(
        "request_detail.html",
        user=user,
        item=item,
        events=events,
        statuses=sorted(REQUEST_STATUSES),
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/residents")
@roles_required("admin", "official")
def residents_page():
    search = request.args.get("search", "").strip()[:80]
    purok_filter = request.args.get("purok", "").strip()[:80]
    status_filter = request.args.get("account", "").strip()
    with get_db() as db:
        puroks = [
            row["purok"]
            for row in db.execute(
                """SELECT DISTINCT purok FROM users
                   WHERE role = 'resident' AND purok != '' ORDER BY purok"""
            ).fetchall()
        ]
        query = """SELECT id, full_name, username, age, phone, purok, address, active
                   FROM users WHERE role = 'resident'"""
        params = []
        if search:
            query += " AND (full_name LIKE ? OR username LIKE ? OR address LIKE ? OR phone LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like, like, like])
        if purok_filter:
            query += " AND purok = ?"
            params.append(purok_filter)
        if status_filter == "active":
            query += " AND active = 1"
        elif status_filter == "inactive":
            query += " AND active = 0"
        query += " ORDER BY full_name COLLATE NOCASE"
        residents = db.execute(query, params).fetchall()
    return render_template(
        "residents.html",
        user=current_user(),
        residents=residents,
        puroks=puroks,
        search=search,
        purok_filter=purok_filter,
        status_filter=status_filter,
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/residents/<int:resident_id>")
@roles_required("admin", "official")
def resident_detail(resident_id):
    with get_db() as db:
        resident = db.execute(
            "SELECT * FROM users WHERE id = ? AND role = 'resident'", (resident_id,)
        ).fetchone()
        if resident is None:
            abort(404)
        resident_requests = db.execute(
            """SELECT * FROM service_requests WHERE resident_id = ?
               ORDER BY created_at DESC, id DESC LIMIT 20""",
            (resident_id,),
        ).fetchall()
    return render_template(
        "resident_detail.html",
        user=current_user(),
        resident=resident,
        resident_requests=resident_requests,
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/residents/<int:resident_id>/update", methods=["POST"])
@roles_required("admin", "official")
def update_resident(resident_id):
    full_name = request.form.get("full_name", "").strip()[:120]
    age_text = request.form.get("age", "").strip()
    phone = request.form.get("phone", "").strip()[:40]
    purok = request.form.get("purok", "").strip()[:80]
    address = request.form.get("address", "").strip()[:200]
    age = None
    if age_text:
        try:
            age = int(age_text)
        except ValueError:
            flash("Age must be a whole number.", "error")
            return redirect(url_for("resident_detail", resident_id=resident_id))
        if not 1 <= age <= 120:
            flash("Enter an age between 1 and 120.", "error")
            return redirect(url_for("resident_detail", resident_id=resident_id))
    if not full_name:
        flash("Full name is required.", "error")
        return redirect(url_for("resident_detail", resident_id=resident_id))
    with get_db() as db:
        result = db.execute(
            """UPDATE users SET full_name = ?, age = ?, phone = ?, purok = ?, address = ?
               WHERE id = ? AND role = 'resident'""",
            (full_name, age, phone, purok, address, resident_id),
        )
        if result.rowcount == 0:
            abort(404)
    log_activity(current_user()["id"], "update_resident", f"Updated resident #{resident_id:04d}")
    flash("Resident details updated.", "success")
    return redirect(url_for("resident_detail", resident_id=resident_id))


@app.route("/residents/export")
@roles_required("admin", "official")
def export_residents():
    with get_db() as db:
        rows = db.execute(
            """SELECT id, full_name, username, age, phone, purok, address, active, created_at
               FROM users WHERE role = 'resident' ORDER BY full_name COLLATE NOCASE"""
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Full name", "Username", "Age", "Phone", "Purok", "Address", "Active", "Created"])
    for row in rows:
        writer.writerow(
            [row["id"], row["full_name"], row["username"], row["age"] or "",
             row["phone"], row["purok"], row["address"],
             "Active" if row["active"] else "Inactive", row["created_at"]]
        )
    log_activity(current_user()["id"], "export_residents", f"Exported {len(rows)} residents")
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=residents.csv"},
    )


@app.route("/accounts")
@roles_required("admin")
def accounts_page():
    search = request.args.get("search", "").strip()[:80]
    role_filter = request.args.get("role", "").strip()
    if role_filter not in ROLES:
        role_filter = ""
    with get_db() as db:
        query = "SELECT id, full_name, username, role, active, created_at FROM users WHERE 1 = 1"
        params = []
        if search:
            query += " AND (full_name LIKE ? OR username LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like])
        if role_filter:
            query += " AND role = ?"
            params.append(role_filter)
        query += " ORDER BY role, full_name COLLATE NOCASE"
        accounts = db.execute(query, params).fetchall()
    return render_template(
        "accounts.html",
        user=current_user(),
        accounts=accounts,
        search=search,
        role_filter=role_filter,
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/announcements")
@login_required
def announcements_page():
    with get_db() as db:
        items = db.execute(
            """SELECT announcements.*, users.full_name AS author_name
               FROM announcements LEFT JOIN users ON users.id = announcements.created_by
               ORDER BY is_pinned DESC, created_at DESC, id DESC"""
        ).fetchall()
    return render_template(
        "announcements.html",
        user=current_user(),
        announcements=items,
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/announcements", methods=["POST"])
@roles_required("admin", "official")
def create_announcement():
    title = request.form.get("title", "").strip()[:150]
    body = request.form.get("body", "").strip()[:2000]
    is_pinned = 1 if request.form.get("is_pinned") == "1" else 0
    if not title or not body:
        flash("Title and details are required.", "error")
    else:
        with get_db() as db:
            db.execute(
                "INSERT INTO announcements (title, body, is_pinned, created_by) VALUES (?, ?, ?, ?)",
                (title, body, is_pinned, current_user()["id"]),
            )
        log_activity(current_user()["id"], "create_announcement", title[:120])
        notify_all_residents(
            f"New announcement: {title}",
            body[:200],
            url_for("announcements_page"),
        )
        flash("Announcement published.", "success")
    return redirect(url_for("announcements_page"))


@app.route("/announcements/<int:announcement_id>/pin", methods=["POST"])
@roles_required("admin", "official")
def toggle_pin_announcement(announcement_id):
    with get_db() as db:
        result = db.execute(
            """UPDATE announcements SET is_pinned = CASE is_pinned WHEN 1 THEN 0 ELSE 1 END,
                      updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (announcement_id,),
        )
        if result.rowcount == 0:
            abort(404)
    flash("Announcement pin updated.", "success")
    return redirect(url_for("announcements_page"))


@app.route("/announcements/<int:announcement_id>/delete", methods=["POST"])
@roles_required("admin", "official")
def delete_announcement(announcement_id):
    with get_db() as db:
        result = db.execute("DELETE FROM announcements WHERE id = ?", (announcement_id,))
        if result.rowcount == 0:
            abort(404)
    log_activity(current_user()["id"], "delete_announcement", f"Announcement #{announcement_id} deleted")
    flash("Announcement deleted.", "success")
    return redirect(url_for("announcements_page"))


@app.route("/profile")
@login_required
def profile_page():
    return render_template(
        "profile.html",
        user=current_user(),
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/developers")
@login_required
def developers_page():
    return render_template(
        "developers.html",
        user=current_user(),
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/profile", methods=["POST"])
@login_required
def update_profile():
    user = current_user()
    full_name = request.form.get("full_name", "").strip()[:120]
    age_text = request.form.get("age", "").strip()
    phone = request.form.get("phone", "").strip()[:40]
    purok = request.form.get("purok", "").strip()[:80]
    address = request.form.get("address", "").strip()[:200]
    birthday = request.form.get("birthday", "").strip()
    sex = request.form.get("sex", "").strip()
    if not full_name:
        flash("Full name is required.", "error")
        return redirect(url_for("profile_page"))
    age = None
    if age_text:
        try:
            age = int(age_text)
        except ValueError:
            flash("Age must be a whole number.", "error")
            return redirect(url_for("profile_page"))
        if not 1 <= age <= 120:
            flash("Enter an age between 1 and 120.", "error")
            return redirect(url_for("profile_page"))
    if sex and sex not in ("Male", "Female", "Other", "Prefer not to say"):
        flash("Please select a valid sex option.", "error")
        return redirect(url_for("profile_page"))
    with get_db() as db:
        try:
            db.execute(
                """UPDATE users SET full_name = ?, age = ?, phone = ?, purok = ?, address = ?,
                   birthday = ?, sex = ? WHERE id = ?""",
                (full_name, age, phone, purok, address, birthday, sex, user["id"]),
            )
        except sqlite3.Error:
            flash("Could not update your profile.", "error")
            return redirect(url_for("profile_page"))
    session.pop("moss_awaiting_profile", None)
    log_activity(user["id"], "update_profile", "Resident updated own profile")
    flash("Your profile has been updated.", "success")
    return redirect(url_for("profile_page"))


@app.route("/profile/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "GET":
        return render_template(
            "change_password.html",
            user=current_user(),
            today=date.today().strftime("%A, %B %d, %Y"),
        )
    user = current_user()
    current_pw = request.form.get("current_password", "")
    new_pw = request.form.get("new_password", "")
    confirm_pw = request.form.get("confirm_password", "")
    with get_db() as db:
        record = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not record or not check_password_hash(record["password_hash"], current_pw):
        flash("Your current password is incorrect.", "error")
        return redirect(url_for("change_password"))
    if len(new_pw) < 8:
        flash("New password must be at least 8 characters.", "error")
        return redirect(url_for("change_password"))
    if new_pw != confirm_pw:
        flash("New passwords do not match.", "error")
        return redirect(url_for("change_password"))
    with get_db() as db:
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_pw), user["id"]),
        )
    log_activity(user["id"], "change_password", "Password changed")
    flash("Password changed successfully.", "success")
    return redirect(url_for("change_password"))


@app.route("/reports")
@roles_required("admin", "official")
def reports_page():
    with get_db() as db:
        by_status = {
            row["status"]: row["total"]
            for row in db.execute(
                "SELECT status, COUNT(*) AS total FROM service_requests GROUP BY status"
            ).fetchall()
        }
        by_type = db.execute(
            "SELECT request_type, COUNT(*) AS total FROM service_requests GROUP BY request_type ORDER BY total DESC"
        ).fetchall()
        by_purok = db.execute(
            """SELECT COALESCE(NULLIF(users.purok, ''), 'Not specified') AS purok, COUNT(*) AS total
               FROM service_requests JOIN users ON users.id = service_requests.resident_id
               GROUP BY purok ORDER BY total DESC LIMIT 8"""
        ).fetchall()
        monthly = db.execute(
            """SELECT substr(created_at, 1, 7) AS month, COUNT(*) AS total
               FROM service_requests GROUP BY month ORDER BY month DESC LIMIT 6"""
        ).fetchall()
        recent = db.execute(
            """SELECT service_requests.id, service_requests.request_type, service_requests.status,
                      service_requests.created_at, users.full_name AS resident_name
               FROM service_requests JOIN users ON users.id = service_requests.resident_id
               ORDER BY service_requests.created_at DESC, service_requests.id DESC LIMIT 8"""
        ).fetchall()
        total_residents = db.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'resident' AND active = 1"
        ).fetchone()[0]
        total_requests = db.execute("SELECT COUNT(*) FROM service_requests").fetchone()[0]
    for status in REQUEST_STATUSES:
        by_status.setdefault(status, 0)
    max_month = max((r["total"] for r in monthly), default=1)
    max_purok = max((r["total"] for r in by_purok), default=1)
    return render_template(
        "reports.html",
        user=current_user(),
        by_status=by_status,
        by_type=by_type,
        by_purok=by_purok,
        monthly=list(reversed(monthly)),
        recent=recent,
        total_residents=total_residents,
        total_requests=total_requests,
        max_month=max_month,
        max_purok=max_purok,
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/activity")
@roles_required("admin")
def activity_page():
    search = request.args.get("search", "").strip()[:80]
    with get_db() as db:
        if search:
            like = f"%{search}%"
            entries = db.execute(
                """SELECT activity_log.*, users.full_name AS actor_name, users.username AS actor_username
                   FROM activity_log LEFT JOIN users ON users.id = activity_log.actor_id
                   WHERE action LIKE ? OR details LIKE ? OR users.full_name LIKE ?
                   ORDER BY activity_log.created_at DESC, activity_log.id DESC LIMIT 100""",
                (like, like, like),
            ).fetchall()
        else:
            entries = db.execute(
                """SELECT activity_log.*, users.full_name AS actor_name, users.username AS actor_username
                   FROM activity_log LEFT JOIN users ON users.id = activity_log.actor_id
                   ORDER BY activity_log.created_at DESC, activity_log.id DESC LIMIT 100"""
            ).fetchall()
    return render_template(
        "activity.html",
        user=current_user(),
        entries=entries,
        search=search,
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/incidents")
@login_required
def incidents_page():
    user = current_user()
    search = request.args.get("search", "").strip()[:80]
    status_filter = request.args.get("status", "").strip()
    category_filter = request.args.get("category", "").strip()
    valid_statuses = INCIDENT_STATUSES
    valid_categories = INCIDENT_CATEGORIES
    if status_filter not in valid_statuses:
        status_filter = ""
    if category_filter not in valid_categories:
        category_filter = ""
    with get_db() as db:
        if user["role"] == "resident":
            query = """SELECT * FROM incidents WHERE resident_id = ?"""
            params = [user["id"]]
        else:
            query = """SELECT incidents.*, users.full_name AS resident_name
                       FROM incidents LEFT JOIN users ON users.id = incidents.resident_id"""
            params = []
        if search:
            like = f"%{search}%"
            query += " AND (title LIKE ? OR description LIKE ? OR category LIKE ?)"
            params.extend([like, like, like])
        if status_filter:
            query += " AND status = ?"
            params.append(status_filter)
        if category_filter:
            query += " AND category = ?"
            params.append(category_filter)
        query += " ORDER BY created_at DESC, id DESC"
        incidents = db.execute(query, params).fetchall()
    return render_template(
        "incidents.html",
        user=user,
        incidents=incidents,
        search=search,
        status_filter=status_filter,
        category_filter=category_filter,
        statuses=sorted(valid_statuses),
        categories=sorted(valid_categories),
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/incidents", methods=["POST"])
@login_required
def create_incident():
    user = current_user()
    title = request.form.get("title", "").strip()[:150]
    description = request.form.get("description", "").strip()[:1000]
    category = request.form.get("category", "other").strip()
    location = request.form.get("location", "").strip()[:200]
    valid_categories = INCIDENT_CATEGORIES
    if category not in valid_categories:
        category = "other"
    if not title or not description:
        flash("Title and description are required.", "error")
        return redirect(url_for("incidents_page"))
    with get_db() as db:
        db.execute(
            """INSERT INTO incidents (title, description, category, location, resident_id)
               VALUES (?, ?, ?, ?, ?)""",
            (title, description, category, location, user["id"]),
        )
        incident_id = db.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
    log_activity(user["id"], "create_incident", f"Incident #{incident_id}: {title[:80]}")
    create_notification(
        user["id"],
        "incident",
        f"Incident #{incident_id:04d} reported",
        f"Your concern ({category.replace('_', ' ').title()}) has been logged and is under review.",
        url_for("incident_detail", incident_id=incident_id),
    )
    flash("Incident reported successfully. It has been logged for review.", "success")
    return redirect(url_for("incidents_page"))


@app.route("/incidents/<int:incident_id>")
@login_required
def incident_detail(incident_id):
    user = current_user()
    with get_db() as db:
        if user["role"] == "resident":
            incident = db.execute(
                """SELECT * FROM incidents WHERE id = ? AND resident_id = ?""",
                (incident_id, user["id"]),
            ).fetchone()
        else:
            incident = db.execute(
                """SELECT incidents.*, users.full_name AS resident_name
                   FROM incidents LEFT JOIN users ON users.id = incidents.resident_id
                   WHERE incidents.id = ?""",
                (incident_id,),
            ).fetchone()
        if incident is None:
            abort(404)
    return render_template(
        "incident_detail.html",
        user=user,
        incident=incident,
        statuses=INCIDENT_STATUSES,
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/incidents/<int:incident_id>/update", methods=["POST"])
@roles_required("admin", "official")
def update_incident(incident_id):
    status = request.form.get("status", "").strip()
    valid_statuses = {"open", "in_progress", "resolved", "closed"}
    if status not in valid_statuses:
        abort(400, description="Invalid status.")
    with get_db() as db:
        resident_row = db.execute(
            "SELECT resident_id FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
        if resident_row is None:
            abort(404)
        resident_id = resident_row["resident_id"]
        result = db.execute(
            """UPDATE incidents SET status = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (status, incident_id),
        )
        if result.rowcount == 0:
            abort(404)
    log_activity(current_user()["id"], "update_incident", f"Incident #{incident_id} -> {status}")
    create_notification(
        resident_id,
        "incident",
        f"Incident #{incident_id:04d} status updated",
        f"Your concern status changed to {status.replace('_', ' ').title()}.",
        url_for("incident_detail", incident_id=incident_id),
    )
    flash(f"Incident status updated to {status}.", "success")
    return redirect(url_for("incident_detail", incident_id=incident_id))


@app.route("/blotters")
@roles_required("admin", "official")
def blotters_page():
    search = request.args.get("search", "").strip()[:80]
    status_filter = request.args.get("status", "").strip()
    valid_statuses = {"open", "in_progress", "resolved", "closed"}
    if status_filter not in valid_statuses:
        status_filter = ""
    with get_db() as db:
        query = """SELECT blotters.*, users.full_name AS reported_by_name
                   FROM blotters LEFT JOIN users ON users.id = blotters.reported_by
                   WHERE 1 = 1"""
        params = []
        if search:
            like = f"%{search}%"
            query += " AND (blotter_number LIKE ? OR complainant_name LIKE ? OR person_involved LIKE ? OR nature_of_complaint LIKE ?)"
            params.extend([like, like, like, like])
        if status_filter:
            query += " AND status = ?"
            params.append(status_filter)
        query += " ORDER BY created_at DESC, id DESC"
        blotters = db.execute(query, params).fetchall()
    return render_template(
        "blotters.html",
        user=current_user(),
        blotters=blotters,
        search=search,
        status_filter=status_filter,
        statuses=sorted(valid_statuses),
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/blotters", methods=["POST"])
@roles_required("admin", "official")
def create_blotter():
    user = current_user()
    complainant_name = request.form.get("complainant_name", "").strip()[:150]
    complainant_address = request.form.get("complainant_address", "").strip()[:200]
    person_involved = request.form.get("person_involved", "").strip()[:150]
    nature_of_complaint = request.form.get("nature_of_complaint", "").strip()[:1000]
    location = request.form.get("location", "").strip()[:200]
    date_time_incident = request.form.get("date_time_incident", "").strip()
    status = request.form.get("status", "open").strip()
    action_taken = request.form.get("action_taken", "").strip()[:1000]
    valid_statuses = {"open", "in_progress", "resolved", "closed"}
    if status not in valid_statuses:
        status = "open"
    if not complainant_name or not nature_of_complaint:
        flash("Complainant name and nature of complaint are required.", "error")
        return redirect(url_for("blotters_page"))
    with get_db() as db:
        # Generate blotter number: BLTR-YYYY-NNNN
        from datetime import date as _date
        today = _date.today()
        year_str = today.strftime("%Y")
        last = db.execute(
            """SELECT blotter_number FROM blotters
               WHERE blotter_number LIKE ?
               ORDER BY id DESC LIMIT 1""",
            (f"BLTR-{year_str}-%",),
        ).fetchone()
        if last:
            try:
                last_num = int(last["blotter_number"].split("-")[-1])
            except (ValueError, AttributeError):
                last_num = 0
        else:
            last_num = 0
        blotter_number = f"BLTR-{year_str}-{last_num + 1:04d}"
        db.execute(
            """INSERT INTO blotters
               (blotter_number, complainant_name, complainant_address, person_involved,
                nature_of_complaint, location, date_time_incident, status, action_taken, reported_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                blotter_number, complainant_name, complainant_address, person_involved,
                nature_of_complaint, location, date_time_incident, status, action_taken,
                user["id"],
            ),
        )
        blotter_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    log_activity(user["id"], "create_blotter", f"Blotter {blotter_number}: {complainant_name[:60]}")
    create_notification(
        user["id"],
        "blotter",
        f"Blotter {blotter_number} created",
        f"New blotter for {complainant_name} has been logged.",
        url_for("blotter_detail", blotter_id=blotter_id),
    )
    flash(f"Blotter {blotter_number} created successfully.", "success")
    return redirect(url_for("blotters_page"))


@app.route("/blotters/<int:blotter_id>")
@roles_required("admin", "official")
def blotter_detail(blotter_id):
    user = current_user()
    with get_db() as db:
        blotter = db.execute(
            """SELECT blotters.*, users.full_name AS reported_by_name
               FROM blotters LEFT JOIN users ON users.id = blotters.reported_by
               WHERE blotters.id = ?""",
            (blotter_id,),
        ).fetchone()
        if blotter is None:
            abort(404)
    return render_template(
        "blotter_detail.html",
        user=user,
        blotter=blotter,
        statuses={"open", "in_progress", "resolved", "closed"},
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/blotters/<int:blotter_id>/update", methods=["POST"])
@roles_required("admin", "official")
def update_blotter(blotter_id):
    status = request.form.get("status", "").strip()
    action_taken = request.form.get("action_taken", "").strip()[:1000]
    valid_statuses = {"open", "in_progress", "resolved", "closed"}
    if status not in valid_statuses:
        abort(400, description="Invalid status.")
    with get_db() as db:
        result = db.execute(
            """UPDATE blotters SET status = ?, action_taken = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (status, action_taken, blotter_id),
        )
        if result.rowcount == 0:
            abort(404)
        blotter = db.execute(
            "SELECT blotter_number, complainant_name FROM blotters WHERE id = ?",
            (blotter_id,),
        ).fetchone()
    log_activity(current_user()["id"], "update_blotter", f"Blotter {blotter['blotter_number']} -> {status}")
    flash(f"Blotter {blotter['blotter_number']} updated.", "success")
    return redirect(url_for("blotter_detail", blotter_id=blotter_id))


@app.route("/faqs")
@roles_required("admin", "official")
def faqs_page():
    with get_db() as db:
        faqs = db.execute(
            "SELECT * FROM faqs WHERE is_active = 1 ORDER BY category, question"
        ).fetchall()
    return render_template(
        "faqs.html",
        user=current_user(),
        faqs=faqs,
        today=date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/faqs", methods=["POST"])
@roles_required("admin")
def create_faq():
    question = request.form.get("question", "").strip()[:300]
    answer = request.form.get("answer", "").strip()[:1000]
    category = request.form.get("category", "general").strip()[:50]
    if not question or not answer:
        flash("Question and answer are required.", "error")
        return redirect(url_for("faqs_page"))
    with get_db() as db:
        db.execute(
            "INSERT INTO faqs (question, answer, category) VALUES (?, ?, ?)",
            (question, answer, category),
        )
    log_activity(current_user()["id"], "create_faq", f"FAQ: {question[:80]}")
    flash("FAQ added successfully.", "success")
    return redirect(url_for("faqs_page"))


@app.route("/assistant", methods=["GET"])
@login_required
def assistant_page():
    return redirect(url_for("dashboard") + "?moss=open#moss-chat")


@app.route("/assistant", methods=["POST"])
@login_required
def assistant():
    message = request.form.get("message", "").strip()
    return_to = request.form.get("return_to", "")
    allowed_paths = {
        url_for("dashboard"),
        url_for("requests_page"),
        url_for("announcements_page"),
        url_for("profile_page"),
        url_for("change_password"),
        url_for("developers_page"),
    }
    user = current_user()
    if user["role"] in {"admin", "official"}:
        allowed_paths.add(url_for("residents_page"))
        allowed_paths.add(url_for("reports_page"))
    if user["role"] == "admin":
        allowed_paths.add(url_for("accounts_page"))
        allowed_paths.add(url_for("activity_page"))
    # Allow returning to a request-detail page.
    if return_to.startswith(url_for("requests_page") + "/"):
        allowed_paths.add(return_to)
    if return_to.startswith(url_for("residents_page") + "/"):
        allowed_paths.add(return_to)
    if return_to not in allowed_paths:
        return_to = url_for("dashboard")

    if not message or len(message) > 300:
        flash("Enter a message of up to 300 characters.", "error")
        return redirect(return_to + "?moss=open#moss-chat")

    if message.strip().lower() == "complete my profile":
        session.pop("moss_awaiting_profile", None)
        return redirect(url_for("profile_page") + "?moss=open#moss-chat")

    if user["role"] in ("admin", "official"):
        if message.strip().lower() == "view incidents":
            return redirect(url_for("incidents_page") + "?moss=open#moss-chat")
        if message.strip().lower() == "manage faqs":
            return redirect(url_for("faqs_page") + "?moss=open#moss-chat")

    if moss_rate_limited():
        flash("You are sending messages too quickly. Please wait a moment and try again.", "error")
        return redirect(return_to + "?moss=open#moss-chat")

    reply = None
    if moss.mode()["mode"] == "ai":
        try:
            reply = moss.chat(
                message=message,
                user=user,
                history=get_moss_history(user["id"]),
                session=session,
            )
            # The AI owns the conversation while it is enabled.
            session.pop("pending_service_request", None)
        except MossAIError as exc:
            session.pop("moss_pending_action", None)
            logging.getLogger("bims").warning("Moss AI unavailable: %s", exc)
            flash("Moss could not reach the AI service, so it answered offline instead.", "error")
        except Exception:
            session.pop("moss_pending_action", None)
            logging.getLogger("bims").exception("Moss AI failed unexpectedly")
            flash("Moss hit an unexpected problem and answered offline instead.", "error")

    if reply is None:
        reply = assistant_request_action(message, user)
        if reply is None:
            reply = assistant_reply(message, user)
    append_assistant_exchange(user["id"], message, reply)
    session["moss_boot_seen"] = True
    return redirect(return_to + "?moss=open#moss-chat")


@app.route("/assistant/clear", methods=["POST"])
@login_required
def assistant_clear():
    clear_assistant_history(current_user()["id"])
    session.pop("moss_pending_action", None)
    session.pop("moss_last_executed", None)
    session.pop("pending_service_request", None)
    flash("Chat with Moss cleared.", "success")
    return_to = request.form.get("return_to", url_for("dashboard"))
    return redirect(return_to + "?moss=open#moss-chat")


@app.route("/requests", methods=["POST"])
@roles_required("resident")
def create_request():
    request_type = request.form.get("request_type", "")
    purpose = request.form.get("purpose", "").strip()
    if request_type not in REQUEST_TYPES or not purpose or len(purpose) > 500:
        flash("Choose a request type and enter a purpose (up to 500 characters).", "error")
    else:
        user = current_user()
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO service_requests (resident_id, request_type, purpose)
                   VALUES (?, ?, ?)""",
                (user["id"], request_type, purpose),
            )
            new_id = cursor.lastrowid
        log_request_event(new_id, user["id"], "", "Pending", "Request submitted")
        log_activity(user["id"], "create_request", f"{request_type} #{new_id:04d}")
        create_notification(
            user["id"],
            "request",
            f"Request #{new_id:04d} submitted",
            f"Your {request_type} request has been submitted and is pending review.",
            url_for("request_detail", request_id=new_id),
        )
        flash("Your request has been sent to the barangay office.", "success")
    return redirect(url_for("requests_page"))


@app.route("/requests/<int:request_id>/update", methods=["POST"])
@roles_required("admin", "official")
def update_request(request_id):
    status = request.form.get("status", "")
    note = request.form.get("official_note", "").strip()[:500]
    if status not in REQUEST_STATUSES:
        abort(400, description="Choose a valid request status.")
    with get_db() as db:
        current = db.execute(
            "SELECT status, resident_id FROM service_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if current is None:
            abort(404)
        old_status = current["status"]
        resident_id = current["resident_id"]
        db.execute(
            """UPDATE service_requests SET status = ?, official_note = ?,
                      updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (status, note, request_id),
        )
    log_request_event(request_id, current_user()["id"], old_status, status, note)
    log_activity(current_user()["id"], "update_request", f"Request #{request_id:04d}: {old_status} -> {status}")
    create_notification(
        resident_id,
        "request",
        f"Request #{request_id:04d} status updated",
        f"Your request status changed from {old_status} to {status}.",
        url_for("request_detail", request_id=request_id),
    )
    flash("Request status updated.", "success")
    next_page = request.form.get("next", "")
    if next_page == "detail":
        return redirect(url_for("request_detail", request_id=request_id))
    return redirect(url_for("requests_page"))


@app.route("/requests/<int:request_id>/cancel", methods=["POST"])
@roles_required("resident")
def cancel_request(request_id):
    user = current_user()
    with get_db() as db:
        item = db.execute(
            "SELECT status FROM service_requests WHERE id = ? AND resident_id = ?",
            (request_id, user["id"]),
        ).fetchone()
        if item is None:
            abort(404)
        if item["status"] not in ("Pending", "Processing"):
            flash("Only Pending or Processing requests can be cancelled.", "error")
            return redirect(url_for("request_detail", request_id=request_id))
        db.execute(
            """UPDATE service_requests SET status = 'Declined', official_note = 'Cancelled by resident',
                      updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (request_id,),
        )
    log_request_event(request_id, user["id"], item["status"], "Declined", "Cancelled by resident")
    log_activity(user["id"], "cancel_request", f"Request #{request_id:04d} cancelled")
    flash("Your request has been cancelled.", "success")
    return redirect(url_for("request_detail", request_id=request_id))


@app.route("/requests/<int:request_id>/delete", methods=["POST"])
@roles_required("admin", "official")
def delete_request(request_id):
    with get_db() as db:
        item = db.execute(
            "SELECT id FROM service_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if item is None:
            abort(404)
        db.execute("DELETE FROM request_events WHERE request_id = ?", (request_id,))
        db.execute("DELETE FROM service_requests WHERE id = ?", (request_id,))
    log_activity(current_user()["id"], "delete_request", f"Request #{request_id:04d} deleted")
    flash("Request deleted.", "success")
    return redirect(url_for("requests_page"))


@app.route("/requests/export")
@roles_required("admin", "official")
def export_requests():
    with get_db() as db:
        rows = db.execute(
            """SELECT service_requests.id, users.full_name AS resident, service_requests.request_type,
                      service_requests.purpose, service_requests.status, service_requests.official_note,
                      service_requests.created_at, service_requests.updated_at
               FROM service_requests JOIN users ON users.id = service_requests.resident_id
               ORDER BY service_requests.created_at DESC, service_requests.id DESC"""
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Ref", "Resident", "Type", "Purpose", "Status", "Office note", "Submitted", "Updated"])
    for row in rows:
        writer.writerow(
            [f"{row['id']:04d}", row["resident"], row["request_type"], row["purpose"],
             row["status"], row["official_note"], row["created_at"], row["updated_at"]]
        )
    log_activity(current_user()["id"], "export_requests", f"Exported {len(rows)} requests")
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=service_requests.csv"},
    )


@app.route("/accounts", methods=["POST"])
@roles_required("admin")
def create_account():
    full_name = request.form.get("full_name", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "")
    error = valid_profile(full_name, username, password, role)
    if role == "admin":
        error = "New accounts can be residents or barangay officials."
    if error:
        flash(error, "error")
    else:
        try:
            with get_db() as db:
                db.execute(
                    """INSERT INTO users (username, password_hash, full_name, role)
                       VALUES (?, ?, ?, ?)""",
                    (username, generate_password_hash(password), full_name, role),
                )
            log_activity(current_user()["id"], "create_account", f"{role} '{username}' created")
            flash(f"{role.title()} account created.", "success")
        except sqlite3.IntegrityError:
            flash("That username is already in use.", "error")
    return redirect(url_for("accounts_page"))


@app.route("/accounts/<int:user_id>/toggle", methods=["POST"])
@roles_required("admin")
def toggle_account(user_id):
    if user_id == current_user()["id"]:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("accounts_page"))
    with get_db() as db:
        result = db.execute(
            """UPDATE users SET active = CASE active WHEN 1 THEN 0 ELSE 1 END
               WHERE id = ? AND role != 'admin'""",
            (user_id,),
        )
        if result.rowcount == 0:
            abort(404)
    log_activity(current_user()["id"], "toggle_account", f"Account #{user_id} toggled")
    flash("Account status updated.", "success")
    return redirect(url_for("accounts_page"))


@app.route("/accounts/<int:user_id>/reset-password", methods=["POST"])
@roles_required("admin")
def reset_password(user_id):
    new_password = request.form.get("new_password", "")
    if len(new_password) < 8:
        flash("New password must be at least 8 characters.", "error")
        return redirect(url_for("accounts_page"))
    with get_db() as db:
        target = db.execute("SELECT id, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if target is None or target["role"] == "admin":
            abort(404)
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password), user_id),
        )
    log_activity(current_user()["id"], "reset_password", f"Password reset for account #{user_id}")
    flash("Password has been reset.", "success")
    return redirect(url_for("accounts_page"))


@app.errorhandler(400)
def bad_request(_error):
    return render_template("index.html", page="error", user=current_user(), code=400), 400


@app.errorhandler(403)
def forbidden(_error):
    return render_template("index.html", page="error", user=current_user(), code=403), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template("index.html", page="error", user=current_user(), code=404), 404


init_db()

# Auto-create admin account on first deploy if none exists (Render free-tier friendly)
try:
    with get_db() as db:
        admin_exists = db.execute(
            "SELECT 1 FROM users WHERE role = 'admin' LIMIT 1"
        ).fetchone()
        if admin_exists is None:
            admin_username = os.environ.get("BIMS_ADMIN_USERNAME", "admin").strip()
            admin_name = os.environ.get("BIMS_ADMIN_NAME", "System Administrator").strip()
            admin_password = os.environ.get("BIMS_ADMIN_PASSWORD")
            if admin_password and len(admin_password) >= 8:
                db.execute(
                    """INSERT INTO users (username, password_hash, full_name, role)
                       VALUES (?, ?, ?, 'admin')""",
                    (admin_username, generate_password_hash(admin_password), admin_name),
                )
                print(f"Administrator '{admin_username}' created automatically.")
            else:
                print("WARNING: No admin account created. Set BIMS_ADMIN_PASSWORD (8+ chars) to create one.")
except sqlite3.Error:
    pass


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("BIMS_DEBUG") == "1")

