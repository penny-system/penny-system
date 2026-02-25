import sys
import os

# Add Trader_bot root to Python path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import json
import os
import platform
import sqlite3
import time

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SYSTEM_STATE.json")
DB_PATH = os.path.normpath(getattr(config, "DB_PATH", None) or os.path.join(BASE_DIR, "output", "trader.sqlite"))
REDACT_KEYS = {"TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "MARKETAUX_API_KEY"}


def safe_config_dict():
    d = {}
    for k in dir(config):
        if k.startswith("_"):
            continue
        v = getattr(config, k)
        if callable(v):
            continue
        if k in REDACT_KEYS:
            d[k] = "***REDACTED***"
        else:
            # keep small/simple types
            if isinstance(v, (str, int, float, bool, type(None))):
                d[k] = v
    return d


def db_introspect(path: str):
    if not os.path.exists(path):
        return {"exists": False, "path": path}

    con = sqlite3.connect(path)
    cur = con.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]

    schema = {}
    for t in tables:
        cur.execute(f"PRAGMA table_info({t})")
        schema[t] = [r[1] for r in cur.fetchall()]

    # latest run id best-effort
    latest_run = None
    if "runs" in tables:
        try:
            cur.execute("SELECT MAX(id) FROM runs")
            latest_run = cur.fetchone()[0]
        except Exception:
            try:
                cur.execute("SELECT MAX(rowid) FROM runs")
                latest_run = cur.fetchone()[0]
            except Exception:
                latest_run = None

    con.close()
    return {
        "exists": True,
        "path": path,
        "tables": tables,
        "schema": schema,
        "latest_run": latest_run,
    }


def main():
    snap = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "paths": {
            "cwd": os.getcwd(),
            "db_path": DB_PATH,
        },
        "config": safe_config_dict(),
        "db": db_introspect(DB_PATH),
  
"objective": "Wire OpenAI QC triggers A & C; validate morning/hourly automation",
  }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2)

    print(f"✅ Wrote snapshot: {OUT}")


if __name__ == "__main__":
    main()