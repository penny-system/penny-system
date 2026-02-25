#!/usr/bin/env python3
"""
dev_tools.py — Pre-flight validation toolkit for penny-system.
Run from project root: python dev_tools.py

Checks: syntax · imports · lint · env · db · telegram (skipped)
Writes: dev_report.txt
"""

import importlib
import os
import py_compile
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Windows cp1252 consoles can't print emoji — force UTF-8 output
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ─── Paths ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.resolve()
TRADER_BOT   = PROJECT_ROOT / "Trader_bot"
ENV_FILE     = PROJECT_ROOT / ".env"
REPORT_FILE  = PROJECT_ROOT / "dev_report.txt"

MAIN_MODULES = [
    "config",
    "main_daily_run",
    "notify_scan",
    "telegram_bot",
    "src_scoring",
    "src_risk",
    "src_sizing",
    "src_features",
    "approve",
]

REQUIRED_ENV_KEYS = [
    "IBKR_ACCOUNT",
    "OPENAI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "MARKETAUX_API_KEY",
]

SKIP_DIRS = {"venv", "__pycache__", ".git", ".claude", "node_modules"}


# ─── File collection ─────────────────────────────────────────────────────────

def collect_py_files() -> list[Path]:
    py_files = []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                py_files.append(Path(root) / f)
    return sorted(py_files)


# ─── 1. SYNTAX ───────────────────────────────────────────────────────────────

def check_syntax(py_files: list[Path]) -> tuple[list[str], int, int]:
    pass_n, fail_n = 0, 0
    failures = []
    for fpath in py_files:
        rel = fpath.relative_to(PROJECT_ROOT)
        try:
            py_compile.compile(str(fpath), doraise=True)
            pass_n += 1
        except py_compile.PyCompileError as e:
            # Strip noisy preamble; keep first meaningful line
            msg = str(e).splitlines()[0]
            failures.append(f"  ❌ {rel}: {msg}")
            fail_n += 1

    lines: list[str] = []
    if failures:
        lines.append(f"  ✅ {pass_n} files pass")
        lines.extend(failures)
    else:
        lines.append(f"  ✅ All {pass_n} files pass — no syntax errors")
    return lines, pass_n, fail_n


# ─── 2. IMPORTS ──────────────────────────────────────────────────────────────

def check_imports() -> tuple[list[str], int, int]:
    lines: list[str] = []
    pass_n = fail_n = 0

    tb_str = str(TRADER_BOT)
    if tb_str not in sys.path:
        sys.path.insert(0, tb_str)

    for mod in MAIN_MODULES:
        # Clear cached version so each check is independent
        sys.modules.pop(mod, None)
        try:
            importlib.import_module(mod)
            lines.append(f"  ✅ {mod}")
            pass_n += 1
        except ImportError as e:
            lines.append(f"  ❌ {mod}: {e}")
            fail_n += 1
        except Exception as e:
            # Non-import errors (IBKR conn, missing files, etc.) — warn, not fail
            lines.append(f"  ⚠️  {mod}: {type(e).__name__} — {str(e)[:72]}")

    return lines, pass_n, fail_n


# ─── 3. LINT ─────────────────────────────────────────────────────────────────

def check_lint() -> tuple[list[str], int]:
    lines: list[str] = []
    warn_n = 0

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pyflakes", str(TRADER_BOT)],
            capture_output=True, text=True, timeout=30,
        )
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()

        # Detect "not installed" before counting warnings
        if "No module named pyflakes" in stderr or "No module named pyflakes" in stdout:
            lines.append("  ⚠️  pyflakes not installed — run: pip install pyflakes")
        else:
            raw = "\n".join(filter(None, [stdout, stderr]))
            if raw:
                all_warnings = [l for l in raw.splitlines() if l.strip()]
                warn_n = len(all_warnings)
                for w in all_warnings[:12]:
                    w_clean = w.replace(str(PROJECT_ROOT) + os.sep, "")
                    lines.append(f"  ⚠️  {w_clean}")
                if warn_n > 12:
                    lines.append(f"  … and {warn_n - 12} more (run: python -m pyflakes Trader_bot/)")
            else:
                lines.append("  ✅ No pyflakes warnings")
    except FileNotFoundError:
        lines.append("  ⚠️  pyflakes not installed — run: pip install pyflakes")
    except subprocess.TimeoutExpired:
        lines.append("  ⚠️  pyflakes timed out after 30 s")
    except Exception as e:
        lines.append(f"  ⚠️  pyflakes error: {e}")

    return lines, warn_n


# ─── 4. ENV ──────────────────────────────────────────────────────────────────

def check_env() -> tuple[list[str], int, int]:
    lines: list[str] = []
    pass_n = fail_n = 0

    if not ENV_FILE.exists():
        return [f"  ❌ .env not found at {ENV_FILE}"], 0, len(REQUIRED_ENV_KEYS)

    env_keys: set[str] = set()
    try:
        for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key = line.split("=", 1)[0].strip()
                env_keys.add(key)
    except Exception as e:
        return [f"  ❌ Cannot read .env: {e}"], 0, 1

    for key in REQUIRED_ENV_KEYS:
        if key in env_keys:
            lines.append(f"  ✅ {key}")
            pass_n += 1
        else:
            lines.append(f"  ❌ {key} — not found in .env")
            fail_n += 1

    return lines, pass_n, fail_n


# ─── 5. DB ───────────────────────────────────────────────────────────────────

def check_db() -> tuple[list[str], int, int]:
    lines: list[str] = []
    found_n = err_n = 0

    db_files: list[Path] = []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith((".sqlite", ".db")):
                db_files.append(Path(root) / f)

    if not db_files:
        return ["  ❌ No SQLite DB files found"], 0, 1

    active_db = TRADER_BOT / "output" / "trader.sqlite"

    for db_path in sorted(db_files):
        rel   = db_path.relative_to(PROJECT_ROOT)
        size  = db_path.stat().st_size // 1024
        tag   = " ← ACTIVE" if db_path.resolve() == active_db.resolve() else ""
        try:
            conn = sqlite3.connect(str(db_path))
            cur  = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [r[0] for r in cur.fetchall()]
            conn.close()
            lines.append(
                f"  ✅ {rel}  ({size} KB)"
                f"  tables: {', '.join(tables) if tables else 'none'}{tag}"
            )
            found_n += 1
        except Exception as e:
            lines.append(f"  ❌ {rel}: {e}")
            err_n += 1

    return lines, found_n, err_n


# ─── Report assembly ─────────────────────────────────────────────────────────

def build_report(sections: list[tuple[str, list[str]]]) -> str:
    out: list[str] = [
        f"DEV PRE-FLIGHT REPORT — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 57,
    ]

    failures: list[str] = []

    for title, items in sections:
        out.append(f"\n[{title}]")
        out.extend(items)
        for item in items:
            if "❌" in item:
                section_tag = title.split()[0]
                failures.append(f"  {section_tag}: {item.strip()}")

    out.append("\n" + "=" * 57)
    if failures:
        out.append("NEEDS ATTENTION:")
        out.extend(failures)
    else:
        out.append("✅ All checks passed — ready to go!")

    return "\n".join(out)


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    print("🔍 penny-system pre-flight check\n")

    py_files = collect_py_files()

    syntax_lines,  s_pass, s_fail  = check_syntax(py_files)
    import_lines,  i_pass, i_fail  = check_imports()
    lint_lines,    l_warn           = check_lint()
    env_lines,     e_pass, e_fail   = check_env()
    db_lines,      d_found, d_err   = check_db()

    sections: list[tuple[str, list[str]]] = [
        (f"SYNTAX   {s_pass}/{s_pass + s_fail} files",   syntax_lines),
        (f"IMPORTS  {i_pass}/{len(MAIN_MODULES)} pass",  import_lines),
        (f"LINT     {l_warn} warnings",                  lint_lines),
        (f"ENV      {e_pass}/{len(REQUIRED_ENV_KEYS)} keys present", env_lines),
        (f"DB       {d_found} found, {d_err} errors",   db_lines),
        ("TELEGRAM", ["  ⏭️  Skipped — no test bot token configured"]),
    ]

    report = build_report(sections)
    REPORT_FILE.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n📄 Saved → {REPORT_FILE}")


if __name__ == "__main__":
    main()
