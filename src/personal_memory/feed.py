"""Local change journal; receiver advances only after successful ingestion."""
from contextlib import closing,contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import uuid


class Feed:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS identity(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT,digest TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS heads(id TEXT PRIMARY KEY,digest TEXT);
            CREATE TABLE IF NOT EXISTS scans(locator TEXT PRIMARY KEY,fingerprint TEXT);''')
            if db.execute('SELECT count(*) FROM identity').fetchone()[0]==0:
                db.execute('INSERT INTO identity VALUES(?)',(str(uuid.uuid4()),))
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path,timeout=30)) as db,db:
            yield db

    def publish(self,docs):
        # Scan checkpoints are intentionally committed outside this transaction.
        # A file is checkpointed only after this complete publish succeeds.
        with self.connection() as db:
            for doc in docs:
                old=db.execute('SELECT digest FROM heads WHERE id=?',(doc.id,)).fetchone()
                if old and old[0]==doc.digest: continue
                db.execute('INSERT INTO events(id,digest,payload) VALUES(?,?,?)',
                    (doc.id,doc.digest,json.dumps(asdict(doc),ensure_ascii=False)))
                db.execute('INSERT OR REPLACE INTO heads VALUES(?,?)',(doc.id,doc.digest))

    def changed(self,locator,paths):
        stats=[(p.stat().st_size,p.stat().st_mtime_ns) if p.exists() else None for p in paths]
        fingerprint=json.dumps(stats)
        with self.connection() as db:
            old=db.execute('SELECT fingerprint FROM scans WHERE locator=?',(locator,)).fetchone()
        return fingerprint if not old or old[0]!=fingerprint else None

    def scanned(self,locator,fingerprint):
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO scans VALUES(?,?)',(locator,fingerprint))

    def batch(self,after=0,limit=20,expected_feed=None):
        if isinstance(after,bool) or not isinstance(after,int) or after<0 or not 1<=limit<=100:
            raise ValueError('Invalid feed cursor/limit')
        with self.connection() as db:
            identity=db.execute('SELECT id FROM identity').fetchone()[0]
            if expected_feed is not None and expected_feed!=identity:
                raise ValueError('Source feed identity changed; inspect cursor before replay')
            high=db.execute('SELECT coalesce(max(seq),0) FROM events').fetchone()[0]
            if after>high: raise ValueError('Feed cursor exceeds source history')
            rows=db.execute('SELECT seq,id,payload FROM events WHERE seq>? ORDER BY seq LIMIT ?',(after,limit)).fetchall()
        latest={row[1]:json.loads(row[2]) for row in rows}
        return {'version':1,'feed':identity,'cursor':rows[-1][0] if rows else after,
                'more':bool(rows and rows[-1][0]<high),'documents':list(latest.values())}
