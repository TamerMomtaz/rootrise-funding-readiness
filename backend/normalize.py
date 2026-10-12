"""
Pure helpers shared by the API, the migrations and the one-off scripts.

Nothing in here touches the database, so it is safe to import from anywhere
(and easy to unit-test).
"""

import re
from datetime import datetime, timezone, date
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import urlparse

STARTUP_TYPES = ['accelerator', 'grant', 'vc_fund', 'angel', 'corporate',
                 'government', 'competition', 'cloud_credits']
BD_TYPES = ['rfp', 'tender', 'consulting', 'procurement']


# ── Datetimes ─────────────────────────────────────────────────────────────
# Storage format for every datetime written from v4.0 on: "YYYY-MM-DDTHH:MM:SS+00:00".
# Older rows may hold naive ISO strings (written by datetime.utcnow()) — those were
# always UTC, so to_utc() treats naive values as UTC rather than rewriting the rows.

_DATE_ONLY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_YEAR_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


def to_utc(value) -> Optional[datetime]:
    """Parse anything date-like into an aware UTC datetime, or None if it can't be parsed."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        s = value.strip()
        # Hunter v3.3 appends "T00:00:00" to whatever the model returned ("RollingT00:00:00").
        if s.endswith("T00:00:00") and not re.match(r"^\d{4}-\d{2}-\d{2}T", s):
            s = s[: -len("T00:00:00")]
        m = _DATE_ONLY.match(s)
        if m:
            try:
                dt = datetime(int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                return None
        else:
            m = _YEAR_MONTH.match(s)
            if m:
                try:
                    dt = datetime(int(m[1]), int(m[2]), 1)
                except ValueError:
                    return None
            else:
                try:
                    dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
                except ValueError:
                    return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_utc(value) -> Optional[str]:
    dt = to_utc(value)
    return dt.replace(microsecond=0).isoformat() if dt else None


# ── Dedup keys (kept identical to Hunter v3.3 so both sides agree) ─────────

def normalize_url(url: Optional[str]) -> str:
    """'https://www.Example.com/path/?utm=x#y' -> 'example.com/path'."""
    if not url:
        return ""
    parsed = urlparse(url.strip().lower().rstrip('/'))
    path = re.sub(r'[?#].*', '', parsed.path).rstrip('/')
    netloc = parsed.netloc.replace('www.', '')
    return f"{netloc}{path}" if netloc else ""


def canonicalize_name(name: str) -> str:
    name = (name or "").lower().strip()
    name = re.sub(r'\b202[0-9]\b', '', name)
    name = re.sub(r'\b(cohort|batch|class|round|cycle|edition)\s*\d+', '', name)
    name = re.sub(r'\b(cohort|batch|class|round|cycle|edition)\s*[ivxlcdm]+\b', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\bfor\s+startups?\b', 'startups', name)
    name = re.sub(r'\s+(program|programme|initiative|applications?)\s*$', '', name)
    name = re.sub(r'[^a-z0-9\s]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return ' '.join(w for w in name.split() if len(w) > 2)


def title_similarity(a: str, b: str) -> float:
    t1 = re.sub(r'[^a-z0-9\s]', '', (a or "").lower().strip())
    t2 = re.sub(r'[^a-z0-9\s]', '', (b or "").lower().strip())
    return SequenceMatcher(None, t1, t2).ratio()


# Fuzzy matches below this were real, distinct programmes in the live data
# (e.g. "Techstars Boston" vs "Techstars London" = 0.89).
FUZZY_DUPLICATE_THRESHOLD = 0.92


def is_duplicate(a: dict, b: dict) -> Optional[str]:
    """Return the reason two opportunity dicts are the same programme, or None."""
    ua, ub = normalize_url(a.get("url")), normalize_url(b.get("url"))
    if ua and ua == ub:
        return "url"
    ca, cb = canonicalize_name(a.get("name", "")), canonicalize_name(b.get("name", ""))
    if ca and len(ca) > 10 and ca == cb:
        return "canonical_name"
    if a.get("type") == b.get("type") and \
            title_similarity(a.get("name", ""), b.get("name", "")) >= FUZZY_DUPLICATE_THRESHOLD:
        return "fuzzy_name"
    return None


# ── Notes tags written by the Hunter: "[📋 BD | 🔒 NEEDS CHECK | Auto: WebSearch] …" ──

_TAG = re.compile(r"^\s*\[([^\]]*)\]")


def parse_notes_tags(notes: Optional[str]) -> dict:
    out = {"category": None, "source": None, "needs_check": False, "access_status": None}
    if not notes:
        return out
    m = _TAG.match(notes)
    if not m:
        return out
    for i, part in enumerate(p.strip() for p in m.group(1).split("|")):
        up = part.upper()
        if i == 0:  # the category is always the first segment
            if "📋" in part or re.search(r"\bBD\b", up):
                out["category"] = "bd"
            elif "🚀" in part or "STARTUP" in up:
                out["category"] = "startup"
        if "NEEDS CHECK" in up:
            out["needs_check"] = True
            out["access_status"] = "auth_required"
        elif "BLOCKED" in up:
            out["access_status"] = "blocked"
        elif "TIMEOUT" in up:
            out["access_status"] = "timeout"
        if part.lower().startswith("auto:"):
            out["source"] = part.split(":", 1)[1].strip() or None
    return out


def category_for_type(type_: Optional[str]) -> str:
    return "bd" if type_ in BD_TYPES else "startup"
