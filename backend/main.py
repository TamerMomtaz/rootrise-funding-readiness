"""
RootRise Funding Readiness System - Backend API
FastAPI application with SQLite database for tracking funding opportunities and readiness items

v4.0 — Oct 2026 (see CHANGELOG.md):
  + Structured opportunity fields (category, source, needs_check, access_status, url_normalized)
    with a versioned, additive migration that backfills them from the Hunter's notes tags
  + Lenient Hunter contract: unknown type/status, free-text deadlines and out-of-range
    scores are coerced and logged instead of rejected with 422
  + UTC-aware datetimes, explicit nulls clear fields, computed is_expired / days_left
  + Readiness status <-> completion % kept consistent; documented weighted score
v1.1 — Feb 13, 2026:
  + Saved/Starred Opportunities (toggle-save, saved filter, sidebar count)
  + Counts by Type endpoint (for filter tabs with dynamic counts)
"""

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Literal
from datetime import datetime, timedelta, timezone
from enum import Enum
import logging
import math
import sqlite3
import os
from contextlib import contextmanager, asynccontextmanager

try:
    from .normalize import (to_utc, iso_utc, normalize_url, parse_notes_tags, category_for_type,
                            is_duplicate, STARTUP_TYPES, BD_TYPES)
    from . import migrations, auth
except ImportError:  # running as `uvicorn main:app` from backend/
    from normalize import (to_utc, iso_utc, normalize_url, parse_notes_tags, category_for_type,
                           is_duplicate, STARTUP_TYPES, BD_TYPES)
    import migrations
    import auth

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("frd")

# ============================================
# Configuration
# ============================================

DATABASE_PATH = os.environ.get("DATABASE_PATH", "funding_readiness.db")
# API_KEY / TEAM_PASSCODE / AUTH_SECRET / AUTH_MODE are read by auth.py
APP_VERSION = "4.0.0"
CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS",
    "https://rootrise-funding-readiness.onrender.com,http://localhost:8000,http://127.0.0.1:8000",
).split(",") if o.strip()]


def utcnow() -> datetime:
    """Timezone-aware current UTC time (replaces deprecated datetime.utcnow())."""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().replace(microsecond=0).isoformat()

# ============================================
# Enums  (stored values are canonical; aliases below are accepted on input)
# ============================================

class OpportunityStatus(str, Enum):
    RESEARCHING = "researching"
    PREPARING = "preparing"
    SUBMITTED = "submitted"      # UI label: Applied
    IN_REVIEW = "in_review"      # UI label: Interview
    ACCEPTED = "accepted"        # UI label: Won
    REJECTED = "rejected"        # UI label: Lost
    CLOSED = "closed"

class OpportunityType(str, Enum):
    ACCELERATOR = "accelerator"
    GRANT = "grant"
    VC_FUND = "vc_fund"
    ANGEL = "angel"
    CORPORATE = "corporate"
    GOVERNMENT = "government"
    COMPETITION = "competition"
    CLOUD_CREDITS = "cloud_credits"
    RFP = "rfp"
    TENDER = "tender"
    CONSULTING = "consulting"
    PROCUREMENT = "procurement"
    OTHER = "other"

class ReadinessStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    BLOCKED = "blocked"

class ReadinessCategory(str, Enum):
    LEGAL = "legal"
    FINANCIAL = "financial"
    PITCH = "pitch"
    TECHNICAL = "technical"
    TEAM = "team"
    PRODUCT = "product"
    MARKET = "market"
    OPERATIONS = "operations"
    DOCUMENTATION = "documentation"

Category = Literal["startup", "bd"]

OPP_TYPE_ALIASES = {
    "vc": "vc_fund", "venture_capital": "vc_fund", "fund": "vc_fund", "investor": "vc_fund",
    "cloud": "cloud_credits", "credits": "cloud_credits", "cloud_credit": "cloud_credits",
    "incubator": "accelerator", "program": "accelerator", "programme": "accelerator",
    "fellowship": "grant", "funding": "grant",
    "challenge": "competition", "prize": "competition", "contest": "competition", "pitch_competition": "competition",
    "tor": "consulting", "consultancy": "consulting",
    "rfq": "procurement", "rfi": "rfp", "eoi": "rfp", "bid": "tender",
}
OPP_STATUS_ALIASES = {
    "applied": "submitted", "interview": "in_review", "under_review": "in_review",
    "won": "accepted", "lost": "rejected", "declined": "rejected",
    "new": "researching", "open": "researching", "expired": "closed",
}
READINESS_CATEGORY_ALIASES = {"pitch_materials": "pitch", "pitch_deck": "pitch", "tech": "technical",
                              "finance": "financial", "docs": "documentation", "ops": "operations"}
READINESS_STATUS_ALIASES = {"done": "complete", "completed": "complete", "todo": "not_started",
                            "to_do": "not_started", "doing": "in_progress", "wip": "in_progress"}

PRE_SUBMISSION = ("researching", "preparing")


def _key(value) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def coerce_choice(value, enum_cls, aliases: dict, default: str, field: str):
    """Map aliases / unknown values to a valid enum value, logging every coercion."""
    if value is None or isinstance(value, enum_cls):
        return value
    k = _key(value)
    allowed = {e.value for e in enum_cls}
    if k in allowed:
        return k
    if k in aliases:
        return aliases[k]
    log.warning("coerced unknown %s %r -> %r", field, value, default)
    return default


def _clamp_int(value, lo, hi, field):
    if value is None:
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number")
    if n < lo or n > hi:
        log.warning("clamped %s %r into [%d, %d]", field, value, lo, hi)
    return max(lo, min(hi, n))


# Whitelisted ORDER BY clauses. NULLs always sort last so rolling programs
# (no deadline) don't crowd the top of a deadline-ascending list.
def _order(col: str):
    return {"asc": f"({col} IS NULL OR {col} = '') ASC, {col} ASC, id ASC",
            "desc": f"({col} IS NULL OR {col} = '') ASC, {col} DESC, id DESC"}

OPPORTUNITY_SORTS = {k: _order(k) for k in ("deadline", "fit_score", "priority", "created_at", "updated_at", "name")}
READINESS_SORTS = {k: _order(k) for k in ("due_date", "priority", "status", "created_at", "category", "name")}

# ============================================
# Pydantic Models
# ============================================

class _OpportunityCoercion(BaseModel):
    """Lenient input for the Hunter and the UI: never 422 on fixable values."""

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data):
        if not isinstance(data, dict):
            return data
        d = dict(data)
        if "name" in d and isinstance(d["name"], str):
            d["name"] = d["name"].strip()
            if len(d["name"]) > 200:
                log.warning("truncated name %r", d["name"][:60])
                d["name"] = d["name"][:200].rstrip()
        if "type" in d:
            d["type"] = coerce_choice(d["type"], OpportunityType, OPP_TYPE_ALIASES, "other", "type")
        if "status" in d:
            d["status"] = coerce_choice(d["status"], OpportunityStatus, OPP_STATUS_ALIASES, "researching", "status")
        if "fit_score" in d:
            d["fit_score"] = _clamp_int(d["fit_score"], 0, 100, "fit_score")
        if "priority" in d:
            d["priority"] = _clamp_int(d["priority"], 1, 5, "priority")
        if "category" in d and d["category"] is not None:
            k = _key(d["category"])
            d["category"] = "bd" if k in ("bd", "business_development") else "startup" if k == "startup" else None
        if d.get("deadline") not in (None, ""):
            parsed = to_utc(d["deadline"])
            if parsed is None:
                log.warning("unparseable deadline %r -> null (kept as deadline_text)", d["deadline"])
                raw = str(d["deadline"])
                d["deadline_text"] = raw[:-9] if raw.endswith("T00:00:00") else raw
            d["deadline"] = parsed
        elif d.get("deadline") == "":
            d["deadline"] = None
        for k in ("url", "notes", "funding_amount", "requirements", "contact_info", "source"):
            if isinstance(d.get(k), str) and not d[k].strip():
                d[k] = None
        if "is_starred" in d and "is_saved" not in d:
            d["is_saved"] = d["is_starred"]
        return d


class OpportunityCreate(_OpportunityCoercion):
    name: str = Field(..., min_length=1, max_length=200)
    type: OpportunityType = OpportunityType.OTHER
    deadline: Optional[datetime] = None
    status: OpportunityStatus = OpportunityStatus.RESEARCHING
    fit_score: int = Field(default=50, ge=0, le=100)
    url: Optional[str] = None
    notes: Optional[str] = None
    funding_amount: Optional[str] = None
    requirements: Optional[str] = None
    contact_info: Optional[str] = None
    priority: int = Field(default=3, ge=1, le=5)
    # v4.0 structured fields (optional; derived from notes tags / type when absent)
    category: Optional[Category] = None
    source: Optional[str] = None
    needs_check: Optional[bool] = None
    access_status: Optional[str] = None
    is_saved: Optional[bool] = None
    deadline_text: Optional[str] = Field(None, description="Free-text deadline when it can't be parsed")


class OpportunityUpdate(_OpportunityCoercion):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    type: Optional[OpportunityType] = None
    deadline: Optional[datetime] = None
    status: Optional[OpportunityStatus] = None
    fit_score: Optional[int] = Field(None, ge=0, le=100)
    url: Optional[str] = None
    notes: Optional[str] = None
    funding_amount: Optional[str] = None
    requirements: Optional[str] = None
    contact_info: Optional[str] = None
    priority: Optional[int] = Field(None, ge=1, le=5)
    category: Optional[Category] = None
    source: Optional[str] = None
    needs_check: Optional[bool] = None
    access_status: Optional[str] = None
    is_saved: Optional[bool] = None
    deadline_text: Optional[str] = None


class Opportunity(BaseModel):
    id: int
    name: str
    type: str
    deadline: Optional[datetime] = None
    status: str
    fit_score: int
    url: Optional[str] = None
    notes: Optional[str] = None
    funding_amount: Optional[str] = None
    requirements: Optional[str] = None
    contact_info: Optional[str] = None
    priority: int
    category: str
    source: Optional[str] = None
    needs_check: bool = False
    access_status: Optional[str] = None
    is_saved: bool = False
    is_starred: bool = False
    is_expired: bool = False
    days_left: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class _ReadinessCoercion(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data):
        if not isinstance(data, dict):
            return data
        d = dict(data)
        if "category" in d:
            d["category"] = coerce_choice(d["category"], ReadinessCategory, READINESS_CATEGORY_ALIASES,
                                          "documentation", "readiness category")
        if "status" in d:
            d["status"] = coerce_choice(d["status"], ReadinessStatus, READINESS_STATUS_ALIASES,
                                        "not_started", "readiness status")
        if "completion_percentage" in d:
            d["completion_percentage"] = _clamp_int(d["completion_percentage"], 0, 100, "completion_percentage")
        if "priority" in d:
            d["priority"] = _clamp_int(d["priority"], 1, 5, "priority")
        if d.get("due_date") == "":
            d["due_date"] = None
        elif d.get("due_date") is not None:
            parsed = to_utc(d["due_date"])
            if parsed is None:
                raise ValueError("due_date must be an ISO date (YYYY-MM-DD)")
            d["due_date"] = parsed
        for k in ("owner", "description", "dependencies"):
            if isinstance(d.get(k), str) and not d[k].strip():
                d[k] = None
        return d


class ReadinessItemCreate(_ReadinessCoercion):
    name: str = Field(..., min_length=1, max_length=200)
    category: ReadinessCategory
    status: ReadinessStatus = ReadinessStatus.NOT_STARTED
    owner: Optional[str] = None
    due_date: Optional[datetime] = None
    description: Optional[str] = None
    priority: int = Field(default=3, ge=1, le=5)
    dependencies: Optional[str] = None
    completion_percentage: int = Field(default=0, ge=0, le=100)


class ReadinessItemUpdate(_ReadinessCoercion):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    category: Optional[ReadinessCategory] = None
    status: Optional[ReadinessStatus] = None
    owner: Optional[str] = None
    due_date: Optional[datetime] = None
    description: Optional[str] = None
    priority: Optional[int] = Field(None, ge=1, le=5)
    dependencies: Optional[str] = None
    completion_percentage: Optional[int] = Field(None, ge=0, le=100)


class ReadinessItem(BaseModel):
    id: int
    name: str
    category: str
    status: str
    owner: Optional[str] = None
    due_date: Optional[datetime] = None
    description: Optional[str] = None
    priority: int
    dependencies: Optional[str] = None
    completion_percentage: int
    is_overdue: bool = False
    created_at: datetime
    updated_at: datetime


class ActivityLog(BaseModel):
    id: int
    action: str
    entity_type: str
    entity_id: int
    details: Optional[str] = None
    timestamp: datetime


class DashboardStats(BaseModel):
    total_opportunities: int
    active_opportunities: int
    upcoming_deadlines: int
    expired_opportunities: int
    needs_check_count: int
    starred_count: int
    total_readiness_items: int
    completed_items: int
    in_progress_items: int
    overall_readiness_score: float
    readiness_formula: str
    opportunities_by_status: dict
    opportunities_by_type: dict
    opportunities_by_category: dict
    readiness_by_category: dict
    upcoming_opportunities: List[dict]
    urgent_items: List[dict]
    recent_activity: List[dict]

# ============================================
# Database Setup
# ============================================

def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL lets readers proceed while the Hunter writes; busy_timeout waits instead of
    # failing with "database is locked". Run a single uvicorn worker (see start.sh).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

@contextmanager
def db_session():
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def clean_row(row) -> dict:
    """Convert SQLite row to dict, handling empty strings as None for datetime fields"""
    d = dict(row)
    datetime_fields = ['deadline', 'due_date', 'created_at', 'updated_at', 'timestamp']
    nullable_fields = ['url', 'notes', 'funding_amount', 'requirements', 'contact_info',
                       'owner', 'description', 'dependencies', 'details']
    for field in datetime_fields + nullable_fields:
        if field in d and d[field] == '':
            d[field] = None
    for field in datetime_fields:
        if field in d and d[field] is not None:
            d[field] = to_utc(d[field])
    for flag in ('is_saved', 'needs_check'):
        if flag in d:
            d[flag] = bool(d[flag])
    return d


def opportunity_out(row, now: datetime = None) -> dict:
    """Row -> API dict with computed fields (is_starred, is_expired, days_left)."""
    d = clean_row(row)
    now = now or utcnow()
    d.pop("url_normalized", None)
    d["category"] = d.get("category") or category_for_type(d.get("type"))
    d["is_starred"] = d.get("is_saved", False)
    dl = d.get("deadline")
    d["days_left"] = math.floor((dl - now).total_seconds() / 86400) if dl else None
    d["is_expired"] = bool(dl and dl < now and d.get("status") in PRE_SUBMISSION)
    return d


def readiness_out(row, now: datetime = None) -> dict:
    d = clean_row(row)
    now = now or utcnow()
    due = d.get("due_date")
    d["is_overdue"] = bool(due and due < now and d.get("status") != "complete")
    return d


def init_db():
    """Initialize database with schema, apply migrations, seed only when empty."""
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                deadline TEXT,
                status TEXT NOT NULL DEFAULT 'researching',
                fit_score INTEGER DEFAULT 50,
                url TEXT,
                notes TEXT,
                funding_amount TEXT,
                requirements TEXT,
                contact_info TEXT,
                priority INTEGER DEFAULT 3,
                is_saved BOOLEAN DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS readiness_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'not_started',
                owner TEXT,
                due_date TEXT,
                description TEXT,
                priority INTEGER DEFAULT 3,
                dependencies TEXT,
                completion_percentage INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                details TEXT,
                timestamp TEXT NOT NULL
            )
        """)
        conn.commit()
        # Versioned, additive migrations (backs the DB up first when anything is pending)
        migrations.run_migrations(conn, DATABASE_PATH)
        # Seed if empty (fresh installs only — never touches existing rows)
        cursor.execute("SELECT COUNT(*) FROM opportunities")
        if cursor.fetchone()[0] == 0:
            seed_initial_data(cursor)


def seed_initial_data(cursor):
    """Seed a fresh install. Dates are relative to install time so the demo data
    never ships already expired (the v1 seeds were pinned to Jan 2026)."""
    now = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    stamp = now_iso()

    def in_days(n):
        return None if n is None else (now + timedelta(days=n)).isoformat()

    # (name, type, days_to_deadline, status, fit, url, notes, funding, requirements, priority)
    opportunities = [
        ("EBRD Star Venture", "accelerator", 45, "researching", 100,
         "https://www.ebrd.com/starventure", "18-month programme, no equity. Primary target for RootRise.",
         "No equity, mentorship + network", "Egypt registration, <5 years old, tech focus, B2B", 1),
        ("Google for Startups Accelerator: MENA", "accelerator", 20, "preparing", 85,
         "https://startup.google.com/programs/accelerator/middle-east-north-africa-turkey/",
         "Equity-free, AI-first cohort.", "Up to $350K Google Cloud credits",
         "Seed to Series A, AI/ML focus", 1),
        ("Flat6Labs Cairo", "accelerator", None, "researching", 60,
         "https://flat6labs.com/", "Rolling applications.",
         "Seed investment for equity", "Egyptian founder, MVP required", 3),
        ("EBRD Innovation Grant", "grant", None, "researching", 80,
         "https://www.ebrd.com/what-we-do/sectors/advice-small-businesses.html",
         "Up to €30K grants. Component-based applications.",
         "Up to €30K", "EBRD country operations, SME focus", 2),
        ("AWS Activate", "cloud_credits", None, "researching", 75,
         "https://aws.amazon.com/activate/", "Rolling. Credits plus technical support.",
         "Up to $100K AWS credits", "Early-stage startup", 2),
        ("UNICEF Digital Feedback Platform RFP", "rfp", 12, "preparing", 70,
         "https://www.ungm.org/", "[📋 BD | Auto: Seed] Example BD tender for DEVONEERS.",
         "Contract", "ICT4D track record, MENA presence", 2),
    ]
    for (name, type_, days, status, fit, url, notes, funding, reqs, prio) in opportunities:
        cursor.execute("""
            INSERT INTO opportunities
            (name, type, deadline, status, fit_score, url, notes, funding_amount, requirements, contact_info,
             priority, is_saved, category, source, needs_check, url_normalized, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, ?, 'seed', 0, ?, ?, ?)
        """, (name, type_, in_days(days), status, fit, url, notes, funding, reqs, prio,
              category_for_type(type_), normalize_url(url) or None, stamp, stamp))

    # (name, category, status, owner, due_in_days, description, priority, dependencies, pct)
    readiness_items = [
        ("Brand Identity", "documentation", "complete", "Tee", None, "Colour system and themes", 1, None, 100),
        ("Architecture Docs", "technical", "complete", "Tee", None, "Complete technical blueprint", 1, None, 100),
        ("Team Bios", "team", "complete", "Tee", None, "All founders documented", 2, None, 100),
        ("Company Registration", "legal", "complete", "Tee", None, "Egyptian commercial registration", 2, None, 100),
        ("Pitch Deck (10 slides)", "pitch", "in_progress", "Rouba", 14, "Target: 10 slides max.", 1, None, 60),
        ("Financial Projections", "financial", "in_progress", "Doaa", 21, "3-year model", 1, None, 40),
        ("One-Pager", "pitch", "not_started", None, 14, "Single A4 summary for investors", 1, "Pitch Deck", 0),
        ("Working Demo (MVD)", "product", "in_progress", "Tee", 10, "Single-flow minimum viable demo", 1, None, 50),
        ("Customer Testimonials", "market", "not_started", None, 45, "Target: 3 testimonials", 2, "Working Demo", 0),
        ("First 5 Pilots", "operations", "not_started", "Alaa", 60, "5 pilot SMEs", 1, "Working Demo", 0),
    ]
    for (name, cat, status, owner, due, desc, prio, deps, pct) in readiness_items:
        cursor.execute("""
            INSERT INTO readiness_items
            (name, category, status, owner, due_date, description, priority, dependencies, completion_percentage, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, cat, status, owner, in_days(due), desc, prio, deps, pct, stamp, stamp))


def log_activity(cursor, action: str, entity_type: str, entity_id: int, details: str = None):
    cursor.execute("""
        INSERT INTO activity_log (action, entity_type, entity_id, details, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (action, entity_type, entity_id, details, now_iso()))


# ============================================
# Readiness math (documented in README → "Readiness score")
# ============================================

READINESS_FORMULA = ("score = Σ(wᵢ·pᵢ) / Σwᵢ, wᵢ = 6 − priority (P1=5 … P5=1), "
                     "pᵢ = 100 if status = complete else completion_percentage")


def effective_progress(item: dict) -> int:
    return 100 if item.get("status") == "complete" else int(item.get("completion_percentage") or 0)


def readiness_score(items: List[dict]) -> float:
    total_w = sum(6 - int(i.get("priority") or 3) for i in items)
    if not total_w:
        return 0.0
    return sum((6 - int(i.get("priority") or 3)) * effective_progress(i) for i in items) / total_w


def reconcile_readiness(status: str, pct: int, status_set: bool, pct_set: bool):
    """Keep status and completion % consistent in both directions.

    An explicitly sent status wins: complete → 100 %, not_started → 0 %,
    in_progress caps at 99 %. Otherwise a sent % drives the status:
    100 → complete, 1–99 → in_progress (unless blocked), 0 from complete → not_started.
    """
    if status_set:
        if status == "complete":
            pct = 100
        elif status == "not_started":
            pct = 0
        elif status == "in_progress" and pct >= 100:
            pct = 99
    elif pct_set:
        if pct >= 100:
            status = "complete"
        elif pct > 0 and status in ("not_started", "complete"):
            status = "in_progress"
        elif pct == 0 and status == "complete":
            status = "not_started"
    return status, pct


# ============================================
# FastAPI Application
# ============================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    auth.startup_warnings()
    yield


app = FastAPI(
    title="RootRise Funding Readiness API",
    description="API for tracking funding opportunities and readiness for DEVONEERS/RootRise",
    version=APP_VERSION,
    lifespan=lifespan,
)

@app.middleware("http")
async def require_credentials(request: Request, call_next):
    """AUTH_MODE gate (see auth.py). Registered before CORS so CORS stays outermost
    and 401s still carry CORS headers."""
    if auth.requires_auth(request.method, request.url.path) and not auth.is_authenticated(request.headers):
        return JSONResponse({"detail": "Authentication required", "auth_mode": auth.mode()},
                            status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key"],
    expose_headers=["X-Duplicate-Of"],
)

static_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ============================================
# Auth Endpoints
# ============================================

class LoginRequest(BaseModel):
    passcode: str = Field(..., min_length=1, max_length=200)


@app.get("/api/auth/status")
async def auth_status(request: Request):
    return {"mode": auth.mode(), "authenticated": auth.is_authenticated(request.headers),
            "passcode_configured": bool(os.environ.get("TEAM_PASSCODE"))}


@app.post("/api/auth/login")
async def auth_login(body: LoginRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not auth.login_allowed(ip):
        raise HTTPException(status_code=429, detail="Too many attempts — try again in 10 minutes")
    if not auth.check_passcode(body.passcode):
        auth.record_failure(ip)
        raise HTTPException(status_code=401, detail="Wrong passcode")
    return {**auth.issue_token(), "mode": auth.mode()}

# ============================================
# Opportunity Endpoints
# ============================================

ExpiredMode = Literal["include", "exclude", "only"]


def _query_opportunities(conn, *, status=None, type=None, category=None, min_fit_score=None,
                         is_saved=None, needs_check=None, source=None, q=None,
                         sort_by="deadline", sort_order="asc", expired: str = "include") -> List[dict]:
    query = "SELECT * FROM opportunities WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if type:
        query += " AND type = ?"
        params.append(type)
    if category:
        # Rows created before the backfill have NULL category; fall back to the type grouping.
        types = BD_TYPES if category == "bd" else STARTUP_TYPES + ["other"]
        query += f" AND (category = ? OR (category IS NULL AND type IN ({','.join('?' * len(types))})))"
        params.extend([category, *types])
    if min_fit_score is not None:
        query += " AND fit_score >= ?"
        params.append(min_fit_score)
    if is_saved is not None:
        query += " AND is_saved = ?"
        params.append(1 if is_saved else 0)
    if needs_check is not None:
        query += " AND needs_check = ?"
        params.append(1 if needs_check else 0)
    if source:
        query += " AND source = ?"
        params.append(source)
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query += (" AND (name LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\'"
                  " OR requirements LIKE ? ESCAPE '\\' OR funding_amount LIKE ? ESCAPE '\\')")
        params.extend([like] * 4)
    query += " ORDER BY " + OPPORTUNITY_SORTS[sort_by][sort_order]
    now = utcnow()
    rows = [opportunity_out(r, now) for r in conn.execute(query, params).fetchall()]
    if sort_by == "deadline":
        # Stored deadlines mix legacy naive strings and +00:00 strings; sort on parsed values.
        far = datetime.max.replace(tzinfo=timezone.utc)
        rows.sort(key=lambda o: (o["deadline"] is None, o["deadline"] or far, o["id"]),
                  reverse=False)
        if sort_order == "desc":
            dated = [o for o in rows if o["deadline"]][::-1]
            rows = dated + [o for o in rows if not o["deadline"]]
    if expired == "exclude":
        rows = [o for o in rows if not o["is_expired"]]
    elif expired == "only":
        rows = [o for o in rows if o["is_expired"]]
    return rows


@app.get("/api/opportunities", response_model=List[Opportunity])
async def get_opportunities(
    status: Optional[OpportunityStatus] = None,
    type: Optional[OpportunityType] = None,
    min_fit_score: Optional[int] = Query(None, ge=0, le=100),
    category: Optional[Category] = None,
    pipeline: Optional[str] = Query(None, pattern="^(startup|bd)$", description="Alias of category (v1.x)"),
    is_saved: Optional[bool] = None,
    is_starred: Optional[bool] = None,
    needs_check: Optional[bool] = None,
    source: Optional[str] = None,
    q: Optional[str] = Query(None, max_length=200, description="Search name, notes, requirements, funding"),
    expired: ExpiredMode = Query("include", description="include | exclude | only (default include for v1.x clients)"),
    sort_by: str = Query("deadline", pattern="^(deadline|fit_score|priority|created_at|updated_at|name)$"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$")
):
    """List opportunities. Every filter is optional; with none, behaves like v1.x (all rows)."""
    starred = is_starred if is_starred is not None else is_saved
    with db_session() as conn:
        return _query_opportunities(
            conn, status=status.value if status else None, type=type.value if type else None,
            category=category or pipeline, min_fit_score=min_fit_score, is_saved=starred,
            needs_check=needs_check, source=source, q=(q or "").strip() or None,
            sort_by=sort_by, sort_order=sort_order, expired=expired)


@app.get("/api/opportunities/counts-by-type")
async def get_opportunity_counts_by_type(
    pipeline: Optional[str] = Query(None, pattern="^(startup|bd)$"),
    expired: ExpiredMode = Query("include"),
):
    """Live counts for filter tabs. `counts` is per type within the pipeline; the
    other keys let the UI badge every tab (pipelines, starred, needs-check, expired)."""
    with db_session() as conn:
        everything = _query_opportunities(conn)
    pool = [o for o in everything if not pipeline or o["category"] == pipeline]
    visible = [o for o in pool if expired == "include"
               or (expired == "exclude" and not o["is_expired"])
               or (expired == "only" and o["is_expired"])]
    counts = {}
    for o in visible:
        counts[o["type"]] = counts.get(o["type"], 0) + 1
    live = [o for o in everything if not o["is_expired"]]
    return {
        "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "total": len(visible),
        "saved_count": sum(o["is_saved"] for o in visible),
        "starred_count": sum(o["is_saved"] for o in visible),
        "auth_count": sum(o["needs_check"] for o in visible),
        "needs_check_count": sum(o["needs_check"] for o in visible),
        "expired_count": sum(o["is_expired"] for o in pool),
        "by_category": {
            "all": len(live),
            "startup": sum(o["category"] == "startup" for o in live),
            "bd": sum(o["category"] == "bd" for o in live),
        },
        "pipeline": pipeline or "all",
        "expired": expired,
    }


@app.put("/api/opportunities/{opp_id}/toggle-save")
async def toggle_save_opportunity(opp_id: int):
    """Toggle the saved/starred status of an opportunity."""
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_saved, name FROM opportunities WHERE id = ?", (opp_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        new_state = 0 if row[0] else 1
        cursor.execute("UPDATE opportunities SET is_saved = ?, updated_at = ? WHERE id = ?", (new_state, now_iso(), opp_id))
        log_activity(cursor, "saved" if new_state else "unsaved", "opportunity", opp_id, f"{'Saved' if new_state else 'Unsaved'}: {row[1]}")
        return {"id": opp_id, "is_saved": bool(new_state), "is_starred": bool(new_state),
                "message": f"Opportunity {'saved' if new_state else 'unsaved'}"}


@app.get("/api/opportunities/{opp_id}", response_model=Opportunity)
async def get_opportunity(opp_id: int):
    with db_session() as conn:
        row = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        return opportunity_out(row)


def _notes_with_deadline_text(notes: Optional[str], deadline_text: Optional[str]) -> Optional[str]:
    if not deadline_text:
        return notes
    suffix = f"(Deadline as published: {deadline_text})"
    return f"{notes} {suffix}" if notes else suffix


DEDUP_FILLABLE = ("deadline", "url", "funding_amount", "requirements", "contact_info")


def find_duplicate(conn, candidate: dict):
    """Return (row, reason) for an existing opportunity that is the same programme.
    URL matches are checked across all rows first, then canonical/fuzzy name."""
    rows = conn.execute("SELECT * FROM opportunities").fetchall()
    best = None
    for r in rows:
        reason = is_duplicate(candidate, dict(r))
        if reason == "url":
            return r, reason
        if reason and best is None:
            best = (r, reason)
    return best or (None, None)


@app.post("/api/opportunities", response_model=Opportunity)
async def create_opportunity(opp: OpportunityCreate, response: Response,
                             allow_duplicate: bool = Query(False, description="Skip server-side dedup")):
    """Create an opportunity. If it duplicates an existing one (same normalized URL,
    same canonical name, or ≥0.92 title similarity with the same type) the existing
    record is returned with 200 and header `X-Duplicate-Of: <id>`; its empty fields
    are filled from the payload. Any 2xx is success for the Hunter, so it is unaffected."""
    now = now_iso()
    tags = parse_notes_tags(opp.notes)
    category = opp.category or tags["category"] or category_for_type(opp.type.value)
    needs_check = opp.needs_check if opp.needs_check is not None else tags["needs_check"]
    access_status = opp.access_status or tags["access_status"]
    source = opp.source or tags["source"] or "manual"
    notes = _notes_with_deadline_text(opp.notes, opp.deadline_text)
    with db_session() as conn:
        cursor = conn.cursor()
        if not allow_duplicate:
            dup, reason = find_duplicate(conn, {"name": opp.name, "type": opp.type.value, "url": opp.url})
            if dup is not None:
                fills = {}
                for f in DEDUP_FILLABLE:
                    new_val = iso_utc(opp.deadline) if f == "deadline" else getattr(opp, f)
                    if new_val and not dup[f]:
                        fills[f] = new_val
                if "url" in fills:
                    fills["url_normalized"] = normalize_url(fills["url"]) or None
                if fills:
                    cursor.execute(
                        f"UPDATE opportunities SET {', '.join(k + ' = ?' for k in fills)}, updated_at = ? WHERE id = ?",
                        [*fills.values(), now, dup["id"]])
                log_activity(cursor, "deduplicated", "opportunity", dup["id"],
                             f"Duplicate of '{dup['name']}' ({reason}) skipped: {opp.name}"
                             + (f"; filled {', '.join(k for k in fills if k != 'url_normalized')}" if fills else ""))
                log.info("dedup: %r matches #%d (%s)", opp.name, dup["id"], reason)
                response.headers["X-Duplicate-Of"] = str(dup["id"])
                return opportunity_out(cursor.execute("SELECT * FROM opportunities WHERE id = ?", (dup["id"],)).fetchone())
        cursor.execute("""
            INSERT INTO opportunities
            (name, type, deadline, status, fit_score, url, notes, funding_amount, requirements, contact_info,
             priority, is_saved, category, source, needs_check, access_status, url_normalized, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            opp.name, opp.type.value, iso_utc(opp.deadline),
            opp.status.value, opp.fit_score, opp.url, notes,
            opp.funding_amount, opp.requirements, opp.contact_info, opp.priority,
            1 if opp.is_saved else 0, category, source, 1 if needs_check else 0, access_status,
            normalize_url(opp.url) or None, now, now
        ))
        opp_id = cursor.lastrowid
        log_activity(cursor, "created", "opportunity", opp_id, f"Created opportunity: {opp.name}")
        return opportunity_out(cursor.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,)).fetchone())


OPP_REQUIRED = {"name", "type", "status", "fit_score", "priority", "category"}
OPP_BOOL = {"needs_check", "is_saved"}


@app.put("/api/opportunities/{opp_id}", response_model=Opportunity)
async def update_opportunity(opp_id: int, opp: OpportunityUpdate):
    """Partial update. Fields absent from the body are untouched; an explicit
    `null` clears a nullable field (deadline, url, notes, …)."""
    with db_session() as conn:
        cursor = conn.cursor()
        existing = cursor.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        update_data = opp.model_dump(exclude_unset=True)
        deadline_text = update_data.pop("deadline_text", None)
        cleared_required = sorted(k for k, v in update_data.items() if v is None and (k in OPP_REQUIRED or k in OPP_BOOL))
        if cleared_required:
            raise HTTPException(status_code=422, detail=f"Cannot clear required field(s): {', '.join(cleared_required)}")
        if deadline_text:
            update_data["notes"] = _notes_with_deadline_text(update_data.get("notes", existing["notes"]), deadline_text)
        update_fields, params = [], []
        for field, value in update_data.items():
            if field == "deadline":
                value = iso_utc(value)
            elif hasattr(value, "value"):
                value = value.value
            elif field in OPP_BOOL:
                value = 1 if value else 0
            update_fields.append(f"{field} = ?")
            params.append(value)
            if field == "url":
                update_fields.append("url_normalized = ?")
                params.append(normalize_url(value) or None)
        if update_fields:
            update_fields.append("updated_at = ?")
            params.append(now_iso())
            params.append(opp_id)
            cursor.execute(f"UPDATE opportunities SET {', '.join(update_fields)} WHERE id = ?", params)
            log_activity(cursor, "updated", "opportunity", opp_id, f"Updated fields: {', '.join(update_data.keys())}")
        return opportunity_out(cursor.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,)).fetchone())


@app.delete("/api/opportunities/{opp_id}")
async def delete_opportunity(opp_id: int):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM opportunities WHERE id = ?", (opp_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        cursor.execute("DELETE FROM opportunities WHERE id = ?", (opp_id,))
        log_activity(cursor, "deleted", "opportunity", opp_id, f"Deleted: {row['name']}")
        return {"message": "Opportunity deleted", "id": opp_id}

# ============================================
# Readiness Item Endpoints
# ============================================

@app.get("/api/readiness-items", response_model=List[ReadinessItem])
async def get_readiness_items(
    status: Optional[ReadinessStatus] = None,
    category: Optional[ReadinessCategory] = None,
    owner: Optional[str] = None,
    sort_by: str = Query("priority", pattern="^(due_date|priority|status|created_at|category|name)$"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$")
):
    with db_session() as conn:
        query = "SELECT * FROM readiness_items WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status.value)
        if category:
            query += " AND category = ?"
            params.append(category.value)
        if owner:
            query += " AND owner = ?"
            params.append(owner)
        query += " ORDER BY " + READINESS_SORTS[sort_by][sort_order]
        now = utcnow()
        return [readiness_out(row, now) for row in conn.execute(query, params).fetchall()]


@app.get("/api/readiness-items/{item_id}", response_model=ReadinessItem)
async def get_readiness_item(item_id: int):
    with db_session() as conn:
        row = conn.execute("SELECT * FROM readiness_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Readiness item not found")
        return readiness_out(row)


@app.post("/api/readiness-items", response_model=ReadinessItem)
async def create_readiness_item(item: ReadinessItemCreate):
    now = now_iso()
    sent = item.model_fields_set
    status, pct = reconcile_readiness(item.status.value, item.completion_percentage,
                                      "status" in sent, "completion_percentage" in sent)
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO readiness_items
            (name, category, status, owner, due_date, description, priority, dependencies, completion_percentage, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item.name, item.category.value, status, item.owner, iso_utc(item.due_date),
            item.description, item.priority, item.dependencies, pct, now, now
        ))
        item_id = cursor.lastrowid
        log_activity(cursor, "created", "readiness_item", item_id, f"Created item: {item.name}")
        return readiness_out(cursor.execute("SELECT * FROM readiness_items WHERE id = ?", (item_id,)).fetchone())


READINESS_REQUIRED = {"name", "category", "status", "priority", "completion_percentage"}


@app.put("/api/readiness-items/{item_id}", response_model=ReadinessItem)
async def update_readiness_item(item_id: int, item: ReadinessItemUpdate):
    with db_session() as conn:
        cursor = conn.cursor()
        existing = cursor.execute("SELECT * FROM readiness_items WHERE id = ?", (item_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Readiness item not found")
        update_data = item.model_dump(exclude_unset=True)
        cleared_required = sorted(k for k, v in update_data.items() if v is None and k in READINESS_REQUIRED)
        if cleared_required:
            raise HTTPException(status_code=422, detail=f"Cannot clear required field(s): {', '.join(cleared_required)}")
        for k, v in list(update_data.items()):
            if hasattr(v, "value"):
                update_data[k] = v.value
        if "status" in update_data or "completion_percentage" in update_data:
            status, pct = reconcile_readiness(
                update_data.get("status", existing["status"]),
                update_data.get("completion_percentage", existing["completion_percentage"] or 0),
                "status" in update_data, "completion_percentage" in update_data)
            if status != existing["status"] or "status" in update_data:
                update_data["status"] = status
            if pct != existing["completion_percentage"] or "completion_percentage" in update_data:
                update_data["completion_percentage"] = pct
        if "due_date" in update_data:
            update_data["due_date"] = iso_utc(update_data["due_date"])
        if update_data:
            fields = [f"{k} = ?" for k in update_data] + ["updated_at = ?"]
            params = list(update_data.values()) + [now_iso(), item_id]
            cursor.execute(f"UPDATE readiness_items SET {', '.join(fields)} WHERE id = ?", params)
            log_activity(cursor, "updated", "readiness_item", item_id, f"Updated fields: {', '.join(update_data.keys())}")
        return readiness_out(cursor.execute("SELECT * FROM readiness_items WHERE id = ?", (item_id,)).fetchone())


@app.delete("/api/readiness-items/{item_id}")
async def delete_readiness_item(item_id: int):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM readiness_items WHERE id = ?", (item_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Readiness item not found")
        cursor.execute("DELETE FROM readiness_items WHERE id = ?", (item_id,))
        log_activity(cursor, "deleted", "readiness_item", item_id, f"Deleted: {row['name']}")
        return {"message": "Readiness item deleted", "id": item_id}

# ============================================
# Dashboard & Stats
# ============================================

@app.get("/api/dashboard-stats", response_model=DashboardStats)
async def get_dashboard_stats():
    now = utcnow()
    with db_session() as conn:
        opps = _query_opportunities(conn)
        items = [readiness_out(r, now) for r in conn.execute("SELECT * FROM readiness_items").fetchall()]
        recent_activity = [clean_row(r) for r in conn.execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT 10").fetchall()]

    live = [o for o in opps if not o["is_expired"]]
    active = [o for o in live if o["status"] not in ("rejected", "closed")]
    horizon = now + timedelta(days=30)
    upcoming = sorted(
        (o for o in active if o["deadline"] and now <= o["deadline"] <= horizon
         and o["status"] not in ("submitted", "accepted")),
        key=lambda o: o["deadline"])

    def count_by(rows, key):
        out = {}
        for r in rows:
            out[r[key]] = out.get(r[key], 0) + 1
        return out

    by_cat = {}
    for i in items:
        c = by_cat.setdefault(i["category"], {"progress": [], "total": 0, "completed": 0})
        c["progress"].append(effective_progress(i))
        c["total"] += 1
        c["completed"] += i["status"] == "complete"
    readiness_by_cat = {k: {"avg_completion": round(sum(v["progress"]) / len(v["progress"]), 1),
                            "total": v["total"], "completed": v["completed"]} for k, v in by_cat.items()}

    week = now + timedelta(days=7)
    urgent = sorted((i for i in items if i["due_date"] and i["due_date"] <= week and i["status"] != "complete"),
                    key=lambda i: i["due_date"])[:10]

    return DashboardStats(
        total_opportunities=len(opps),
        active_opportunities=len(active),
        upcoming_deadlines=len(upcoming),
        expired_opportunities=sum(o["is_expired"] for o in opps),
        needs_check_count=sum(o["needs_check"] for o in live),
        starred_count=sum(o["is_saved"] for o in opps),
        total_readiness_items=len(items),
        completed_items=sum(i["status"] == "complete" for i in items),
        in_progress_items=sum(i["status"] == "in_progress" for i in items),
        overall_readiness_score=round(readiness_score(items), 1),
        readiness_formula=READINESS_FORMULA,
        opportunities_by_status=count_by(opps, "status"),
        opportunities_by_type=count_by(opps, "type"),
        opportunities_by_category={"startup": sum(o["category"] == "startup" for o in active),
                                   "bd": sum(o["category"] == "bd" for o in active)},
        readiness_by_category=readiness_by_cat,
        upcoming_opportunities=upcoming[:5],
        urgent_items=urgent,
        recent_activity=recent_activity,
    )


@app.get("/api/activity-log", response_model=List[ActivityLog])
async def get_activity_log(limit: int = Query(20, ge=1, le=100)):
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [clean_row(row) for row in rows]


@app.get("/api/health")
async def health_check():
    with db_session() as conn:
        schema_version = migrations.get_version(conn)
    return {"status": "healthy", "version": APP_VERSION, "schema_version": schema_version,
            "timestamp": now_iso()}


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Served from the root so its scope covers the whole app (PWA installability)."""
    path = os.path.join(os.path.dirname(__file__), "..", "frontend", "sw.js")
    return FileResponse(path, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
    if os.path.exists(frontend_path):
        with open(frontend_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Frontend not found. Please build the frontend.</h1>")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
