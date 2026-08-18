# Notes App

A minimal HTTP JSON REST API backed by SQLite, used to demonstrate the
task system (`s10_task_system`).

The implementation is split across tasks:

| Task | Deliverable |
|------|-------------|
| `task_76ba0742` Setup database schema | `schema.sql` |
| `task_23e97cfd` Create API endpoints  | `app.py` |
| `task_3658651a` Write docs            | `README.md` (this file) |
| `task_bfbeb9ff` Write tests           | `tests/test_api.py` |

## Requirements

- Python 3.9+ (standard library only, no third-party packages).

## Database schema

The database is a single SQLite file (default: `notes.db`).

```sql
CREATE TABLE notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    body       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_notes_created_at ON notes (created_at);
```

- `id` ¡ª auto-incrementing primary key.
- `title` ¡ª required note heading.
- `body` ¡ª optional note content.
- `created_at` / `updated_at` ¡ª ISO-8601 timestamps maintained by SQLite.
- The index on `created_at` speeds up list ordering (newest first).

The schema is created automatically on startup by `app.init_db()`, which
executes `schema.sql` against a fresh database file.

## Running the server

```bash
python app.py --host 127.0.0.1 --port 8000 --db notes.db
```

Then try:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/notes
```

## API reference

All endpoints return and accept JSON. Success responses:

- `GET /health` ¡ú `{"status": "healthy"}`
- `GET /api/notes` ¡ú `[{...note}]`
- `POST /api/notes` ¡ú `201 {...note}`
- `GET /api/notes/{id}` ¡ú `{...note}`
- `PUT /api/notes/{id}` ¡ú `{...note}`
- `DELETE /api/notes/{id}` ¡ú `{"deleted": {id}}`

A note object looks like:

```json
{
  "id": 1,
  "title": "Shopping",
  "body": "Milk, eggs, bread",
  "created_at": "2026-08-16 10:30:00",
  "updated_at": "2026-08-16 10:30:00"
}
```

### Errors

Errors are returned as `{"error": "message"}` with an appropriate status:

| Status | Meaning |
|--------|---------|
| 400    | Invalid or missing request body / `title` |
| 404    | Unknown path or note id |
| 500    | Server error |

### Examples

Create a note:

```bash
curl -X POST http://127.0.0.1:8000/api/notes \
  -H "Content-Type: application/json" \
  -d '{"title": "Shopping", "body": "Milk, eggs, bread"}'
```

Update a note:

```bash
curl -X PUT http://127.0.0.1:8000/api/notes/1 \
  -H "Content-Type: application/json" \
  -d '{"body": "Milk, eggs, bread, butter"}'
```

Delete a note:

```bash
curl -X DELETE http://127.0.0.1:8000/api/notes/1
```

## Running the tests

```bash
python -m unittest discover -s tests -v
```

The test suite uses an in-memory SQLite database so it never touches your
real `notes.db`.
