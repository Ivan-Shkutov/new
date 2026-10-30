"""Small single-process persistence; all SQL data is parameterized."""
from contextlib import contextmanager
import json
import os
import sqlite3
from pathlib import Path
from cryptography.fernet import Fernet


class Store:
    def __init__(self, runtime):
        self.runtime = Path(runtime)
        self.runtime.mkdir(parents=True, exist_ok=True)
        key = self.runtime / 'registry.key'
        if not key.exists():
            # Never silently replace a lost key for an existing installation.
            if (self.runtime / 'pulse.sqlite3').exists():
                raise RuntimeError('registry.key missing: restore the original key from backup')
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        self.cipher = Fernet(key.read_bytes().strip())
        self.path = self.runtime / 'pulse.sqlite3'
        with self.connect() as c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS clusters (id TEXT PRIMARY KEY,name TEXT NOT NULL,enabled INTEGER NOT NULL,config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots (cluster_id TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY,name TEXT NOT NULL,cluster_id TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,sha256 TEXT NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(id UNINDEXED,body);
                CREATE TABLE IF NOT EXISTS investigations (id TEXT PRIMARY KEY,cluster_id TEXT NOT NULL,question TEXT NOT NULL,status TEXT NOT NULL,answer TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY,cluster_id TEXT NOT NULL,question TEXT NOT NULL,answer TEXT NOT NULL,solution TEXT NOT NULL,created_at TEXT NOT NULL);
            ''')
            columns = {r['name'] for r in c.execute('PRAGMA table_info(investigations)')}
            if 'previous_id' not in columns:
                c.execute("ALTER TABLE investigations ADD COLUMN previous_id TEXT NOT NULL DEFAULT ''")
            for name in ('context','trace'):
                if name not in columns:
                    c.execute(f"ALTER TABLE investigations ADD COLUMN {name} TEXT NOT NULL DEFAULT '{{}}'")
            c.execute("UPDATE investigations SET status='interrupted',error='Процесс перезапущен. Повторите расследование вручную.' WHERE status IN ('queued','running')")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def rows(self, query, args=()):
        with self.connect() as c:
            return [dict(row) for row in c.execute(query, args)]

    def execute(self, query, args=()):
        with self.connect() as c:
            return c.execute(query, args).rowcount

    def encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def decrypt(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def settings(self):
        rows = self.rows('SELECT value FROM settings WHERE id=1')
        return self.decrypt(rows[0]['value']) if rows else {}

    def cluster(self, cid):
        rows = self.rows('SELECT * FROM clusters WHERE id=?', (cid,))
        if not rows:
            return None
        row = rows[0]
        row['config'] = self.decrypt(row['config'])
        return row
