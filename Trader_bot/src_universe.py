from ib_insync import Stock
from pathlib import Path
import config
import csv


def load_static_universe() -> list[str]:
    """
    Combine all scan_*.csv files into one symbol list.
    Assumes symbol is the FIRST column in each scan CSV.
    If no scan files exist, falls back to universe_static.txt.
    """
    base = Path(".")
    all_symbols = []

    # Load scan_*.csv files
    for file in base.glob("scan_*.csv"):
        try:
            with file.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)  # skip header
                for row in reader:
                    if not row:
                        continue
                    sym = row[0].strip().upper()
                    if sym and sym.isalnum():
                        all_symbols.append(sym)
        except Exception:
            continue

    # Fallback to universe_static.txt
    if not all_symbols:
        txt = Path("universe_static.txt")
        if txt.exists():
            for line in txt.read_text(encoding="utf-8").splitlines():
                t = line.strip().upper()
                if t and not t.startswith("#"):
                    all_symbols.append(t)

    # Deduplicate preserving order
    universe = list(dict.fromkeys(all_symbols))

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
