import base64
import gzip
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import HTTPException


APPWRITE_DATABASE_ID = "aika"
APPWRITE_COLLECTION_ID = "aika_state"


class AppwriteError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class AppwriteStore:
    def __init__(self):
        self.endpoint = os.environ["APPWRITE_ENDPOINT"].rstrip("/")
        self.project_id = os.environ["APPWRITE_PROJECT_ID"]
        self.api_key = os.environ["APPWRITE_API_KEY"]

    def request(self, method: str, path: str, body: dict[str, Any] | None = None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            self.endpoint + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Appwrite-Project": self.project_id,
                "X-Appwrite-Key": self.api_key,
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AppwriteError(exc.code, detail) from exc
        except URLError as exc:
            raise AppwriteError(503, f"Could not reach Appwrite: {exc.reason}") from exc

    def latest(self):
        query = urlencode([
            ("queries[]", json.dumps({"method": "orderDesc", "attribute": "version"})),
            ("queries[]", json.dumps({"method": "limit", "values": [1]})),
        ])
        result = self.request(
            "GET",
            f"/databases/{APPWRITE_DATABASE_ID}/collections/{APPWRITE_COLLECTION_ID}/documents?{query}",
        )
        documents = result.get("documents", [])
        return documents[0] if documents else None

    def save(self, version: int, connection: sqlite3.Connection):
        encoded = base64.b64encode(gzip.compress(connection.serialize())).decode("ascii")
        if len(encoded) > 1_000_000:
            raise HTTPException(413, "Appwrite snapshot exceeds the configured payload size limit")
        self.request(
            "POST",
            f"/databases/{APPWRITE_DATABASE_ID}/collections/{APPWRITE_COLLECTION_ID}/documents",
            {
                "documentId": f"v{version:020d}",
                "data": {"version": version, "payload": encoded},
                "permissions": [],
            },
        )


def appwrite_configured() -> bool:
    return all(os.environ.get(key) for key in (
        "APPWRITE_ENDPOINT", "APPWRITE_PROJECT_ID", "APPWRITE_API_KEY"
    ))


@contextmanager
def appwrite_database():
    store = AppwriteStore()
    try:
        document = store.latest()
    except AppwriteError as exc:
        raise HTTPException(
            503, f"Appwrite database operation failed (HTTP {exc.status})"
        ) from exc
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    version = 0
    if document:
        version = document["version"]
        try:
            compressed = base64.b64decode(document["payload"])
            connection.deserialize(gzip.decompress(compressed))
        except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
            connection.close()
            raise AppwriteError(500, "Stored Appwrite database snapshot is invalid") from exc
    initial_changes = connection.total_changes
    try:
        yield connection
        connection.commit()
        if connection.total_changes != initial_changes:
            store.save(version + 1, connection)
    except AppwriteError as exc:
        connection.rollback()
        if exc.status == 409:
            raise HTTPException(409, "Data changed concurrently; please retry this request") from exc
        raise HTTPException(503, f"Appwrite database operation failed: {exc}") from exc
    finally:
        connection.close()


class DictRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class ScalarRow:
    def __init__(self, value):
        self.value = value

    def __getitem__(self, key):
        if key == 0:
            return self.value
        raise KeyError(key)


class PostgresCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.lastrowid = None

    def _sql(self, sql):
        sql = sql.replace("?", "%s")
        sql = sql.replace("substr(", "LEFT(")
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        sql = sql.replace("INTEGER NOT NULL DEFAULT", "BIGINT NOT NULL DEFAULT")
        sql = sql.replace(" REAL ", " DOUBLE PRECISION ")
        sql = sql.replace(" REAL,", " DOUBLE PRECISION,")
        sql = sql.replace(" REAL)", " DOUBLE PRECISION)")
        return sql

    def execute(self, sql, params=None):
        sql = self._sql(sql)
        statement = sql.strip().upper()
        if statement.startswith("INSERT ") and " RETURNING " not in statement:
            sql = sql.rstrip(";") + " RETURNING id"
        if params is None:
            self.cursor.execute(sql)
        else:
            self.cursor.execute(sql, params)
        if statement.startswith("INSERT ") and " RETURNING " not in statement:
            row = self.cursor.fetchone()
            if row:
                self.lastrowid = row[0]
        return self

    def executemany(self, sql, params_seq):
        self.cursor.executemany(self._sql(sql), params_seq)
        return self

    def executescript(self, sql):
        for statement in sql.split(";"):
            statement = statement.strip()
            if statement:
                self.execute(statement)

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is None:
            return None
        if len(row) == 1:
            return ScalarRow(row[0])
        names = [column.name for column in self.cursor.description]
        result = DictRow(zip(names, row))
        return result

    def fetchall(self):
        rows = self.cursor.fetchall()
        if not rows:
            return []
        if len(rows[0]) == 1:
            return [ScalarRow(row[0]) for row in rows]
        names = [column.name for column in self.cursor.description]
        return [DictRow(zip(names, row)) for row in rows]


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=None):
        return PostgresCursor(self.connection.cursor()).execute(sql, params)

    def executemany(self, sql, params_seq):
        return PostgresCursor(self.connection.cursor()).executemany(sql, params_seq)

    def executescript(self, sql):
        cursor = PostgresCursor(self.connection.cursor())
        cursor.executescript(sql)

    def commit(self):
        self.connection.commit()

    def close(self):
        self.connection.close()


@contextmanager
def database(base_dir: Path):
    if appwrite_configured():
        with appwrite_database() as connection:
            yield connection
        return
    appwrite_settings = ("APPWRITE_ENDPOINT", "APPWRITE_PROJECT_ID", "APPWRITE_API_KEY")
    if any(os.environ.get(key) for key in appwrite_settings):
        raise RuntimeError("Appwrite configuration is incomplete; refusing to use local storage")

    database_url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if database_url:
        import psycopg

        connection = psycopg.connect(database_url)
        try:
            yield PostgresConnection(connection)
            connection.commit()
        finally:
            connection.close()
        return

    sqlite_path = os.environ.get("AIKA_DB")
    if not sqlite_path:
        sqlite_path = "/tmp/aika.db" if os.environ.get("VERCEL") else str(base_dir / "aika.db")
    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()
