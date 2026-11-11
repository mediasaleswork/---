from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store(AbstractContextManager):
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS partners (
          id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, username TEXT, title TEXT,
          source_chat_id TEXT, source_message_id INTEGER, source_url TEXT,
          added_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', last_error TEXT,
          last_message_id INTEGER NOT NULL DEFAULT 0, channel_id INTEGER);
        CREATE TABLE IF NOT EXISTS requested_posts (
          partner_id INTEGER REFERENCES partners(id), message_id INTEGER,
          done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(partner_id,message_id));
        CREATE TABLE IF NOT EXISTS posts (
          id INTEGER PRIMARY KEY, partner_id INTEGER NOT NULL REFERENCES partners(id),
          album_key TEXT NOT NULL, message_id INTEGER NOT NULL, message_ids TEXT NOT NULL,
          url TEXT NOT NULL, published_at TEXT NOT NULL, first_seen_at TEXT NOT NULL,
          text TEXT NOT NULL, match_reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
          last_error TEXT, UNIQUE(partner_id,album_key));
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(id),
          due_key TEXT NOT NULL, due_at TEXT NOT NULL, checked_at TEXT NOT NULL, views INTEGER,
          screenshot_path TEXT, screenshot_status TEXT NOT NULL, screenshot_error TEXT,
          screenshot_at TEXT, screenshot_url TEXT, capture_attempts INTEGER NOT NULL DEFAULT 0,
          UNIQUE(post_id,due_key));
        CREATE TABLE IF NOT EXISTS placements (
          id INTEGER PRIMARY KEY, partner_id INTEGER NOT NULL REFERENCES partners(id),
          source_chat_id TEXT NOT NULL, source_message_id INTEGER NOT NULL,
          source_text TEXT NOT NULL, name TEXT NOT NULL, ad_type TEXT NOT NULL,
          terms TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
          retention_hours REAL, keywords TEXT NOT NULL, domains TEXT NOT NULL,
          direct_ids TEXT NOT NULL, status TEXT NOT NULL, review_reason TEXT,
          scan_complete INTEGER NOT NULL DEFAULT 0,
          default_window INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
          UNIQUE(partner_id,source_chat_id,source_message_id));
        CREATE TABLE IF NOT EXISTS placement_posts (
          placement_id INTEGER NOT NULL REFERENCES placements(id),
          post_id INTEGER NOT NULL REFERENCES posts(id),
          PRIMARY KEY(placement_id,post_id));
        CREATE TABLE IF NOT EXISTS measurement_jobs (
          post_id INTEGER NOT NULL REFERENCES posts(id), due_key TEXT NOT NULL,
          due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          checked_at TEXT, last_error TEXT, PRIMARY KEY(post_id,due_key));
        CREATE TABLE IF NOT EXISTS contract_plans (
          id INTEGER PRIMARY KEY, contract_key TEXT NOT NULL, client_name TEXT NOT NULL,
          partner_id INTEGER NOT NULL REFERENCES partners(id), title TEXT NOT NULL,
          language TEXT NOT NULL, planned_posts INTEGER NOT NULL,
          baseline_count INTEGER NOT NULL DEFAULT 0, baseline_urls TEXT NOT NULL DEFAULT '[]',
          UNIQUE(contract_key,partner_id));
        CREATE TABLE IF NOT EXISTS intake_drafts (
          id INTEGER PRIMARY KEY, source_chat_id TEXT NOT NULL, anchor_id INTEGER NOT NULL,
          name TEXT NOT NULL, client_name TEXT NOT NULL, language TEXT NOT NULL DEFAULT '',
          creative_text TEXT NOT NULL DEFAULT '', media_keys TEXT NOT NULL DEFAULT '[]',
          target_refs TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'draft',
          review_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(source_chat_id,anchor_id));
        CREATE TABLE IF NOT EXISTS source_messages (
          source_chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, draft_id INTEGER,
          text TEXT NOT NULL, media_keys TEXT NOT NULL, targets TEXT NOT NULL,
          kind TEXT NOT NULL, review_reason TEXT, received_at TEXT NOT NULL,
          PRIMARY KEY(source_chat_id,message_id));
        CREATE TABLE IF NOT EXISTS campaign_checks (
          id TEXT PRIMARY KEY, partner_id INTEGER NOT NULL REFERENCES partners(id),
          message_ids TEXT NOT NULL, placement_ids TEXT NOT NULL,
          source_signature TEXT NOT NULL, candidate_signature TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          next_try_at TEXT NOT NULL, created_at TEXT NOT NULL, checked_at TEXT,
          reason TEXT NOT NULL DEFAULT '', decision TEXT, post_id INTEGER REFERENCES posts(id));
        CREATE INDEX IF NOT EXISTS campaign_pending ON campaign_checks(status,next_try_at);
        CREATE TABLE IF NOT EXISTS campaign_requests (
          id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
          provider TEXT NOT NULL, model TEXT NOT NULL, job_ids TEXT NOT NULL,
          campaign_mappings TEXT NOT NULL, status TEXT NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS campaign_reviews (
          job_id TEXT PRIMARY KEY REFERENCES campaign_checks(id), opened_at TEXT NOT NULL,
          opened_reason TEXT NOT NULL,original_snapshot TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS campaign_review_events (
          id INTEGER PRIMARY KEY,job_id TEXT NOT NULL REFERENCES campaign_checks(id),
          recorded_at TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,decision TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS campaign_review_history ON campaign_review_events(job_id,id);
        CREATE TABLE IF NOT EXISTS candidate_versions(
          partner_id INTEGER NOT NULL,signature TEXT NOT NULL,manifest TEXT NOT NULL,
          PRIMARY KEY(partner_id,signature));
        CREATE TABLE IF NOT EXISTS candidate_files(
          key TEXT PRIMARY KEY,name TEXT NOT NULL,mime TEXT NOT NULL,expected INTEGER NOT NULL,
          saved TEXT NOT NULL DEFAULT '',size INTEGER,md5 TEXT,drive_id TEXT NOT NULL DEFAULT '',
          retry TEXT NOT NULL DEFAULT '',upload_retry TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS capture_due ON candidate_files(saved,retry);
        CREATE INDEX IF NOT EXISTS upload_due ON candidate_files(drive_id,upload_retry);
        CREATE TABLE IF NOT EXISTS candidate_parts(
          partner_id INTEGER NOT NULL,signature TEXT NOT NULL,position INTEGER NOT NULL,mid INTEGER NOT NULL,
          asset TEXT NOT NULL REFERENCES candidate_files(key),PRIMARY KEY(partner_id,signature,position));
        CREATE INDEX IF NOT EXISTS archive_origins ON candidate_parts(asset);
        CREATE TABLE IF NOT EXISTS source_revisions (
          source_chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, revision_hash TEXT NOT NULL,
          payload TEXT NOT NULL, recorded_at TEXT NOT NULL,
          PRIMARY KEY(source_chat_id,message_id,revision_hash));
        """)
        # Compatible with databases created by early versions of the MVP.
        if "channel_id" not in {r[1] for r in self.db.execute("PRAGMA table_info(partners)")}:
            self.execute("ALTER TABLE partners ADD COLUMN channel_id INTEGER")
        if "private_channel_id" not in {r[1] for r in self.db.execute("PRAGMA table_info(partners)")}:
            self.execute("ALTER TABLE partners ADD COLUMN private_channel_id INTEGER")
        for table, additions in {
            "placements": {"client_name": "TEXT NOT NULL DEFAULT ''", "language": "TEXT NOT NULL DEFAULT ''", "planned_posts": "INTEGER",
                           "media_keys": "TEXT NOT NULL DEFAULT '[]'", "draft_id": "INTEGER", "contract_key": "TEXT NOT NULL DEFAULT ''"},
            "posts": {"last_checked_at": "TEXT", "unavailable_since": "TEXT"},
            "snapshots": {"next_capture_at": "TEXT"},
            "contract_plans": {"baseline_until": "TEXT"},
            "campaign_checks": {"priority": "INTEGER NOT NULL DEFAULT 0", "availability_attempts": "INTEGER NOT NULL DEFAULT 0", "candidate_snapshot": "TEXT NOT NULL DEFAULT ''", "archive_signature": "TEXT NOT NULL DEFAULT ''", "provisional_decision": "TEXT NOT NULL DEFAULT ''", "provisional_at": "TEXT", "individual_review": "INTEGER NOT NULL DEFAULT 0"},
        }.items():
            existing = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, declaration in additions.items():
                if name not in existing:
                    self.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        self.execute("UPDATE campaign_checks SET individual_review=1 WHERE reason='individual_review' AND individual_review=0")
        if not self.state('campaign_review_register_v1',''):
            from .review_queue import reconcile
            reconcile(self)
            self.set_state('campaign_review_register_v1','1')

    def __exit__(self, *args):
        self.db.close()

    def rows(self, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in self.db.execute(sql, params)]

    def execute(self, sql: str, params=()):
        if self.db.in_transaction:
            return self.db.execute(sql, params)
        with self.db:
            return self.db.execute(sql, params)

    @contextmanager
    def transaction(self):
        """Group synchronous Store writes, including calls through intake helpers."""
        if self.db.in_transaction:
            yield
        else:
            with self.db:
                self.db.execute("BEGIN")
                yield

    def state(self, key: str, default: str = "0") -> str:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value) -> None:
        self.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def add_partner(self, ref, chat_id: int | str, source_message_id: int, *, enqueue: bool = True) -> int:
        key = ref.username.lower() if ref.username else (f"private:{ref.private_channel_id}" if ref.private_channel_id else ref.source_url)
        self.execute("""INSERT INTO partners(key,username,title,source_chat_id,source_message_id,source_url,added_at,status,last_error)
          VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING""",
          (key, ref.username, ref.username or ref.source_url, str(chat_id), source_message_id, ref.source_url, utcnow(),
           "private" if ref.private else "active", "Закрытый канал: автоматическое вступление отключено" if ref.private else None))
        pid = self.rows("SELECT id FROM partners WHERE key=?", (key,))[0]["id"]
        if ref.private_channel_id:
            self.execute("UPDATE partners SET private_channel_id=? WHERE id=?", (ref.private_channel_id, pid))
        if ref.message_id and not ref.private and enqueue:
            self.execute("INSERT OR IGNORE INTO requested_posts(partner_id,message_id) VALUES (?,?)", (pid, ref.message_id))
        return pid

    def register_brief(self, refs, text: str, chat_id, message_id: int, received: datetime, config) -> list[int]:
        from .briefs import parse_brief
        brief = parse_brief(text, refs, received, config.timezone, config.default_window_days,
                            config.keywords, config.domains)
        grouped: dict[int, list] = {}
        for ref in refs:
            pid = self.add_partner(ref, chat_id, message_id, enqueue=not brief.review_reason)
            grouped.setdefault(pid, []).append(ref)
        result = []
        for pid, items in grouped.items():
            row = self.execute("""INSERT OR IGNORE INTO placements(
              partner_id,source_chat_id,source_message_id,source_text,name,ad_type,terms,
              starts_at,ends_at,retention_hours,keywords,domains,direct_ids,status,review_reason,default_window,created_at,
              client_name,language,planned_posts)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (pid, str(chat_id), message_id, text, brief.name, brief.ad_type, brief.terms,
               brief.start, brief.end, brief.retention_hours, json.dumps(brief.keywords, ensure_ascii=False),
               json.dumps(brief.domains), json.dumps(sorted({r.message_id for r in items if r.message_id})),
               "needs_review" if brief.review_reason else "watching", brief.review_reason or None,
               int(brief.default_window), utcnow(), brief.client_name, brief.language, brief.planned_posts))
            placement = self.rows("SELECT id FROM placements WHERE partner_id=? AND source_chat_id=? AND source_message_id=?",
                                  (pid, str(chat_id), message_id))[0]["id"]
            result.append(placement)
            if row.rowcount:
                # A new brief may concern an earlier window than the saved cursor.
                self.execute("UPDATE partners SET last_message_id=0 WHERE id=?", (pid,))
        return result

    def resolve_placement(self, placement_id: int, *, start: str, end: str, keywords=None,
                          domains=None, retention_hours=None):
        if not self.rows("SELECT id FROM placements WHERE id=?", (placement_id,)):
            raise ValueError("Заявка не найдена")
        if datetime.fromisoformat(end) <= datetime.fromisoformat(start):
            raise ValueError("Окончание периода должно быть позже начала")
        self.execute("""UPDATE placements SET starts_at=?,ends_at=?,status='watching',review_reason=NULL,
          default_window=0,scan_complete=0,keywords=COALESCE(?,keywords),domains=COALESCE(?,domains),
          retention_hours=COALESCE(?,retention_hours) WHERE id=?""",
          (start, end, json.dumps(keywords, ensure_ascii=False) if keywords is not None else None,
           json.dumps(domains) if domains is not None else None, retention_hours, placement_id))
        self.execute("UPDATE partners SET last_message_id=0 WHERE id=(SELECT partner_id FROM placements WHERE id=?)", (placement_id,))

    def save_post(self, partner_id: int, messages: list, reason: str, username: str) -> int:
        messages = sorted(messages, key=lambda m: m.id)
        anchor = messages[0]
        album_key = f"g:{anchor.grouped_id}" if getattr(anchor, "grouped_id", None) else f"m:{anchor.id}"
        from .message_content import combined_content
        text = combined_content(messages).text
        ids = json.dumps([m.id for m in messages])
        self.execute("""INSERT INTO posts(partner_id,album_key,message_id,message_ids,url,published_at,first_seen_at,text,match_reason)
          VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(partner_id,album_key) DO UPDATE SET
          message_ids=excluded.message_ids,text=excluded.text,
          message_id=excluded.message_id,url=excluded.url,published_at=excluded.published_at,
          match_reason=CASE WHEN posts.match_reason LIKE 'manual:%' THEN posts.match_reason ELSE excluded.match_reason END""",
          (partner_id, album_key, anchor.id, ids, f"https://t.me/{username}/{anchor.id}", anchor.date.isoformat(), utcnow(), text, reason))
        return self.rows("SELECT id FROM posts WHERE partner_id=? AND album_key=?", (partner_id, album_key))[0]["id"]

    def add_review(self, chat_id: int | str, message_id: int, filename: str, reason: str):
        self.execute("""INSERT INTO partners(key,title,source_chat_id,source_message_id,added_at,status,last_error)
          VALUES (?,?,?,?,?,'needs_review',?) ON CONFLICT(key) DO UPDATE SET last_error=excluded.last_error""",
          (f"review:{chat_id}:{message_id}", f"Уточнить канал: {filename[:160]}", str(chat_id), message_id, utcnow(), reason))

    def dump(self) -> tuple[list[dict], list[dict], list[dict]]:
        partners = self.rows("SELECT * FROM partners ORDER BY id")
        for partner in partners:
            from .review_queue import rows_for_partner
            partner['manual_reviews']=rows_for_partner(self,partner['id'])
            partner["contract_plans"] = self.rows("SELECT * FROM contract_plans WHERE partner_id=? ORDER BY id", (partner["id"],))
            # Intake diagnostics travel through the existing report API without
            # changing its three-list signature. Only the first partner owns it.
            partner["intake"] = []
            partner["campaign_checks"] = self.rows("SELECT id,message_ids,status,reason,checked_at,next_try_at,availability_attempts,candidate_snapshot FROM campaign_checks WHERE partner_id=? AND status IN ('unavailable','review','archived_no_match','offline_review') ORDER BY created_at DESC,id",(partner['id'],))
            partner["campaign_queue"] = self.rows("SELECT status,COUNT(*) AS n FROM campaign_checks WHERE partner_id=? AND status IN ('pending','error','unavailable','review','archived_no_match','local_checked','offline_review') GROUP BY status",(partner['id'],))
            partner["placements"] = self.rows("SELECT * FROM placements WHERE partner_id=? ORDER BY id", (partner["id"],))
            for placement in partner["placements"]:
                placement["post_urls"] = [r["url"] for r in self.rows("SELECT p.url FROM posts p JOIN placement_posts x ON x.post_id=p.id WHERE x.placement_id=?", (placement["id"],))]
        posts = self.rows("SELECT * FROM posts ORDER BY published_at,id")
        for post in posts:
            post["measurement_jobs"] = self.rows("SELECT * FROM measurement_jobs WHERE post_id=? ORDER BY due_at", (post["id"],))
            post["placement_ids"] = [r["placement_id"] for r in self.rows("SELECT placement_id FROM placement_posts WHERE post_id=?", (post["id"],))]
        if partners:
            partners[0]['campaign_mode']=self.state('campaign_mode','ai')
            partners[0]['campaign_local_reason']=self.state('campaign_local_reason','')
            partners[0]["intake"] = self.rows("""SELECT s.*,d.name AS draft_name,d.client_name FROM source_messages s
              LEFT JOIN intake_drafts d ON d.id=s.draft_id WHERE s.kind!='ignored' ORDER BY received_at,message_id""")
        return (partners, posts,
                self.rows("SELECT * FROM snapshots ORDER BY checked_at,id"))

    def configure_plans(self, config):
        from .scope import channel_ref
        for plan in config.channel_plans:
            ref = channel_ref(plan["channel"])
            if not config.permits(ref):
                continue
            pid = self.add_partner(ref, "contract", 0, enqueue=False)
            self.execute("""INSERT INTO contract_plans(contract_key,client_name,partner_id,title,language,
              planned_posts,baseline_count,baseline_urls,baseline_until) VALUES (?,?,?,?,?,?,?,?,?)
              ON CONFLICT(contract_key,partner_id) DO UPDATE SET client_name=excluded.client_name,
              title=excluded.title,language=excluded.language,planned_posts=excluded.planned_posts,
              baseline_count=excluded.baseline_count,baseline_urls=excluded.baseline_urls,baseline_until=excluded.baseline_until""",
              (plan["contract"], plan["client"], pid, plan.get("title") or ref.username,
               plan.get("language", ""), plan["planned_posts"], plan.get("baseline_count", 0),
               json.dumps(plan.get("baseline_urls", [])), plan.get("baseline_until")))
