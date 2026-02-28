from ib_insync import Stock
from pathlib import Path
import config
import csv


def load_static_universe() -> list[str]:
    """
    Load universe with priority:
      1st: universe_dynamic.txt  (written by src_universe_refresh.py weekly)
      2nd: scan_*.csv files      (manually exported scans)
      3rd: universe_static.txt   (final hardcoded fallback)

    Logs which source was used and how many symbols were loaded.
    """
    base = Path(".")
    all_symbols = []
    source_used = None

    # Priority 1: universe_dynamic.txt
    dynamic_txt = base / "universe_dynamic.txt"
    if dynamic_txt.exists():
        lines = [
            line.strip().upper()
            for line in dynamic_txt.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if lines:
            all_symbols = lines
            source_used = "universe_dynamic.txt"

    # Priority 2: scan_*.csv files
    if not all_symbols:
        for file in base.glob("scan_*.csv"):
            try:
                with file.open("r", encoding="utf-8-sig", newline="") as f:
                    reader = csv.reader(f)
                    next(reader, None)  # skip header
                    for row in reader:
                        if not row:
                            continue
                        sym = row[0].strip().upper()
                        if sym and sym.isalnum():
                            all_symbols.append(sym)
            except Exception:
                continue
        if all_symbols:
            source_used = "scan_*.csv"

    # Priority 3: universe_static.txt
    if not all_symbols:
        txt = base / "universe_static.txt"
        if txt.exists():
            for line in txt.read_text(encoding="utf-8").splitlines():
                t = line.strip().upper()
                if t and not t.startswith("#"):
                    all_symbols.append(t)
        if all_symbols:
            source_used = "universe_static.txt"

    # Deduplicate preserving order
    universe = list(dict.fromkeys(all_symbols))

    print(f"[Universe] Loaded from: {source_used or 'none'} -- {len(universe)} symbols")
    return universe[:config.STATIC_POOL_MAX]


def dynamic_scan_symbols(ib) -> list[str]:
    """
    Disabled (we are using exported scan CSVs).
    """
    return []


def qualify_contracts(ib, symbols: list[str]):
    contracts = [Stock(sym, "SMART", "USD") for sym in symbols]
    qualified = ib.qualifyContracts(*contracts)
    return list(qualified)
