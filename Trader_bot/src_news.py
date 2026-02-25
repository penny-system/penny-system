from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

try:
    import config
except ImportError:
    from Trader_bot import config

RISK_KEYWORDS = [
    "offering", "registered direct", "public offering", "warrant", "dilution",
    "reverse split", "split", "bankruptcy", "going concern", "halt",
    "sec", "investigation", "lawsuit"
]

POS_KEYWORDS = [
    "contract", "partnership", "acquisition", "merger", "fda", "approval",
    "phase", "guidance", "beats", "raises outlook"
]


def _contains_any(text: str, keywords: list[str]) -> bool:
    t = (text or "").lower()
    return any(k in t for k in keywords)

def _count_hits(text: str, keywords: list[str]) -> int:
    t = (text or "").lower()
    hits = 0
    for k in keywords:
        if k in t:
            hits += 1
    return hits

def _clean_headline(h: str) -> str:
    """
    IBKR headlines often look like:
      "{A:...}!Some Headline"
    We keep only the part after '!'.
    """
    if not h:
        return ""
    if "!" in h:
        return h.split("!", 1)[1].strip()
    return h.strip()


def news_summary_for_contract(ib, contract) -> dict:
    if not getattr(config, "NEWS_ENABLED", True):
        return {"count": 0, "risk_hits": 0, "pos_hits": 0, "top_headline": ""}

    lookback_hours = int(getattr(config, "NEWS_LOOKBACK_HOURS", 72))
    max_items = int(getattr(config, "NEWS_MAX_HEADLINES", 20))

    try:
        providers = ib.reqNewsProviders()
        if not providers:
            return {"count": 0, "risk_hits": 0, "pos_hits": 0, "top_headline": ""}

        provider_codes = "+".join([p.code for p in providers[:5]])

        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=lookback_hours)

        start_str = start.strftime("%Y%m%d-%H:%M:%S")
        end_str = now.strftime("%Y%m%d-%H:%M:%S")

        news = ib.reqHistoricalNews(
            contract.conId,
            provider_codes,
            start_str,
            end_str,
            max_items,
        )

        if not news:
            return {"count": 0, "risk_hits": 0, "pos_hits": 0, "top_headline": ""}

        raw = [n.headline for n in news if getattr(n, "headline", None)]
        headlines = [_clean_headline(h) for h in raw if _clean_headline(h)]


        count = len(headlines)
        risk_hits = sum(1 for h in headlines if _contains_any(h, RISK_KEYWORDS))
        pos_hits = sum(1 for h in headlines if _contains_any(h, POS_KEYWORDS))
        trigger_a_hits = [h for h in headlines if _count_hits(h, RISK_KEYWORDS) >= 2]
        top = headlines[0] if headlines else ""

        return {
            "count": count,
            "risk_hits": risk_hits,
            "pos_hits": pos_hits,
            "top_headline": top,
            "trigger_a_hits": trigger_a_hits,
        }

    except Exception:
        return {
            "count": 0,
            "risk_hits": 0,
            "pos_hits": 0,
            "top_headline": "",
            "trigger_a_hits": [],
        }


import re as _re
import requests

# Keywords specifically relevant to SEC dilution / toxic financing filings
SEC_RISK_KEYWORDS = [
    "s-1", "s-3", "registration statement", "convertible", "preferred stock",
    "promissory note", "variable rate", "toxic", "equity line", "at-the-market",
    "atm offering", "shelf registration"
]


def _fetch_rss_titles(url: str, timeout: int = 10) -> list[str]:
    """Fetch an RSS/Atom feed and return a flat list of entry titles."""
    try:
        headers = {"User-Agent": "PennySystemBot/1.0 (research; contact@example.com)"}
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content.decode("iso-8859-1", errors="replace"))
        titles = []
        # Atom namespace — prefer summary over title (richer for EDGAR entries)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            form_el = entry.find("atom:title", ns)
            summ_el = entry.find("atom:summary", ns)
            form = (form_el.text or "").strip() if form_el is not None else ""
            # Strip HTML tags from summary to get plain item descriptions
            raw = (summ_el.text or "") if summ_el is not None else ""
            summary = " ".join(_re.sub(r"<[^>]+>", " ", raw).split())
            label = f"{form} — {summary}" if summary else form
            if label:
                titles.append(label)
        # RSS (no namespace)
        if not titles:
            for item in root.findall(".//item"):
                t = item.find("title")
                if t is not None and t.text:
                    titles.append(t.text.strip())
        return titles
    except Exception:
        return []


def fetch_sec_filings(symbol: str, limit: int = 10) -> list[str]:
    """Return recent SEC filing titles for *symbol* via EDGAR company Atom feed."""
    atom_url = (
        f"https://www.sec.gov/cgi-bin/browse-edgar"
        f"?action=getcompany&CIK={symbol}&type=8-K&dateb=&owner=include"
        f"&count={limit}&search_text=&output=atom"
    )
    titles = _fetch_rss_titles(atom_url)
    return titles[:limit]


def _analyze_titles(titles: list[str], source_label: str) -> dict:
    """Run keyword analysis over a list of titles; returns partial result dicts."""
    risk_hits = []
    positive_hits = []
    trigger_a_hits = []
    sec_hits = []

    for title in titles:
        if _contains_any(title, RISK_KEYWORDS):
            risk_hits.append(f"[{source_label}] {title}")
        if _contains_any(title, POS_KEYWORDS):
            positive_hits.append(f"[{source_label}] {title}")
        if _count_hits(title, RISK_KEYWORDS) >= 2:
            trigger_a_hits.append(f"[{source_label}] {title}")
        if _contains_any(title, SEC_RISK_KEYWORDS):
            sec_hits.append(f"[{source_label}] {title}")

    return {
        "risk_hits": risk_hits,
        "positive_hits": positive_hits,
        "trigger_a_hits": trigger_a_hits,
        "sec_hits": sec_hits,
    }


def fetch_and_analyze_news(symbol: str, limit: int = 5):
    """
    Fetch recent headlines from Marketaux + SEC EDGAR and analyze using keyword logic.

    Returns:
        {
            "risk_hits": [...],
            "positive_hits": [...],
            "headlines": [...],
            "trigger_a_hits": [...],
            "sec_hits": [...],      # SEC dilution/toxic filing signals
        }
    """
    risk_hits: list[str] = []
    positive_hits: list[str] = []
    headlines: list[str] = []
    trigger_a_hits: list[str] = []
    sec_hits: list[str] = []

    # --- Source 1: Marketaux ---
    if config.MARKETAUX_API_KEY:
        try:
            url = "https://api.marketaux.com/v1/news/all"
            params = {
                "api_token": config.MARKETAUX_API_KEY,
                "symbols": symbol,
                "limit": limit,
                "language": "en",
            }
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                articles = r.json().get("data", [])
                for article in articles:
                    title = article.get("title", "")
                    desc = article.get("description", "")
                    combined = f"{title} {desc}"
                    headlines.append(title)
                    if _contains_any(combined, RISK_KEYWORDS):
                        risk_hits.append(title)
                    if _contains_any(combined, POS_KEYWORDS):
                        positive_hits.append(title)
                    if _count_hits(combined, RISK_KEYWORDS) >= 2:
                        trigger_a_hits.append(title)
                    if _contains_any(combined, SEC_RISK_KEYWORDS):
                        sec_hits.append(title)
        except Exception:
            pass

    # --- Source 2: SEC EDGAR (8-K, S-1, S-3) ---
    try:
        sec_titles = fetch_sec_filings(symbol, limit=10)
        if sec_titles:
            partial = _analyze_titles(sec_titles, "SEC")
            headlines.extend(sec_titles)
            risk_hits.extend(partial["risk_hits"])
            positive_hits.extend(partial["positive_hits"])
            trigger_a_hits.extend(partial["trigger_a_hits"])
            sec_hits.extend(partial["sec_hits"])
    except Exception:
        pass

    return {
        "risk_hits": risk_hits,
        "positive_hits": positive_hits,
        "headlines": headlines,
        "trigger_a_hits": trigger_a_hits,
        "sec_hits": sec_hits,
    }
