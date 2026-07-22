"""Auto-fetched news dates from ForexFactory's free weekly calendar feed.

Keeps the Morning Plan's event calendar current without anyone maintaining
``econ_events.json`` by hand: at launch the app pulls FF's public weekly XML
(this week + next week), keeps only the release types the spike model was
trained on, and caches them in ``config_dir()/analysis/news_feed.json``.
``calendar.event_flag`` merges the cache with the curated JSON and derived
rules (curated/derived win ties).

The filter to KNOWN release types is deliberate: ``news_score`` is a model
feature, and letting arbitrary calendar entries through would inflate it
relative to how the model was fitted. NFP and jobless claims are excluded
here too — those are derived by rule in calendar.py and would double-count.

Best-effort everywhere: no network, bad XML, or a bad cache never raises —
mornings offline simply run on the last cached fetch plus the curated JSON.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..config import config_dir

FEED_URLS = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.xml",
)
_CACHE_NAME = "news_feed.json"
_KEEP_DAYS = 45  # prune cached entries older than this

# FF title substring (lowercase) -> curated key in calendar._CURATED.
# Order matters: first match wins ("core pce" before generic patterns).
_TITLE_KEYS: List[Tuple[str, str]] = [
    ("core pce", "pce"),
    ("pce price", "pce"),
    ("cpi", "cpi"),
    ("ppi", "ppi"),
    ("retail sales", "retail"),
    ("advance gdp", "gdp"),
    ("prelim gdp", "gdp"),
    ("final gdp", "gdp"),
    ("gdp ", "gdp"),
    ("ism manufacturing pmi", "ism_mfg"),
    ("ism services pmi", "ism_svc"),
    ("ism non-manufacturing", "ism_svc"),
    ("federal funds rate", "fomc"),
    ("fomc statement", "fomc"),
]


def _cache_path() -> Path:
    return config_dir() / "analysis" / _CACHE_NAME


def _http_get_bytes(url: str, timeout: float) -> bytes:
    import requests
    from .. import __version__
    r = requests.get(url, timeout=timeout,
                     headers={"User-Agent": f"imbabot/{__version__}"})
    r.raise_for_status()
    return r.content


def _norm_time(raw: str) -> Optional[str]:
    """'8:30am' / '2:00pm' -> zero-padded 24h 'HH:MM'; 'All Day'/'Tentative' -> None."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(am|pm)\s*$", (raw or "").lower())
    if not m:
        return None
    h, mnt, ap = int(m.group(1)), m.group(2), m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return f"{h:02d}:{mnt}"


def _title_key(title: str) -> Optional[str]:
    t = (title or "").lower()
    for sub, key in _TITLE_KEYS:
        if sub in t:
            return key
    return None


def parse_feed(xml_bytes: bytes) -> Dict[str, List[Tuple[str, Optional[str]]]]:
    """FF weekly XML -> {iso_date: [(curated_key, 'HH:MM'|None), ...]} (USD, known types only)."""
    import xml.etree.ElementTree as ET
    out: Dict[str, List[Tuple[str, Optional[str]]]] = {}
    head = xml_bytes.lstrip()[:200].lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html"):
        return out          # rate-limit/error page served as 200 — no data this round
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        # FF declares encoding="windows-1252", which expat can't decode natively.
        # Decode ourselves and drop the declaration so ET parses plain text.
        text = xml_bytes.decode("cp1252", "replace")
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1)
        root = ET.fromstring(text)
    for ev in root.iter("event"):
        get = lambda tag: (ev.findtext(tag) or "").strip()
        if get("country").upper() != "USD":
            continue
        key = _title_key(get("title"))
        if key is None:
            continue
        try:  # feed dates are MM-DD-YYYY
            iso = datetime.strptime(get("date"), "%m-%d-%Y").date().isoformat()
        except ValueError:
            continue
        pair = (key, _norm_time(get("time")))
        day = out.setdefault(iso, [])
        if all(p[0] != key for p in day):  # one entry per type per day (FOMC has several rows)
            day.append(pair)
    return out


def _read_cache() -> dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def cached_events(iso_date: str) -> List[Tuple[str, Optional[str]]]:
    """Feed events cached for one ISO date: [(curated_key, 'HH:MM'|None), ...]."""
    day = (_read_cache().get("events") or {}).get(iso_date) or []
    out = []
    for item in day:
        if isinstance(item, (list, tuple)) and item and isinstance(item[0], str):
            out.append((item[0], item[1] if len(item) > 1 else None))
    return out


def fetch(*, get_bytes: Callable[[str, float], bytes] = _http_get_bytes,
          log: Optional[Callable[..., None]] = None, timeout: float = 10.0,
          max_age_hours: float = 6.0) -> bool:
    """Pull this week's + next week's feed and merge into the cache. Never raises.

    Skips entirely while the cache is younger than ``max_age_hours`` — the feed
    host 429s aggressive polling, and relaunching the app five times in a row
    (e.g. during updates) must not look like polling.
    """
    if max_age_hours > 0:
        try:
            fetched = datetime.fromisoformat(_read_cache().get("fetched", ""))
            if datetime.now() - fetched < timedelta(hours=max_age_hours):
                return False        # fresh enough; don't touch the network
        except Exception:
            pass
    fresh: Dict[str, List[Tuple[str, Optional[str]]]] = {}
    for i, url in enumerate(FEED_URLS):
        try:
            if i:
                import time
                time.sleep(2.0)     # be polite between the two weekly files
            fresh.update(parse_feed(get_bytes(url, timeout)))
        except Exception:
            continue  # one week failing shouldn't lose the other
    if not fresh:
        if log:
            log("News feed fetch: nothing retrieved (offline?) — using cached dates.", "warn")
        return False
    try:
        cache = _read_cache()
        events = cache.get("events") or {}
        events.update({d: [list(p) for p in pairs] for d, pairs in fresh.items()})
        cutoff = (datetime.now().date() - timedelta(days=_KEEP_DAYS)).isoformat()
        events = {d: v for d, v in sorted(events.items()) if d >= cutoff}
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched": datetime.now().isoformat(timespec="seconds"),
                                    "events": events}, indent=1), encoding="utf-8")
        if log:
            n = sum(len(v) for v in fresh.values())
            log(f"News feed updated: {n} tracked event(s) across {len(fresh)} day(s).")
        return True
    except Exception as exc:
        if log:
            log(f"News feed cache write failed: {exc}", "warn")
        return False
