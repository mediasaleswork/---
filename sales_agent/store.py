from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone


def stamp():
    return datetime.now(timezone.utc).isoformat()


def normalized(value):
    # These literal names/acronyms are documented together in the source posts.
    folded=''.join(c for c in unicodedata.normalize('NFKD',value.casefold()) if c.isalnum() and c not in 'ʻʼ')
    aliases={'ozbektelekom':'uztelecom','узбектелеком':'uztelecom',
             'toskentamaliyfanlaruniversiteti':'tafu','toshkentamaliyfanlaruniversiteti':'tafu',
             'ташкентскииуниверситетприкладныхнаук':'tafu','тафу':'tafu'}
    if folded in aliases:return aliases[folded]
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
          CREATE INDEX IF NOT EXISTS source_queue ON sources(processed,published,id);
          CREATE TABLE IF NOT EXISTS leads(
            id TEXT PRIMARY KEY, company_key TEXT UNIQUE, payload TEXT, created TEXT);
          CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,started TEXT,finished TEXT,
            status TEXT,sources INTEGER DEFAULT 0,leads INTEGER DEFAULT 0,detail TEXT);
          CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY,day TEXT,attempted TEXT);
          CREATE TABLE IF NOT EXISTS source_health(url TEXT PRIMARY KEY,checked TEXT,status TEXT,detail TEXT);
          CREATE TABLE IF NOT EXISTS publications(lead_id TEXT,source_url TEXT,payload TEXT,created TEXT,
            PRIMARY KEY(lead_id,source_url));
          CREATE TABLE IF NOT EXISTS publication_revisions(lead_id TEXT,source_url TEXT,payload TEXT,
            PRIMARY KEY(lead_id,source_url,payload));
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
          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            processed=CASE WHEN (sources.title,sources.text,sources.published,sources.links) IS NOT
              (excluded.title,excluded.text,excluded.published,excluded.links) THEN 0 ELSE sources.processed END,
            title=excluded.title,text=excluded.text,published=excluded.published,links=excluded.links,
            origin=excluded.origin,checked=excluded.checked""",
          (sid, item["url"], item["title"], item["text"], item["published"], stamp(), item["origin"], json.dumps(item.get("links", []))))
        self.db.commit()

    def pending(self, since, cursor=None, oldest_first=False):
        order,comparison=('ASC','>') if oldest_first else ('DESC','<')
        sql="SELECT * FROM sources WHERE processed=0 AND published>=?"
        params=[since]
        if cursor is not None:
            sql+=f" AND (published,id) {comparison} (?,?)"
            params.extend(cursor)
        return [dict(r) for r in self.db.execute(sql+f" ORDER BY published {order},id {order} LIMIT 300",params)]

    def processed(self, ids):
        self.db.executemany("UPDATE sources SET processed=1 WHERE id=?", [(s,) for s in ids])
        self.db.commit()

    def add_lead(self, lead):
        key = normalized(lead["company"])
        existing=self.db.execute('SELECT id,payload FROM leads WHERE company_key=?',(key,)).fetchone()
        lead["id"] = existing["id"] if existing else "MS-" + hashlib.sha256(key.encode()).hexdigest()[:10].upper()
        source = (lead['id'], lead['source_url'])
        payload = json.dumps(lead, ensure_ascii=False)
        with self.db:
            previous = self.db.execute('SELECT payload FROM publications WHERE lead_id=? AND source_url=?', source).fetchone()
            if previous:
                old = json.loads(previous[0])
                if {**old, 'checked': lead['checked']} == lead:
                    return False
                self.db.execute('INSERT OR IGNORE INTO publication_revisions VALUES(?,?,?)', (*source, previous[0]))
                self.db.execute('UPDATE publications SET payload=? WHERE lead_id=? AND source_url=?', (payload, *source))
            else:
                self.db.execute('INSERT INTO publications VALUES(?,?,?,?)', (*source, payload, stamp()))
            if not existing:
                self.db.execute('INSERT INTO leads VALUES(?,?,?,?)', (lead['id'], key, payload, stamp()))
            else:
                # An edit can correct a date in either direction. Retain the latest source.
                latest = self.db.execute("""SELECT payload FROM publications WHERE lead_id=?
                    ORDER BY json_extract(payload,'$.published') DESC, created ASC LIMIT 1""", (lead['id'],)).fetchone()
                self.db.execute('UPDATE leads SET payload=? WHERE id=?', (latest[0], lead['id']))
        return existing is None

    def publication_revisions(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM publication_revisions')]

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
