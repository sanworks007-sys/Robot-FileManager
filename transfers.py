import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from .security import DATA, download_url


DB = DATA / "transfers.sqlite3"


@contextmanager
def database():
    connection = sqlite3.connect(DB, timeout=30)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE IF NOT EXISTS transfers (id TEXT PRIMARY KEY, kind TEXT, name TEXT, root TEXT, path TEXT, "
                           "url TEXT, platform TEXT, metadata TEXT, reusable INTEGER, size INTEGER, status TEXT, created REAL, updated REAL)")
        yield connection
        connection.commit()
    finally:
        connection.close()


def record(kind, name, root, path, url=None, platform=None, metadata=None, size=None, status="waiting"):
    identifier = uuid.uuid4().hex
    public_url = download_url(url) if url else None
    reusable = bool(url and (public_url == url or platform in ("huggingface", "civitai")))
    now = time.time()
    with database() as connection:
        connection.execute("INSERT INTO transfers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (identifier, kind, name, root, path, public_url, platform, json.dumps(metadata or {}),
                            reusable, size, status, now, now))
    return identifier


def update(identifier, status, size=None):
    with database() as connection:
        connection.execute("UPDATE transfers SET status=?, size=COALESCE(?, size), updated=? WHERE id=?",
                           (status, size, time.time(), identifier))


def history():
    with database() as connection:
        result = []
        for row in connection.execute("SELECT * FROM transfers ORDER BY created DESC"):
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"])
            item["reusable"] = bool(item["reusable"])
            result.append(item)
        return result
