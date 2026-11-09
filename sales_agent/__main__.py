import argparse
import json
import logging
import os

from .config import Config
from .runner import run
from .store import Store

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
os.umask(0o077)
parser=argparse.ArgumentParser(description="Mediasales lead research and Google Sheets CRM")
parser.add_argument("command",choices=["run","once","status"],default="run",nargs="?")
args=parser.parse_args()
cfg=Config.load()
if args.command == "status":
    store=Store(cfg.data_dir / "sales.sqlite3")
    print(json.dumps({"leads":len(store.leads()),"last_synced":store.state("last_synced"),
        "last_researched":store.state("last_researched"),"next_retry":store.state("next_retry")},ensure_ascii=False))
    store.close()
else:
    run(cfg,once=args.command=="once")
