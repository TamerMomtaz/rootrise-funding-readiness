"""
RootRise Funding Readiness System - Backend API
FastAPI application with SQLite database for tracking funding opportunities and readiness items

v1.1 — Feb 13, 2026:
  + Saved/Starred Opportunities (toggle-save, saved filter, sidebar count)
  + Counts by Type endpoint (for filter tabs with dynamic counts)
  + is_saved column migration (idempotent)
"""

from fastapi import FastAPI, HTTPException, Depends, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime, timedelta, timezone
from enum import Enum
import sqlite3
import json
import os
from contextlib import contextmanager, asynccontextmanager

# ============================================
# Configuration
# ============================================

DATABASE_PATH = os.environ.get("DATABASE_PATH", "funding_readiness.db")
API_KEY = os.environ.get("API_KEY", "rootrise-dev-key-2024")
APP_VERSION = "4.0.0"


def utcnow() -> datetime:
    """Timezone-aware current UTC time (replaces deprecated datetime.utcnow())."""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().replace(microsecond=0).isoformat()

# ============================================
# Enums
# ============================================

class OpportunityStatus(str, Enum):
    RESEARCHING = "researching"
    PREPARING = "preparing"
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
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
    TEAM = "team"
    PRODUCT = "product"
    MARKET = "market"
    OPERATIONS = "operations"
    DOCUMENTATION = "documentation"

# Pipeline type groupings
STARTUP_TYPES = ['accelerator', 'grant', 'vc_fund', 'angel', 'corporate',
                 'government', 'competition', 'cloud_credits']
BD_TYPES = ['rfp', 'tender', 'consulting', 'procurement']

# ============================================
# Pydantic Models
# ============================================

class OpportunityBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    type: OpportunityType
    deadline: Optional[datetime] = None
    status: OpportunityStatus = OpportunityStatus.RESEARCHING
    fit_score: int = Field(default=50, ge=0, le=100)
    url: Optional[str] = None
    notes: Optional[str] = None
    funding_amount: Optional[str] = None
    requirements: Optional[str] = None
    contact_info: Optional[str] = None
    priority: int = Field(default=3, ge=1, le=5)

class OpportunityCreate(OpportunityBase):
    pass

class OpportunityUpdate(BaseModel):
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

class Opportunity(OpportunityBase):
    id: int
    is_saved: Optional[bool] = False
    created_at: datetime
    updated_at: datetime

class ReadinessItemBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    category: ReadinessCategory
    status: ReadinessStatus = ReadinessStatus.NOT_STARTED
    owner: Optional[str] = None
    due_date: Optional[datetime] = None
    description: Optional[str] = None
    priority: int = Field(default=3, ge=1, le=5)
    dependencies: Optional[str] = None
    completion_percentage: int = Field(default=0, ge=0, le=100)

class ReadinessItemCreate(ReadinessItemBase):
    pass

class ReadinessItemUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    category: Optional[ReadinessCategory] = None
    status: Optional[ReadinessStatus] = None
    owner: Optional[str] = None
    due_date: Optional[datetime] = None
    description: Optional[str] = None
    priority: Optional[int] = Field(None, ge=1, le=5)
    dependencies: Optional[str] = None
    completion_percentage: Optional[int] = Field(None, ge=0, le=100)

class ReadinessItem(ReadinessItemBase):
    id: int
    created_at: datetime
    updated_at: datetime

class ActivityLog(BaseModel):
    id: int
    action: str
    entity_type: str
    entity_id: int
    details: Optional[str]
    timestamp: datetime

class DashboardStats(BaseModel):
    total_opportunities: int
    active_opportunities: int
    upcoming_deadlines: int
    total_readiness_items: int
    completed_items: int
    in_progress_items: int
    overall_readiness_score: float
    opportunities_by_status: dict
    opportunities_by_type: dict
    readiness_by_category: dict
    urgent_items: List[dict]
    recent_activity: List[dict]

# ============================================
# Database Setup
# ============================================

def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
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
    # Normalize is_saved to boolean
    if 'is_saved' in d:
        d['is_saved'] = bool(d['is_saved'])
    return d

def run_migrations(conn):
    """Run database migrations safely (idempotent). Called on every startup."""
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(opportunities)")
    columns = [col[1] for col in cursor.fetchall()]
    if "is_saved" not in columns:
        cursor.execute("ALTER TABLE opportunities ADD COLUMN is_saved BOOLEAN DEFAULT 0")
        conn.commit()
        print("✅ Migration: Added 'is_saved' column to opportunities table", flush=True)

def init_db():
    """Initialize database with schema"""
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
        # Run migrations for existing databases
        run_migrations(conn)
        # Seed if empty
        cursor.execute("SELECT COUNT(*) FROM opportunities")
        if cursor.fetchone()[0] == 0:
            seed_initial_data(cursor)

def seed_initial_data(cursor):
    """Seed database with initial RootRise funding opportunities and readiness items"""
    now = now_iso()
    opportunities = [
        ("Google for Startups MENA", "accelerator", "2026-01-30T00:00:00", "preparing", 80,
         "https://startup.google.com/programs/accelerator/middle-east-north-africa-turkey/",
         "Up to $350K GCP credits. URGENT: 7 days until deadline!",
         "Up to $350K GCP credits", "Seed to Series A, product-market fit, AI/ML focus", None, 1),
        ("Sanabil 500 MENA (Batch 11)", "accelerator", "2026-03-08T00:00:00", "preparing", 60,
         "https://500.co/mena", "Seed funding program. $35K program fee required.",
         "$35K program fee", "Early traction, MENA focus", None, 2),
        ("EBRD Star Venture (Cohort 5)", "accelerator", "2026-04-15T00:00:00", "researching", 100,
         "https://www.ebrd.com/starventure", "18-month program, no equity. Primary target for RootRise.",
         "No equity, mentorship + network", "Egypt registration, <5 years old, tech focus, B2B", None, 1),
        ("Flat6Labs Cairo", "accelerator", None, "researching", 60,
         "https://flat6labs.com/cairo", "$10-20K for 10-20% equity. Rolling applications.",
         "$10-20K for 10-20% equity", "Egyptian founder, MVP required", None, 3),
        ("AUC Venture Lab", "accelerator", None, "researching", 60,
         "https://business.aucegypt.edu/research/vlab", "Egypt's first university accelerator. Cohort-based.",
         "Mentorship + workspace", "Innovation focus, Egyptian connection", None, 3),
        ("EBRD Innovation Programme", "grant", None, "researching", 80,
         "https://www.ebrd.com/what-we-do/sectors/advice-small-businesses/star-venture.html",
         "Up to €30K grants. Component-based applications.",
         "Up to €30K", "EBRD country operations, SME focus", None, 2),
        ("Berytech ScaleSmart", "accelerator", None, "researching", 60,
         "https://berytech.org/", "Investor readiness program. Pre-applications open.",
         "Mentorship + investor access", "MENA startup, growth stage", None, 3),
        ("Web Summit Qatar", "competition", "2026-01-27T00:00:00", "preparing", 50,
         "https://qatar.websummit.com/", "Networking opportunity. Jan 26-27, 2026.",
         "Exposure + networking", "Startup pitch competition entry", None, 2),
    ]
    for opp in opportunities:
        cursor.execute("""
            INSERT INTO opportunities
            (name, type, deadline, status, fit_score, url, notes, funding_amount, requirements, contact_info, priority, is_saved, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """, (*opp, now, now))

    readiness_items = [
        ("Brand Identity", "documentation", "complete", "Tee", None, "Color system v6.5, 4 themes", 1, None, 100),
        ("Architecture Docs", "documentation", "complete", "Tee", None, "Complete technical blueprint", 1, None, 100),
        ("Agent System", "product", "complete", "Tee", None, "11 agents with prompts", 1, None, 100),
        ("Sector Knowledge", "product", "complete", "Tee", None, "27+ sector packs", 1, None, 100),
        ("Team Bios", "team", "complete", "Tee", None, "All founders documented", 2, None, 100),
        ("Pitch Deck (10 slides)", "pitch", "in_progress", "Tee", "2026-02-15T00:00:00", "Simplified version needed. Target: 10 slides max.", 1, None, 60),
        ("Financial Projections", "financial", "in_progress", "Rouba", "2026-02-28T00:00:00", "3-year model required", 1, None, 40),
        ("One-Pager", "pitch", "not_started", None, "2026-02-15T00:00:00", "Single A4 summary for quick investor review", 1, "Pitch Deck", 0),
        ("Working Demo (MVD)", "product", "not_started", None, "2026-02-15T00:00:00", "Single flow Minimum Viable Demo", 1, None, 0),
        ("Demo Video (2 min)", "pitch", "not_started", None, "2026-02-28T00:00:00", "Screen recording walkthrough", 2, "Working Demo", 0),
        ("Customer Testimonials", "market", "not_started", None, "2026-03-31T00:00:00", "Target: 3 testimonials by March", 1, None, 0),
        ("Case Studies", "market", "not_started", None, "2026-03-31T00:00:00", "Target: 2 detailed case studies by March", 1, "Customer Testimonials", 0),
        ("Company Registration", "legal", "complete", "Tee", None, "Egyptian commercial registration complete", 2, None, 100),
        ("Market Size Analysis", "market", "complete", "Rouba", None, "TAM/SAM/SOM for Egyptian SME market", 2, None, 100),
        ("Competitive Analysis", "market", "in_progress", "Rouba", "2026-02-20T00:00:00", "Detailed competitor landscape", 2, None, 60),
        ("First 5 Pilots", "operations", "not_started", None, "2026-02-28T00:00:00", "Target: 5 pilot SMEs by end of February", 1, "Working Demo", 0),
        ("First Revenue", "financial", "not_started", None, "2026-03-31T00:00:00", "Target: $500 revenue by March", 1, "First 5 Pilots", 0),
    ]
    for item in readiness_items:
        cursor.execute("""
            INSERT INTO readiness_items
            (name, category, status, owner, due_date, description, priority, dependencies, completion_percentage, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (*item, now, now))

def log_activity(cursor, action: str, entity_type: str, entity_id: int, details: str = None):
    cursor.execute("""
        INSERT INTO activity_log (action, entity_type, entity_id, details, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (action, entity_type, entity_id, details, now_iso()))

# ============================================
# FastAPI Application
# ============================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="RootRise Funding Readiness API",
    description="API for tracking funding opportunities and readiness for DEVONEERS/RootRise",
    version=APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ============================================
# Opportunity Endpoints
# ============================================

@app.get("/api/opportunities", response_model=List[Opportunity])
async def get_opportunities(
    status: Optional[OpportunityStatus] = None,
    type: Optional[OpportunityType] = None,
    min_fit_score: Optional[int] = Query(None, ge=0, le=100),
    pipeline: Optional[str] = Query(None, pattern="^(startup|bd)$"),
    is_saved: Optional[bool] = None,
    sort_by: str = Query("deadline", pattern="^(deadline|fit_score|priority|created_at)$"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$")
):
    """Get all funding opportunities with optional filters including pipeline and saved"""
    with db_session() as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM opportunities WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status.value)
        if type:
            query += " AND type = ?"
            params.append(type.value)
        if min_fit_score:
            query += " AND fit_score >= ?"
            params.append(min_fit_score)
        if is_saved is not None:
            query += " AND is_saved = ?"
            params.append(1 if is_saved else 0)
        if pipeline == "startup":
            placeholders = ','.join('?' * len(STARTUP_TYPES))
            query += f" AND type IN ({placeholders})"
            params.extend(STARTUP_TYPES)
        elif pipeline == "bd":
            placeholders = ','.join('?' * len(BD_TYPES))
            query += f" AND type IN ({placeholders})"
            params.extend(BD_TYPES)
        query += f" ORDER BY {sort_by} {sort_order.upper()}"
        cursor.execute(query, params)
        return [clean_row(row) for row in cursor.fetchall()]

@app.get("/api/opportunities/counts-by-type")
async def get_opportunity_counts_by_type(pipeline: Optional[str] = Query(None, pattern="^(startup|bd)$")):
    """Return opportunity counts grouped by type for filter tabs with dynamic counts."""
    with db_session() as conn:
        cursor = conn.cursor()
        if pipeline == "startup":
            placeholders = ','.join('?' * len(STARTUP_TYPES))
            type_filter = f"WHERE type IN ({placeholders})"
            params = list(STARTUP_TYPES)
        elif pipeline == "bd":
            placeholders = ','.join('?' * len(BD_TYPES))
            type_filter = f"WHERE type IN ({placeholders})"
            params = list(BD_TYPES)
        else:
            type_filter = ""
            params = []

        cursor.execute(f"SELECT type, COUNT(*) as count FROM opportunities {type_filter} GROUP BY type ORDER BY count DESC", params)
        counts = {row[0]: row[1] for row in cursor.fetchall()}
        cursor.execute(f"SELECT COUNT(*) FROM opportunities {type_filter}", params)
        total = cursor.fetchone()[0]

        saved_q = f"SELECT COUNT(*) FROM opportunities {type_filter}"
        saved_q += (" AND is_saved = 1" if type_filter else " WHERE is_saved = 1")
        cursor.execute(saved_q, params)
        saved_count = cursor.fetchone()[0]

        auth_q = f"SELECT COUNT(*) FROM opportunities {type_filter}"
        auth_q += (" AND notes LIKE '%NEEDS CHECK%'" if type_filter else " WHERE notes LIKE '%NEEDS CHECK%'")
        cursor.execute(auth_q, params)
        auth_count = cursor.fetchone()[0]

        return {"counts": counts, "total": total, "saved_count": saved_count, "auth_count": auth_count, "pipeline": pipeline or "all"}

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
        now = now_iso()
        cursor.execute("UPDATE opportunities SET is_saved = ?, updated_at = ? WHERE id = ?", (new_state, now, opp_id))
        log_activity(cursor, "saved" if new_state else "unsaved", "opportunity", opp_id, f"{'Saved' if new_state else 'Unsaved'}: {row[1]}")
        return {"id": opp_id, "is_saved": bool(new_state), "message": f"Opportunity {'saved' if new_state else 'unsaved'}"}

@app.get("/api/opportunities/{opp_id}", response_model=Opportunity)
async def get_opportunity(opp_id: int):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        return clean_row(row)

@app.post("/api/opportunities", response_model=Opportunity)
async def create_opportunity(opp: OpportunityCreate):
    now = now_iso()
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO opportunities
            (name, type, deadline, status, fit_score, url, notes, funding_amount, requirements, contact_info, priority, is_saved, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """, (
            opp.name, opp.type.value,
            opp.deadline.isoformat() if opp.deadline else None,
            opp.status.value, opp.fit_score, opp.url, opp.notes,
            opp.funding_amount, opp.requirements, opp.contact_info, opp.priority,
            now, now
        ))
        opp_id = cursor.lastrowid
        log_activity(cursor, "created", "opportunity", opp_id, f"Created opportunity: {opp.name}")
        cursor.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
        return clean_row(cursor.fetchone())

@app.put("/api/opportunities/{opp_id}", response_model=Opportunity)
async def update_opportunity(opp_id: int, opp: OpportunityUpdate):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
        existing = cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        update_fields = []
        params = []
        update_data = opp.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            if value is not None:
                if field == "deadline" and value:
                    value = value.isoformat()
                elif hasattr(value, 'value'):
                    value = value.value
                update_fields.append(f"{field} = ?")
                params.append(value)
        if update_fields:
            update_fields.append("updated_at = ?")
            params.append(now_iso())
            params.append(opp_id)
            cursor.execute(f"UPDATE opportunities SET {', '.join(update_fields)} WHERE id = ?", params)
            log_activity(cursor, "updated", "opportunity", opp_id, f"Updated fields: {', '.join(update_data.keys())}")
        cursor.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
        return clean_row(cursor.fetchone())

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
    sort_by: str = Query("priority", pattern="^(due_date|priority|status|created_at|category)$"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$")
):
    with db_session() as conn:
        cursor = conn.cursor()
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
        query += f" ORDER BY {sort_by} {sort_order.upper()}"
        cursor.execute(query, params)
        return [clean_row(row) for row in cursor.fetchall()]

@app.get("/api/readiness-items/{item_id}", response_model=ReadinessItem)
async def get_readiness_item(item_id: int):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM readiness_items WHERE id = ?", (item_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Readiness item not found")
        return clean_row(row)

@app.post("/api/readiness-items", response_model=ReadinessItem)
async def create_readiness_item(item: ReadinessItemCreate):
    now = now_iso()
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO readiness_items
            (name, category, status, owner, due_date, description, priority, dependencies, completion_percentage, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item.name, item.category.value, item.status.value, item.owner,
            item.due_date.isoformat() if item.due_date else None,
            item.description, item.priority, item.dependencies, item.completion_percentage,
            now, now
        ))
        item_id = cursor.lastrowid
        log_activity(cursor, "created", "readiness_item", item_id, f"Created item: {item.name}")
        cursor.execute("SELECT * FROM readiness_items WHERE id = ?", (item_id,))
        return clean_row(cursor.fetchone())

@app.put("/api/readiness-items/{item_id}", response_model=ReadinessItem)
async def update_readiness_item(item_id: int, item: ReadinessItemUpdate):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM readiness_items WHERE id = ?", (item_id,))
        existing = cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Readiness item not found")
        update_fields = []
        params = []
        update_data = item.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            if value is not None:
                if field == "due_date" and value:
                    value = value.isoformat()
                elif hasattr(value, 'value'):
                    value = value.value
                update_fields.append(f"{field} = ?")
                params.append(value)
        if "completion_percentage" in update_data:
            pct = update_data["completion_percentage"]
            if pct == 100 and "status" not in update_data:
                update_fields.append("status = ?")
                params.append("complete")
            elif pct > 0 and pct < 100 and existing["status"] == "not_started":
                update_fields.append("status = ?")
                params.append("in_progress")
        if update_fields:
            update_fields.append("updated_at = ?")
            params.append(now_iso())
            params.append(item_id)
            cursor.execute(f"UPDATE readiness_items SET {', '.join(update_fields)} WHERE id = ?", params)
            log_activity(cursor, "updated", "readiness_item", item_id, f"Updated fields: {', '.join(update_data.keys())}")
        cursor.execute("SELECT * FROM readiness_items WHERE id = ?", (item_id,))
        return clean_row(cursor.fetchone())

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
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM opportunities")
        total_opps = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM opportunities WHERE status NOT IN ('rejected', 'closed')")
        active_opps = cursor.fetchone()[0]
        thirty_days = (utcnow() + timedelta(days=30)).isoformat()
        cursor.execute("""
            SELECT COUNT(*) FROM opportunities
            WHERE deadline IS NOT NULL AND deadline != '' AND deadline <= ? AND deadline >= ?
            AND status NOT IN ('submitted', 'accepted', 'rejected', 'closed')
        """, (thirty_days, now_iso()))
        upcoming_deadlines = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM readiness_items")
        total_items = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM readiness_items WHERE status = 'complete'")
        completed_items = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM readiness_items WHERE status = 'in_progress'")
        in_progress_items = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(completion_percentage * (6 - priority)) as wc, SUM(6 - priority) as tw FROM readiness_items")
        row = cursor.fetchone()
        overall_score = (row[0] / row[1]) if row[1] and row[0] else 0
        cursor.execute("SELECT status, COUNT(*) as count FROM opportunities GROUP BY status")
        opps_by_status = {r["status"]: r["count"] for r in cursor.fetchall()}
        cursor.execute("SELECT type, COUNT(*) as count FROM opportunities GROUP BY type")
        opps_by_type = {r["type"]: r["count"] for r in cursor.fetchall()}
        cursor.execute("SELECT category, AVG(completion_percentage) as avg_completion, COUNT(*) as total, SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) as completed FROM readiness_items GROUP BY category")
        readiness_by_cat = {r["category"]: {"avg_completion": round(r["avg_completion"], 1), "total": r["total"], "completed": r["completed"]} for r in cursor.fetchall()}
        seven_days = (utcnow() + timedelta(days=7)).isoformat()
        cursor.execute("SELECT * FROM readiness_items WHERE due_date IS NOT NULL AND due_date != '' AND due_date <= ? AND status != 'complete' ORDER BY due_date ASC LIMIT 10", (seven_days,))
        urgent_items = [clean_row(r) for r in cursor.fetchall()]
        cursor.execute("SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT 10")
        recent_activity = [clean_row(r) for r in cursor.fetchall()]
        return DashboardStats(
            total_opportunities=total_opps, active_opportunities=active_opps,
            upcoming_deadlines=upcoming_deadlines, total_readiness_items=total_items,
            completed_items=completed_items, in_progress_items=in_progress_items,
            overall_readiness_score=round(overall_score, 1),
            opportunities_by_status=opps_by_status, opportunities_by_type=opps_by_type,
            readiness_by_category=readiness_by_cat, urgent_items=urgent_items, recent_activity=recent_activity
        )

@app.get("/api/activity-log", response_model=List[ActivityLog])
async def get_activity_log(limit: int = Query(20, ge=1, le=100)):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT ?", (limit,))
        return [clean_row(row) for row in cursor.fetchall()]

@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "version": APP_VERSION, "timestamp": now_iso()}

@app.post("/api/reset-database")
async def reset_database():
    if os.path.exists(DATABASE_PATH):
        os.remove(DATABASE_PATH)
    init_db()
    return {"message": "Database reset to initial state"}

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
