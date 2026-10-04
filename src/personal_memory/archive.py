"""Local redacted source archive and an idempotent outbox for memory backends."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .model import Document


class Archive:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
              id TEXT PRIMARY KEY, source TEXT NOT NULL, key TEXT NOT NULL,
              content TEXT NOT NULL, scope TEXT NOT NULL, metadata TEXT NOT NULL,
              digest TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
              pushed_digest TEXT, UNIQUE(source,key));
            CREATE TABLE IF NOT EXISTS revisions (
              id TEXT, revision INTEGER, content TEXT, digest TEXT, metadata TEXT,
              scope TEXT, PRIMARY KEY(id,revision));
            CREATE TABLE IF NOT EXISTS memory_exclusions (
              memory_id TEXT, source_id TEXT, digest TEXT, reason TEXT NOT NULL,
              created_at TEXT NOT NULL, PRIMARY KEY(memory_id,source_id,digest));
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, documents):
        counts = {"added": 0, "updated": 0, "skipped": 0}
        with self.connection() as db:
            for d in documents:
                old = db.execute("SELECT digest,revision FROM documents WHERE id=?", (d.id,)).fetchone()
                if old and old["digest"] == d.digest:
                    counts["skipped"] += 1
                    continue
                revision = old["revision"] + 1 if old else 1
                metadata = json.dumps(d.metadata, ensure_ascii=False, sort_keys=True)
                db.execute("""INSERT INTO documents(id,source,key,content,scope,metadata,digest,revision,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    content=excluded.content, scope=excluded.scope, metadata=excluded.metadata,
                    digest=excluded.digest,revision=excluded.revision,updated_at=excluded.updated_at""",
                    (d.id,d.source,d.key,d.content,d.scope,metadata,d.digest,revision,
                     datetime.now(timezone.utc).isoformat()))
                db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?)",
                           (d.id,revision,d.content,d.digest,metadata,d.scope))
                counts["updated" if old else "added"] += 1
        return counts

    def pending(self, scope="personal", limit=20):
        with self.connection() as db:
            return [dict(r) for r in db.execute("""SELECT * FROM documents WHERE scope=?
                AND (pushed_digest IS NULL OR pushed_digest != digest) ORDER BY source,key LIMIT ?""", (scope,limit))]

    def push(self, backend, *, scope="personal", limit=20):
        pushed = 0
        for row in self.pending(scope, limit):
            backend.retain(row)
            # A concurrent source update must remain pending.
            with self.connection() as db:
                db.execute("UPDATE documents SET pushed_digest=? WHERE id=? AND digest=?",
                           (row["digest"], row["id"], row["digest"]))
            pushed += 1
        return {"pushed": pushed}

    def get(self, doc_id, scope="personal"):
        with self.connection() as db:
            r = db.execute("SELECT * FROM documents WHERE id=? AND scope=?", (doc_id,scope)).fetchone()
            return dict(r) if r else None

    def exclude_memory(self, memory_id, doc_id, reason, scope="personal"):
        """Quarantine an incorrect derived fact for the current source version."""
        if not memory_id or not reason.strip():
            raise ValueError("Memory ID and review reason required")
        with self.connection() as db:
            row=db.execute('SELECT digest FROM documents WHERE id=? AND scope=?',(doc_id,scope)).fetchone()
            if row is None:
                raise ValueError("Source is outside this archive/scope")
            db.execute('INSERT OR REPLACE INTO memory_exclusions VALUES(?,?,?,?,?)',
                       (memory_id,doc_id,row['digest'],reason,datetime.now(timezone.utc).isoformat()))

    def memory_excluded(self, memory_id, doc_id, digest):
        with self.connection() as db:
            return db.execute('SELECT 1 FROM memory_exclusions WHERE memory_id=? AND source_id=? AND digest=?',
                              (memory_id,doc_id,digest)).fetchone() is not None

    def export(self, directory, scope="personal"):
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            rows = db.execute("SELECT * FROM documents WHERE scope=? ORDER BY id", (scope,)).fetchall()
            for r in rows:
                # IDs instead of source filenames prevent path traversal/collisions.
                content = ("---\n" + "\n".join(f"{k}: {json.dumps(r[k],ensure_ascii=False)}" for k in
                           ("id","source","key","scope","revision","digest")) + "\n---\n\n" + r["content"])
                dest = target / (r["id"] + ".md")
                temp = dest.with_suffix(".tmp")
                temp.write_text(content, encoding="utf-8")
                temp.replace(dest)
        return len(rows)

    def backup(self, target):
        target = Path(target).resolve()
        if target == self.path.resolve():
            raise ValueError("Backup target must differ from source")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination)

    def stats(self):
        with self.connection() as db:
            return {"documents": db.execute("SELECT count(*) FROM documents").fetchone()[0],
                    "revisions": db.execute("SELECT count(*) FROM revisions").fetchone()[0],
                    "pending": db.execute("SELECT count(*) FROM documents WHERE pushed_digest IS NULL OR pushed_digest != digest").fetchone()[0]}
