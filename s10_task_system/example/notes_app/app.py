#!/usr/bin/env python3
"""Notes API - a small HTTP JSON REST API backed by SQLite.

Implements the API endpoints required by task_23e97cfd on top of the
database schema defined in ``schema.sql`` (task_76ba0742).

Endpoints
---------
    GET    /health              Health check
    GET    /api/notes           List all notes
    POST   /api/notes           Create a note          {"title": ..., "body": ...}
    GET    /api/notes/{id}      Fetch a single note
    PUT    /api/notes/{id}      Update a note          {"title": ..., "body": ...}
    DELETE /api/notes/{id}      Delete a note

Run
---
    python app.py [--host 127.0.0.1] [--port 8000] [--db notes.db]
"""

import argparse
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_DB = Path(__file__).with_name("notes.db")
SCHEMA_SQL = Path(__file__).with_name("schema.sql")

STATUS = {
    "ok": 200,
    "created": 201,
    "bad_request": 400,
    "not_found": 404,
    "conflict": 409,
    "error": 500,
}


def connect(db_path):
    """Open a connection to the SQLite database with row access by name."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path):
    """Create tables from schema.sql if the database does not exist yet."""
    if not db_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def row_to_note(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "body": row["body"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def json_body(handler):
    """Read and parse the JSON request body, returning (data, error)."""
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        return None, "Invalid Content-Length header"
    if length <= 0:
        return None, "Request body is required"
    try:
        raw = handler.rfile.read(length).decode("utf-8")
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"Invalid JSON body: {exc}"
    if not isinstance(data, dict):
        return None, "JSON body must be an object"
    return data, None


class NotesHandler(BaseHTTPRequestHandler):
    server_version = "NotesAPI/1.0"

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, payload=None):
        self._send(STATUS["ok"], payload if payload is not None else {"ok": True})

    def _created(self, payload):
        self._send(STATUS["created"], payload)

    def _error(self, status, message):
        self._send(status, {"error": message})

    def _parse_id(self, path):
        parts = path.strip("/").split("/")
        # parts == ["api", "notes", "<id>"]
        if len(parts) != 3 or parts[:2] != ["api", "notes"]:
            return None
        try:
            return int(parts[2])
        except ValueError:
            return None

    # -- Routes ---------------------------------------------------------

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            return self._ok({"status": "healthy"})
        if url.path == "/api/notes":
            return self.list_notes()
        note_id = self._parse_id(url.path)
        if note_id is not None:
            return self.get_note(note_id)
        self._error(STATUS["not_found"], f"Unknown path: {url.path}")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/api/notes":
            return self._error(STATUS["not_found"], f"Unknown path: {url.path}")
        data, err = json_body(self)
        if err:
            return self._error(STATUS["bad_request"], err)
        title = (data.get("title") or "").strip()
        if not title:
            return self._error(STATUS["bad_request"], "Field 'title' is required")
        body = data.get("body") or ""
        with connect(self.server.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO notes (title, body, created_at, updated_at) "
                "VALUES (?, ?, datetime('now'), datetime('now'))",
                (title, body),
            )
            note_id = cursor.lastrowid
            conn.commit()
            row = conn.execute(
                "SELECT * FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
        self._created(row_to_note(row))

    def do_PUT(self):
        url = urlparse(self.path)
        note_id = self._parse_id(url.path)
        if note_id is None:
            return self._error(STATUS["not_found"], f"Unknown path: {url.path}")
        data, err = json_body(self)
        if err:
            return self._error(STATUS["bad_request"], err)
        with connect(self.server.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
            if row is None:
                return self._error(STATUS["not_found"], f"Note {note_id} not found")
            title = (data.get("title") or row["title"]).strip()
            body = data.get("body", row["body"])
            conn.execute(
                "UPDATE notes SET title = ?, body = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (title, body, note_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
        self._ok(row_to_note(row))

    def do_DELETE(self):
        url = urlparse(self.path)
        note_id = self._parse_id(url.path)
        if note_id is None:
            return self._error(STATUS["not_found"], f"Unknown path: {url.path}")
        with connect(self.server.db_path) as conn:
            cursor = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            conn.commit()
            if cursor.rowcount == 0:
                return self._error(STATUS["not_found"], f"Note {note_id} not found")
        self._ok({"deleted": note_id})

    # -- Data helpers ----------------------------------------------------

    def list_notes(self):
        with connect(self.server.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM notes ORDER BY created_at DESC, id DESC"
            ).fetchall()
        self._ok([row_to_note(row) for row in rows])

    def get_note(self, note_id):
        with connect(self.server.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
        if row is None:
            return self._error(STATUS["not_found"], f"Note {note_id} not found")
        self._ok(row_to_note(row))

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(),
                                               self.log_date_time_string(),
                                               fmt % args))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Notes API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    db_path = Path(args.db)
    init_db(db_path)

    server = ThreadingHTTPServer((args.host, args.port), NotesHandler)
    server.db_path = db_path
    print(f"Notes API listening on http://{args.host}:{args.port} "
          f"(db: {db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
