from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone


def stamp():
    return datetime.now(timezone.utc).isoformat()


def normalized(value):
    return re.sub(r"[\W_]+", "", value.casefold())


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS sources(
            id TEXT PRIMARY KEY, url TEXT UNIQUE, title TEXT, text TEXT, published TEXT,
            checked TEXT, origin TEXT, links TEXT, processed INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS leads(
            id TEXT PRIMARY KEY, company_key TEXT UNIQUE, payload TEXT, created TEXT);
          CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,started TEXT,finished TEXT,
            status TEXT,sources INTEGER DEFAULT 0,leads INTEGER DEFAULT 0,detail TEXT);
          CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY,day TEXT,attempted TEXT);
          CREATE TABLE IF NOT EXISTS source_health(url TEXT PRIMARY KEY,checked TEXT,status TEXT,detail TEXT);
          CREATE TABLE IF NOT EXISTS publications(lead_id TEXT,source_url TEXT,payload TEXT,created TEXT,
            PRIMARY KEY(lead_id,source_url));
        """)
        self.db.execute('INSERT OR IGNORE INTO publications SELECT id,json_extract(payload,\'$.source_url\'),payload,created FROM leads')
        self.db.commit()

    def close(self):
        self.db.close()

    def state(self, key, default=""):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, str(value)))
        self.db.commit()

    def source_health(self, url, status, detail):
        self.db.execute("INSERT OR REPLACE INTO source_health VALUES(?,?,?,?)", (url, stamp(), status, detail))
        self.db.commit()

    def add_source(self, item):
        sid = hashlib.sha256(item["url"].encode()).hexdigest()[:20]
        self.db.execute("""INSERT INTO sources(id,url,title,text,published,checked,origin,links)
          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET checked=excluded.checked""",
          (sid, item["url"], item["title"], item["text"], item["published"], stamp(), item["origin"], json.dumps(item.get("links", []))))
        self.db.commit()

    def pending(self, since):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM sources WHERE processed=0 AND published>=? ORDER BY published DESC LIMIT 300", (since,))]

    def processed(self, ids):
        self.db.executemany("UPDATE sources SET processed=1 WHERE id=?", [(s,) for s in ids])
        self.db.commit()

    def add_lead(self, lead):
        key = normalized(lead["company"])
        existing=self.db.execute('SELECT id,payload FROM leads WHERE company_key=?',(key,)).fetchone()
        if existing:
            lead['id']=existing['id']
            old=json.loads(existing['payload'])
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO publications VALUES(?,?,?,?)',(lead['id'],lead['source_url'],json.dumps(lead,ensure_ascii=False),stamp()))
                if lead['published']>old['published']:
                    self.db.execute('UPDATE leads SET payload=? WHERE id=?',(json.dumps(lead,ensure_ascii=False),lead['id']))
            return False
        lead["id"] = "MS-" + hashlib.sha256(key.encode()).hexdigest()[:10].upper()
        result = self.db.execute("INSERT OR IGNORE INTO leads VALUES(?,?,?,?)",
            (lead["id"], key, json.dumps(lead, ensure_ascii=False), stamp()))
        self.db.commit()
        self.db.execute('INSERT OR IGNORE INTO publications VALUES(?,?,?,?)',(lead['id'],lead['source_url'],json.dumps(lead,ensure_ascii=False),stamp()))
        self.db.commit()
        return result.rowcount > 0

    def leads(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM leads ORDER BY created")]

    def publications(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM publications ORDER BY created')]

    def reserve_request(self, day, limit):
        # Count attempts before network I/O; failures consume the local budget too.
        with self.db:
            if self.db.execute("SELECT COUNT(*) FROM requests WHERE day=?", (day,)).fetchone()[0] >= limit:
                return False
            self.db.execute("INSERT INTO requests(day,attempted) VALUES(?,?)", (day, stamp()))
        return True
