#!/usr/bin/env python3
"""Tests for the Notes API (task_bfbeb9ff).

Run from the notes_app directory:

    python -m unittest discover -s tests -v

The tests build a fresh in-memory SQLite database per test so they never
touch a real ``notes.db`` file.
"""

import json
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import app as notes_app  # noqa: E402


class NotesAPITestCase(unittest.TestCase):
    """Drive the API through an in-memory SQLite connection."""

    def setUp(self):
        self.db = notes_app.connect(":memory:")
        self.db.executescript(notes_app.SCHEMA_SQL.read_text(encoding="utf-8"))
        self.db.commit()
        # Point the handler's connection helper at our in-memory DB.
        self._real_connect = notes_app.connect
        notes_app.connect = lambda path=None: self.db

    def tearDown(self):
        notes_app.connect = self._real_connect
        self.db.close()

    # -- Helpers --------------------------------------------------------

    def _insert(self, title="Demo", body="Body"):
        cursor = self.db.execute(
            "INSERT INTO notes (title, body, created_at, updated_at) "
            "VALUES (?, ?, datetime('now'), datetime('now'))",
            (title, body),
        )
        self.db.commit()
        return cursor.lastrowid

    def _call(self, method, path, payload=None):
        handler = object.__new__(notes_app.NotesHandler)
        handler.path = path
        handler.headers = {"Content-Length": "0"}
        handler.server = type("Server", (), {"db_path": ":memory:"})()
        if payload is not None:
            handler.headers = {
                "Content-Length": str(len(json.dumps(payload).encode("utf-8")))
            }
            handler.rfile = type(
                "RFile",
                (),
                {"read": lambda self, n: json.dumps(payload).encode("utf-8")},
            )()

        captured = {}

        def fake_send(status, body):
            captured["status"] = status
            captured["body"] = body

        handler._send = fake_send
        method(handler)
        return captured["status"], captured["body"]

    # -- Health -----------------------------------------------------------

    def test_health(self):
        status, body = self._call(
            notes_app.NotesHandler.do_GET, "/health"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "healthy"})

    # -- Create (POST) ----------------------------------------------------

    def test_create_note(self):
        status, note = self._call(
            notes_app.NotesHandler.do_POST,
            "/api/notes",
            payload={"title": "Shopping", "body": "Milk"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(note["title"], "Shopping")
        self.assertEqual(note["body"], "Milk")
        self.assertIn("id", note)
        self.assertIn("created_at", note)

    def test_create_note_requires_title(self):
        status, body = self._call(
            notes_app.NotesHandler.do_POST,
            "/api/notes",
            payload={"body": "no title"},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_create_note_rejects_bad_json(self):
        handler = object.__new__(notes_app.NotesHandler)
        handler.path = "/api/notes"
        handler.headers = {"Content-Length": "5"}
        handler.rfile = type(
            "RFile", (), {"read": lambda self, n: b"not-json"}
        )()
        captured = {}

        def fake_send(status, body):
            captured["status"] = status
            captured["body"] = body

        handler._send = fake_send
        notes_app.NotesHandler.do_POST(handler)
        self.assertEqual(captured["status"], 400)

    # -- Read (GET) --------------------------------------------------------

    def test_list_notes(self):
        self._insert("A")
        self._insert("B")
        status, notes = self._call(
            notes_app.NotesHandler.do_GET, "/api/notes"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(notes), 2)

    def test_get_note(self):
        note_id = self._insert("Hello")
        status, note = self._call(
            notes_app.NotesHandler.do_GET, f"/api/notes/{note_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(note["title"], "Hello")

    def test_get_missing_note(self):
        status, body = self._call(
            notes_app.NotesHandler.do_GET, "/api/notes/9999"
        )
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    # -- Update (PUT) ------------------------------------------------------

    def test_update_note(self):
        note_id = self._insert("Old", "Old body")
        status, note = self._call(
            notes_app.NotesHandler.do_PUT,
            f"/api/notes/{note_id}",
            payload={"body": "New body"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(note["body"], "New body")
        self.assertEqual(note["title"], "Old")  # title unchanged

    def test_update_missing_note(self):
        status, body = self._call(
            notes_app.NotesHandler.do_PUT,
            "/api/notes/9999",
            payload={"title": "Nope"},
        )
        self.assertEqual(status, 404)

    # -- Delete (DELETE) ---------------------------------------------------

    def test_delete_note(self):
        note_id = self._insert("Temp")
        status, body = self._call(
            notes_app.NotesHandler.do_DELETE, f"/api/notes/{note_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"deleted": note_id})
        # The note is really gone.
        status, _ = self._call(
            notes_app.NotesHandler.do_GET, f"/api/notes/{note_id}"
        )
        self.assertEqual(status, 404)

    def test_delete_missing_note(self):
        status, body = self._call(
            notes_app.NotesHandler.do_DELETE, "/api/notes/9999"
        )
        self.assertEqual(status, 404)

    # -- Unknown paths ------------------------------------------------------

    def test_unknown_path_returns_404(self):
        status, body = self._call(
            notes_app.NotesHandler.do_GET, "/api/unknown"
        )
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
