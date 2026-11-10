from __future__ import annotations

import argparse
import asyncio
import logging
import json
import math
import os
from pathlib import Path
import sys
import time

from .config import load_config
from .storage import Store


async def telegram_command(config, command):
    from .engine import client_for
    from telethon import types, utils

    client = client_for(config)
    try:
        if command == "login":
            await client.start()
            print("Вход выполнен. Сессия сохранена локально в data/telegram.session.")
        else:
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError("Сначала выполните команду login")
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if isinstance(entity, types.Chat) or isinstance(entity, types.Channel) and entity.megagroup:
                    print(f"{utils.get_peer_id(entity)}\t{dialog.name}")
    finally:
        await client.disconnect()


def demo(root: Path, timezone: str):
    from datetime import datetime, timezone as tz, timedelta
    from .unified_report import export_unified

    now = datetime.now(tz.utc)
    partners = [{"id": 1, "username": None, "title": "ДЕМО — партнёр", "status": "demo", "last_error": "Вымышленные данные для примера"}]
    posts = [{"id": 1, "partner_id": 1, "message_id": 1, "url": "", "published_at": (now - timedelta(hours=24)).isoformat(),
              "text": "ДЕМО. Здесь будет текст найденной рекламы.", "match_reason": "ДЕМО", "status": "demo", "last_error": ""}]
    snapshots = [{"id": 1, "post_id": 1, "checked_at": now.isoformat(), "due_at": now.isoformat(), "due_key": "24h", "views": 12345,
                  "screenshot_status": "demo", "screenshot_error": "ДЕМО: скриншот не создавался", "screenshot_path": None}]
    path = export_unified(partners, posts, snapshots, root / "demo-report.xlsx", timezone=timezone,
                          note="ДЕМОНСТРАЦИЯ. Все данные вымышлены; реальные скриншоты и проверки Telegram не выполнялись.")
    print(path)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Мониторинг рекламы в публичных Telegram-каналах без вступления")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Создать config.yaml из примера; существующий не перезаписывается")
    sub.add_parser("check-config", help="Проверить настройки без подключения")
    sub.add_parser("login", help="Локальный интерактивный вход в Telegram")
    sub.add_parser("dialogs", help="Показать ID доступных рабочих групп")
    run_parser = sub.add_parser("run", help="Запустить постоянное наблюдение")
    run_parser.add_argument("--once", action="store_true", help="Один цикл проверки")
    export = sub.add_parser("export", help="Экспортировать локальную базу в Excel")
    export.add_argument("--month", help="Месяц публикаций, например 2026-11")
    export.add_argument("--output", type=Path)
    add = sub.add_parser("add", help="Вручную добавить ссылку на канал или пост")
    add.add_argument("reference")
    imp = sub.add_parser("import-file", help="Импортировать Telegram-ссылки из PDF, DOCX или XLSX")
    imp.add_argument("path", type=Path)
    preview = sub.add_parser("preview", help="Проверить разбор сообщения или файла без Telegram и записи в базу")
    content = preview.add_mutually_exclusive_group(required=True)
    content.add_argument("--text")
    content.add_argument("--file", type=Path)
    sub.add_parser("review", help="Список заявок, требующих уточнения")
    sub.add_parser("campaign-review", help="Очередь распознавания: сомнительные совпадения и ошибки; без подключения Telegram")
    resolve = sub.add_parser("resolve", help="Уточнить одну заявку и запустить её мониторинг")
    resolve.add_argument("id", type=int)
    resolve.add_argument("--start", required=True, help="YYYY-MM-DD HH:MM, местное время из config.yaml")
    resolve.add_argument("--end", required=True)
    resolve.add_argument("--keyword", action="append", help="Можно повторить; заменяет признаки из заявки")
    resolve.add_argument("--domain", action="append")
    resolve.add_argument("--hours", type=float, help="Длительность нахождения рекламы в канале")
    dismiss = sub.add_parser("dismiss", help="Отклонить ненужную заявку из очереди уточнения")
    dismiss.add_argument("id", type=int)
    sub.add_parser("status", help="Показать количество партнёров и публикаций")
    sub.add_parser("sync-google", help="Синхронизировать данные с отдельными вкладками Google Sheets")
    google_login = sub.add_parser("google-login", help="Локальный OAuth-вход для личного Google Drive и Google Sheets")
    google_login.add_argument("--client-secrets", type=Path, required=True, help="OAuth JSON для Desktop app из Google Cloud")
    sub.add_parser("demo", help="Создать отдельный Excel с явно вымышленным примером")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "init":
            template = Path(__file__).resolve().parent.parent / "config.example.yaml"
            content = template.read_text(encoding="utf-8") if template.exists() else "source_group: ''\nkeywords: []\ndomains: []\ntimezone: Asia/Tashkent\n"
            with args.config.open("x", encoding="utf-8") as out:
                out.write(content)
            print(f"Создан {args.config}. Укажите source_group; доступы задайте локально.")
            return 0
        config = load_config(args.config)
        if args.command == "check-config":
            print("Конфигурация корректна.")
            print("Рабочая группа: " + ("указана" if config.source_group else "нужно заполнить source_group перед run"))
            print("Telegram: " + ("переменные заданы" if os.environ.get("TELEGRAM_API_ID") and os.environ.get("TELEGRAM_API_HASH") else "нужны TELEGRAM_API_ID и TELEGRAM_API_HASH перед login"))
            print("Google: " + ("настроен путь к credentials" if config.google_credentials else "не подключён; Excel доступен без Google"))
            variable = "GEMINI_API_KEY" if config.campaign_provider == "gemini" else "OPENAI_API_KEY"
            print("Распознавание кампаний: " + (config.campaign_provider + "; ключ " + ("задан" if os.environ.get(variable) else "не задан") if config.campaign_matching else "выключено"))
        elif args.command in {"login", "dialogs"}:
            asyncio.run(telegram_command(config, args.command))
        elif args.command == "run":
            from .engine import run
            asyncio.run(run(config, once=args.once))
        elif args.command == "demo":
            demo(config.root, config.timezone)
        elif args.command == "preview":
            from dataclasses import asdict
            from datetime import datetime, timezone
            from .briefs import parse_brief
            from .parsing import parse_partner_references
            if args.file:
                from .documents import extract_document
                text = extract_document(args.file)
            else:
                text = args.text
            refs = parse_partner_references(text)
            brief = parse_brief(text, refs, datetime.now(timezone.utc), config.timezone,
                                config.default_window_days, config.keywords, config.domains)
            print(json.dumps({"channels": [asdict(r) for r in refs], "brief": asdict(brief)}, ensure_ascii=False, indent=2))
        elif args.command == "google-login":
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secrets), scopes=[
                "https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
            credentials = flow.run_local_server(host="127.0.0.1", port=0, access_type="offline", prompt="consent")
            output = config.google_credentials or config.root / "secrets" / "google.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(credentials.to_json(), encoding="utf-8")
            print(f"Доступ Google сохранён в {output}. В config.yaml укажите google_credentials: secrets/google.json и ID папки Drive.")
        else:
            with Store(config.db_path) as store:
                if args.command in {"add", "import-file"}:
                    from datetime import datetime, timezone
                    from .parsing import parse_partner_references
                    if args.command == "import-file":
                        from .documents import extract_document
                        text = extract_document(args.path)
                    else:
                        text = args.reference
                    refs = parse_partner_references(text)
                    ids = store.register_brief(refs, text, "manual", time.time_ns(), datetime.now(timezone.utc), config)
                    print(f"Обработано ссылок: {len(refs)}. Вступление в каналы не выполняется.")
                    print("Заявки: " + ", ".join(map(str, ids)))
                    if not refs:
                        print("Telegram-ссылок не найдено. Добавьте явную ссылку командой add.")
                elif args.command == "review":
                    for row in store.rows("""SELECT b.id,p.title,b.name,b.review_reason FROM placements b
                      JOIN partners p ON p.id=b.partner_id WHERE b.status='needs_review' ORDER BY b.id"""):
                        print(f"{row['id']}\t{row['title']}\t{row['name']}\t{row['review_reason']}")
                    for row in store.rows("SELECT id,title,last_error FROM partners WHERE status='needs_review'"):
                        print(f"Вложение {row['id']}\t{row['title']}\t{row['last_error']}")
                elif args.command == "resolve":
                    from .briefs import parse_time
                    if args.hours is not None and (not math.isfinite(args.hours) or not 0 < args.hours <= 8760):
                        raise ValueError("--hours: положительное число, не больше 8760")
                    rows = store.rows("SELECT * FROM placements WHERE id=?", (args.id,))
                    if not rows:
                        raise ValueError("Заявка не найдена")
                    row = rows[0]
                    if not json.loads(row["direct_ids"]) and not (args.keyword or args.domain or json.loads(row["keywords"]) or json.loads(row["domains"]) or json.loads(row.get("media_keys") or "[]")):
                        raise ValueError("Укажите --keyword или --domain для поиска рекламы")
                    store.resolve_placement(args.id, start=parse_time(args.start, config.timezone).isoformat(),
                        end=parse_time(args.end, config.timezone).isoformat(), keywords=args.keyword,
                        domains=args.domain, retention_hours=args.hours)
                    print(f"Заявка {args.id} уточнена. Проверка начнётся при следующем цикле.")
                elif args.command == "dismiss":
                    result = store.execute("UPDATE placements SET status='dismissed' WHERE id=?", (args.id,))
                    if not result.rowcount:
                        raise ValueError("Заявка не найдена")
                    print(f"Заявка {args.id} отклонена.")
                elif args.command == "export":
                    from .reports import export_xlsx
                    path = export_xlsx(*store.dump(), args.output or config.runtime_dir / "report.xlsx", timezone=config.timezone, month=args.month)
                    print(path)
                elif args.command == "status":
                    partners, posts, snapshots = store.dump()
                    print(f"Партнёры: {len(partners)}; публикации: {len(posts)}; измерения: {len(snapshots)}")
                    for row in store.rows("SELECT status,COUNT(*) AS n FROM partners GROUP BY status"):
                        print(f"  {row['status']}: {row['n']}")
                    for row in store.rows("SELECT status,COUNT(*) AS n FROM placements GROUP BY status"):
                        print(f"  заявки {row['status']}: {row['n']}")
                    for row in store.rows("SELECT status,COUNT(*) AS n FROM campaign_checks GROUP BY status"):
                        print(f"  распознавание {row['status']}: {row['n']}")
                elif args.command == "campaign-review":
                    rows = store.rows("""SELECT c.id,p.username,c.message_ids,c.status,c.reason FROM campaign_checks c
                        JOIN partners p ON p.id=c.partner_id WHERE c.status IN ('review','error','pending','local_checked','offline_review') ORDER BY c.created_at LIMIT 100""")
                    print(json.dumps(rows, ensure_ascii=False, indent=2))
                elif args.command == "sync-google":
                    from .reports import GoogleReporter
                    if not config.google_credentials or not config.spreadsheet_id:
                        raise ValueError("Заполните google_credentials и spreadsheet_id в конфигурации")
                    reporter = GoogleReporter(config.google_credentials, config.spreadsheet_id, config.drive_folder_id, config.timezone)
                    links = reporter.sync(*store.dump())
                    for sid, url in links.items():
                        store.execute("UPDATE snapshots SET screenshot_url=? WHERE id=?", (url, int(sid)))
                    print("Обновлён отчёт: сводка, публикации со скриншотами и недоступные посты.")
        return 0
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return 130
    except Exception as exc:
        # Google/browser exceptions can contain URLs with credentials; do not dump them.
        message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError, FileExistsError)) else type(exc).__name__
        print(f"Ошибка: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
