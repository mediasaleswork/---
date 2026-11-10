"""Durable configured milestones, with honest restart and late-discovery states."""
from datetime import datetime, timedelta, timezone


def ensure_jobs(store, post, hours):
    published = datetime.fromisoformat(post["published_at"])
    first_seen = datetime.fromisoformat(post["first_seen_at"])
    points = [("initial", first_seen)] + [(f"{h:g}h", published + timedelta(hours=h)) for h in hours]
    existing = {s["due_key"] for s in store.rows("SELECT due_key FROM snapshots WHERE post_id=?", (post["id"],))}
    with store.db:
        keys={key for key,_ in points}
        for job in store.rows("SELECT due_key FROM measurement_jobs WHERE post_id=? AND status IN ('pending','unavailable')",(post['id'],)):
            if job['due_key'] not in keys:
                store.db.execute("UPDATE measurement_jobs SET status='superseded',last_error=? WHERE post_id=? AND due_key=?",
                    ('График изменён; задание сохранено в истории',post['id'],job['due_key']))
        for key, due in points:
            state = "done" if key in existing else "missed" if key != "initial" and due <= first_seen else "pending"
            reason = "Пост обнаружен после контрольной точки; прошлый замер недоступен" if state == "missed" else None
            store.db.execute("""INSERT OR IGNORE INTO measurement_jobs(post_id,due_key,due_at,status,last_error)
              VALUES (?,?,?,?,?)""", (post["id"], key, due.isoformat(), state, reason))
            if key in existing:
                store.db.execute("UPDATE measurement_jobs SET status='done' WHERE post_id=? AND due_key=? AND status IN ('pending','unavailable','superseded')",
                                 (post["id"], key))
            else:
                store.db.execute("UPDATE measurement_jobs SET status=?,last_error=? WHERE post_id=? AND due_key=? AND status='superseded'",
                    (state,reason,post['id'],key))


def ready_jobs(store, post_id, now=None):
    now = now or datetime.now(timezone.utc)
    pending = [j for j in store.rows("SELECT * FROM measurement_jobs WHERE post_id=? AND (status='pending' OR status='unavailable' AND due_key!='48h') ORDER BY due_at", (post_id,))
               if datetime.fromisoformat(j["due_at"]) <= now]
    # After an outage a current counter cannot be both a historical 24h and a
    # 48h counter. Keep the latest due measurement; explicitly mark older gaps.
    timed = [j for j in pending if j["due_key"] != "initial"]
    for job in timed[:-1]:
        store.execute("""UPDATE measurement_jobs SET status='missed',checked_at=?,last_error=?
          WHERE post_id=? AND due_key=?""", (now.isoformat(), "Пропущено во время остановки; значение задним числом не восстанавливается",
                                           post_id, job["due_key"]))
    return [j for j in pending if j["due_key"] == "initial"] + timed[-1:]


def complete_job(store, post_id, key, checked_at, error=None):
    note = error
    rows = store.rows("SELECT due_at FROM measurement_jobs WHERE post_id=? AND due_key=?", (post_id, key))
    if not error and rows and key != "initial":
        delay = datetime.fromisoformat(checked_at) - datetime.fromisoformat(rows[0]["due_at"])
        if delay > timedelta(minutes=5):
            note = f"Замер выполнен с задержкой {int(delay.total_seconds() // 60)} мин; время измерения указано фактическое"
    store.execute("UPDATE measurement_jobs SET status=?,checked_at=?,last_error=? WHERE post_id=? AND due_key=?",
                  ("unavailable" if error else "done", checked_at, note, post_id, key))
