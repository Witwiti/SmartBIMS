"""Tests for the Moss AI assistant.

The suite runs against a fake OpenAI-compatible server so no real API key or
network access is needed.  Run it with:

    python -m unittest test_moss_ai -v
"""

import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

# Point the app at a throwaway database before it is imported.
_TMP_DIR = tempfile.mkdtemp(prefix="bims-test-")
os.environ["BIMS_DATABASE"] = os.path.join(_TMP_DIR, "test.sqlite3")
os.environ["BIMS_SECRET_KEY"] = "test-secret-key"
os.environ.pop("MOSS_API_KEY", None)
os.environ.pop("MOSS_ENABLED", None)

import app as bims  # noqa: E402


def text_response(content):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def tool_response(name, arguments, call_id="call_1"):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class FakeProvider:
    """A tiny OpenAI-compatible server that replays scripted responses."""

    def __init__(self):
        self.responses = []
        self.requests = []
        self._server = None
        self._thread = None

    def start(self):
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                provider.requests.append(json.loads(body))
                if provider.responses:
                    payload = provider.responses.pop(0)
                else:
                    payload = text_response("No more scripted responses.")
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()


def tearDownModule():
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class MossTestCase(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()
        base_url = self.provider.start()
        os.environ["MOSS_API_KEY"] = "test-key"
        os.environ["MOSS_API_BASE_URL"] = base_url
        os.environ["MOSS_MODEL"] = "test-model"
        os.environ["MOSS_ENABLED"] = "1"

        self._reset_database()
        self.client = bims.app.test_client()
        self.resident_id = self._create_user("resident1", "resident", "Maria Santos")
        self.other_resident_id = self._create_user("resident2", "resident", "Pedro Reyes")
        self.staff_id = self._create_user("staff1", "official", "Ana Cruz")

    def tearDown(self):
        self.provider.stop()
        os.environ.pop("MOSS_API_KEY", None)

    # -- helpers ---------------------------------------------------------

    def _reset_database(self):
        with bims.get_db() as db:
            for table in (
                "moss_messages",
                "request_events",
                "activity_log",
                "notifications",
                "incidents",
                "blotters",
                "service_requests",
                "announcements",
                "faqs",
                "users",
            ):
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM sqlite_sequence")

    def _create_user(self, username, role, full_name):
        from werkzeug.security import generate_password_hash

        with bims.get_db() as db:
            db.execute(
                """INSERT INTO users (username, password_hash, full_name, role, age, phone,
                                      purok, address, birthday, sex)
                   VALUES (?, ?, ?, ?, 30, '0915', 'Purok 1', 'Street 1', '1995-01-01', 'Female')""",
                (username, generate_password_hash("password123"), full_name, role),
            )
            return db.execute("SELECT last_insert_rowid()").fetchone()[0]

    def _csrf(self, path="/login"):
        html = self.client.get(path, follow_redirects=True).get_data(as_text=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        self.assertIsNotNone(match, "no CSRF token found")
        return match.group(1)

    def _login(self, username="resident1", password="password123"):
        token = self._csrf("/login")
        return self.client.post(
            "/login",
            data={"username": username, "password": password, "csrf_token": token},
            follow_redirects=True,
        )

    def _say(self, message, path="/dashboard"):
        token = self._csrf(path)
        return self.client.post(
            "/assistant",
            data={"message": message, "return_to": path, "csrf_token": token},
            follow_redirects=True,
        )

    def _requests_in_db(self):
        with bims.get_db() as db:
            return db.execute("SELECT * FROM service_requests").fetchall()

    def _last_moss_message(self):
        with bims.get_db() as db:
            row = db.execute(
                "SELECT content FROM moss_messages WHERE role = 'assistant' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row["content"] if row else ""

    def _tool_messages(self):
        """Return every tool result the provider was shown."""
        results = []
        for request in self.provider.requests:
            for message in request.get("messages", []):
                if message.get("role") == "tool":
                    results.append(json.loads(message["content"]))
        return results

    # -- tests -----------------------------------------------------------

    def test_offline_fallback_when_no_api_key(self):
        os.environ.pop("MOSS_API_KEY", None)
        self._login()
        self._say("I need a barangay clearance for work")
        # The rule-based assistant prepared a request and asked for confirmation.
        self.assertEqual(len(self._requests_in_db()), 0)
        self.assertIn("barangay clearance", self._last_moss_message().lower())
        self.assertEqual(self.provider.requests, [])

    def test_ai_asks_before_submitting(self):
        self.provider.responses = [
            tool_response(
                "submit_service_request",
                {"request_type": "Barangay clearance", "purpose": "For work"},
            ),
            text_response(
                "May I submit a Barangay clearance request for work? Reply yes to submit."
            ),
        ]
        self._login()
        self._say("I need a barangay clearance for work")

        # The tool was prepared but not executed.
        self.assertEqual(len(self._requests_in_db()), 0)
        results = self._tool_messages()
        self.assertEqual(results[-1]["status"], "awaiting_confirmation")
        self.assertIn("may i submit", self._last_moss_message().lower())

    def test_ai_submits_after_the_user_says_yes(self):
        self.provider.responses = [
            tool_response(
                "submit_service_request",
                {"request_type": "Barangay clearance", "purpose": "For work"},
            ),
            text_response("May I submit a Barangay clearance request for work? Reply yes."),
            # The model repeats the call after the confirmation.
            tool_response(
                "submit_service_request",
                {"request_type": "Barangay clearance", "purpose": "For work"},
                call_id="call_2",
            ),
            text_response("Done! I submitted your request with reference #0001."),
        ]
        self._login()
        self._say("I need a barangay clearance for work")
        self._say("yes")

        rows = self._requests_in_db()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_type"], "Barangay clearance")
        self.assertEqual(rows[0]["purpose"], "For work")
        self.assertEqual(rows[0]["status"], "Pending")

    def test_ai_does_not_submit_when_the_user_declines(self):
        self.provider.responses = [
            tool_response(
                "submit_service_request",
                {"request_type": "Barangay clearance", "purpose": "For work"},
            ),
            text_response("May I submit a Barangay clearance request for work? Reply yes."),
            text_response("Okay, I cancelled that. I have not submitted anything."),
        ]
        self._login()
        self._say("I need a barangay clearance for work")
        self._say("no thanks")

        self.assertEqual(len(self._requests_in_db()), 0)

    def test_repeated_confirmation_does_not_file_the_request_twice(self):
        self.provider.responses = [
            tool_response(
                "submit_service_request",
                {"request_type": "Barangay clearance", "purpose": "For work"},
            ),
            text_response("May I submit a Barangay clearance request for work? Reply yes."),
        ]
        self._login()
        self._say("I need a barangay clearance for work")

        # The model repeats the call with slightly different wording.
        self.provider.responses = [
            tool_response(
                "submit_service_request",
                {"request_type": "Barangay clearance", "purpose": "For my job"},
                call_id="call_2",
            ),
            text_response("Done, your request is filed."),
        ]
        self._say("yes")
        self.assertEqual(len(self._requests_in_db()), 1)

        # A second "yes" must not file a second request either.
        self.provider.responses = [text_response("It is already filed.")]
        self._say("yes")
        self.assertEqual(len(self._requests_in_db()), 1)

    def test_incomplete_profile_blocks_the_ai_from_filing(self):
        with bims.get_db() as db:
            db.execute(
                "UPDATE users SET birthday = '', sex = '' WHERE username = 'resident1'"
            )
        self.provider.responses = [
            tool_response(
                "submit_service_request",
                {"request_type": "Barangay clearance", "purpose": "For work"},
            ),
            text_response("Please complete your profile first."),
        ]
        self._login()
        self._say("I need a barangay clearance for work")

        self.assertEqual(len(self._requests_in_db()), 0)
        results = self._tool_messages()
        self.assertEqual(results[-1]["status"], "invalid")
        self.assertIn("profile is incomplete", results[-1]["error"].lower())
        # No confirmation should even be queued.
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("moss_pending_action"))

    def test_incomplete_profile_blocks_the_ai_from_logging_a_concern(self):
        with bims.get_db() as db:
            db.execute("UPDATE users SET phone = '' WHERE username = 'resident1'")
        self.provider.responses = [
            tool_response(
                "report_incident",
                {"title": "Broken streetlight", "description": "Out for three days."},
            ),
            text_response("Please complete your profile first."),
        ]
        self._login()
        self._say("the streetlight on main street has been out")

        with bims.get_db() as db:
            count = db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertIn("profile is incomplete", self._tool_messages()[-1]["error"].lower())

    def test_ai_cannot_touch_another_residents_request(self):
        # Give the second resident a request that the first resident may not touch.
        with bims.get_db() as db:
            db.execute(
                """INSERT INTO service_requests (resident_id, request_type, purpose)
                   VALUES (?, 'Barangay clearance', 'Not yours')""",
                (self.other_resident_id,),
            )
        self.provider.responses = [
            tool_response("cancel_service_request", {"request_id": 1}),
            text_response("May I cancel request #0001? Reply yes to confirm."),
        ]
        self._login()
        self._say("cancel my request")
        self._say("yes")

        with bims.get_db() as db:
            row = db.execute("SELECT status FROM service_requests WHERE id = 1").fetchone()
        self.assertEqual(row["status"], "Pending", "another resident's request was cancelled")

    def test_staff_only_tools_are_refused_for_residents(self):
        self.provider.responses = [
            tool_response("get_barangay_stats", {}),
            text_response("I could not share that."),
        ]
        self._login()
        self._say("how many residents are registered")
        results = self._tool_messages()
        self.assertIn("error", results[-1])
        self.assertIn("barangay staff", results[-1]["error"].lower())

    def test_incident_report_requires_confirmation(self):
        self.provider.responses = [
            tool_response(
                "report_incident",
                {
                    "title": "Broken streetlight",
                    "description": "The streetlight on Main Street has been out for three days.",
                    "category": "streetlights",
                    "location": "Main Street",
                },
            ),
            text_response("Shall I log this streetlight concern? Reply yes to confirm."),
        ]
        self._login()
        self._say("the streetlight on main street has been out for 3 days")

        with bims.get_db() as db:
            count = db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        self.assertEqual(count, 0)

        self.provider.responses = [
            text_response("Logged. Your concern is reference #0001."),
        ]
        self._say("yes")
        with bims.get_db() as db:
            count = db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        self.assertEqual(count, 1)

    def test_ai_failure_falls_back_to_the_offline_assistant(self):
        os.environ["MOSS_API_BASE_URL"] = "http://127.0.0.1:1/v1"
        self._login()
        response = self._say("hello")
        body = response.get_data(as_text=True)
        self.assertIn("offline", body.lower())
        self.assertEqual(len(self._requests_in_db()), 0)

    def test_rate_limit_blocks_bursts(self):
        self.provider.responses = [text_response("Hi there.")] * 20
        self._login()
        for _ in range(10):
            self._say("hi")
        with self.client.session_transaction() as sess:
            self.assertGreaterEqual(sess["moss_rate"]["minute"]["count"], 10)
        # The 11th message in the same minute is rejected.
        body = self._say("hi").get_data(as_text=True)
        self.assertIn("too quickly", body.lower())

    def test_history_is_stored_in_the_database(self):
        self.provider.responses = [text_response("Hi! How can I help?")]
        self._login()
        self._say("hello")
        with bims.get_db() as db:
            rows = db.execute(
                "SELECT role, content FROM moss_messages ORDER BY id"
            ).fetchall()
        self.assertEqual([row["role"] for row in rows], ["user", "assistant"])
        self.assertEqual(rows[0]["content"], "hello")


if __name__ == "__main__":
    unittest.main(verbosity=2)
