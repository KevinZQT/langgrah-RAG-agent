import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Catalog:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir(exist_ok=True)
        self.database = self.root / "catalog.sqlite3"
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                size INTEGER NOT NULL, status TEXT NOT NULL, addedAt TEXT NOT NULL,
                excerpt TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL, index_started INTEGER NOT NULL DEFAULT 0
            )""")
            # Additive migration: existing ready documents refer to unversioned ES chunks.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
            for name, definition in {
                "file_hash": "TEXT NOT NULL DEFAULT ''",
                "active_version": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
                "updating": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {definition}")
            if "active_version" not in columns:
                conn.execute("UPDATE documents SET active_version='legacy' WHERE status='ready'")
            conn.execute("""CREATE TABLE IF NOT EXISTS document_versions (
                document_id TEXT NOT NULL, version TEXT NOT NULL, path TEXT NOT NULL,
                name TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL,
                file_hash TEXT NOT NULL, state TEXT NOT NULL,
                PRIMARY KEY (document_id, version)
            )""")
            if "active_version" not in columns:
                conn.execute("""INSERT INTO document_versions
                    SELECT id, 'legacy', path, name, kind, size, '',
                           CASE WHEN status='ready' THEN 'active' ELSE 'obsolete' END
                    FROM documents""")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.database, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, record):
        with self.connect() as conn:
            conn.execute("INSERT INTO documents (id,name,kind,size,status,addedAt,path) VALUES (?,?,?,?,?,?,?)",
                         tuple(record[key] for key in ["id", "name", "kind", "size", "status", "addedAt", "path"]))

    def get(self, document_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        return dict(row) if row else None

    def all(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM documents ORDER BY addedAt DESC, rowid DESC")]

    def update(self, document_id, **fields):
        if not fields or not set(fields) <= {"status", "excerpt", "error", "index_started", "file_hash"}:
            raise ValueError("Unsupported catalog fields")
        with self.connect() as conn:
            conn.execute(f"UPDATE documents SET {', '.join(key+'=?' for key in fields)} WHERE id=?",
                         [*fields.values(), document_id])

    def delete(self, document_id):
        with self.connect() as conn:
            conn.execute("DELETE FROM document_versions WHERE document_id=?", (document_id,))
            conn.execute("DELETE FROM documents WHERE id=?", (document_id,))

    def stage(self, record, version, *, new=False):
        """Persist the pending file and claim the document in one transaction."""
        with self.connect() as conn:
            if new:
                conn.execute("""INSERT INTO documents
                    (id,name,kind,size,status,addedAt,path,file_hash,updating)
                    VALUES (?,?,?,?,'processing',?,?,?,1)""",
                    tuple(record[k] for k in ["id", "name", "kind", "size", "addedAt", "path", "file_hash"]))
            else:
                result = conn.execute("UPDATE documents SET updating=1, error='' WHERE id=? AND updating=0", (record["id"],))
                if result.rowcount != 1:
                    raise ValueError("Document already updating or missing")
            conn.execute("""INSERT INTO document_versions
                (document_id,version,path,name,kind,size,file_hash,state)
                VALUES (?,?,?,?,?,?,?,'pending')""",
                (record["id"], version, *(record[k] for k in ["path", "name", "kind", "size", "file_hash"])))

    def versions(self, document_id=None, state=None):
        clauses, values = [], []
        for key, value in [("document_id", document_id), ("state", state)]:
            if value is not None:
                clauses.append(f"{key}=?")
                values.append(value)
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM document_versions" + (" WHERE " + " AND ".join(clauses) if clauses else ""), values)]

    def activate(self, record, excerpt, updated_at):
        with self.connect() as conn:
            key, version = record["document_id"], record["version"]
            conn.execute("UPDATE document_versions SET state='obsolete' WHERE document_id=? AND version<>?", (key, version))
            conn.execute("UPDATE document_versions SET state='active' WHERE document_id=? AND version=?", (key, version))
            conn.execute("""UPDATE documents SET name=?,kind=?,size=?,path=?,file_hash=?,
                active_version=?,excerpt=?,updated_at=?,status='ready',error='',updating=0,index_started=1
                WHERE id=?""", (*(record[k] for k in ["name", "kind", "size", "path", "file_hash"]),
                               version, excerpt, updated_at, key))

    def fail(self, document_id, version, error):
        with self.connect() as conn:
            conn.execute("UPDATE document_versions SET state='obsolete' WHERE document_id=? AND version=?", (document_id, version))
            conn.execute("""UPDATE documents SET updating=0, error=?,
                status=CASE WHEN active_version<>'' THEN 'ready' ELSE 'error' END WHERE id=?""", (error, document_id))

    def forget_version(self, document_id, version):
        with self.connect() as conn:
            conn.execute("DELETE FROM document_versions WHERE document_id=? AND version=? AND state='obsolete'", (document_id, version))

    def recover_interrupted(self):
        with self.connect() as conn:
            conn.execute("""UPDATE documents SET updating=0,
                status=CASE WHEN active_version<>'' THEN 'ready' ELSE 'error' END,
                error='The server restarted during processing. Update the file to retry. Any previous ready version is still available.'
                WHERE updating=1 OR status='processing'""")
            conn.execute("UPDATE document_versions SET state='obsolete' WHERE state='pending'")

    @staticmethod
    def public(record):
        result = {key: record[key] for key in ["id", "name", "kind", "size", "status", "addedAt", "excerpt", "error"]}
        result.update(updating=bool(record["updating"]), updatedAt=record["updated_at"])
        return result
