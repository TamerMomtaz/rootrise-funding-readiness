"""Hunter v3.3 contract: every payload shape hunter.py POSTs today must be accepted."""
import pytest


def hunter_v33_payload(name, type_, deadline, fit, notes, category="startup", source="WebSearch",
                       access_status="ok", funding=None, requirements=None, priority=2, url="https://example.org/p"):
    """Byte-for-byte the dict built in hunter.py DashboardClient.add_opportunity (v3.3)."""
    cat_tag = "🚀" if category == "startup" else "📋"
    auth_tag = ""
    if access_status == "auth_required":
        auth_tag = " 🔒 NEEDS CHECK |"
    elif access_status in ("blocked", "timeout"):
        auth_tag = f" ⚠️ {access_status.upper()} |"
    return {
        "name": name, "type": type_,
        "deadline": f"{deadline}T00:00:00" if deadline else None,
        "status": "researching", "fit_score": fit, "url": url,
        "notes": f"[{cat_tag} {category.upper()} |{auth_tag} Auto: {source}] {notes}",
        "funding_amount": funding, "requirements": requirements, "priority": priority,
    }


@pytest.mark.parametrize("deadline", ["2026-12-01", None, "Rolling", "2026-11", "TBD", "Q1 2027", "31/12/2026"])
def test_free_text_deadlines_never_422(client, deadline):
    p = hunter_v33_payload("Deadline Probe", "grant", deadline, 70, "fit")
    r = client.post("/api/opportunities?allow_duplicate=true", json=p)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("type_,expected", [("accelerator", "accelerator"), ("fellowship", "grant"),
                                            ("TOR", "consulting"), ("hackathon", "other"), ("VC Fund", "vc_fund")])
def test_unknown_types_coerced(client, type_, expected):
    r = client.post("/api/opportunities?allow_duplicate=true",
                    json=hunter_v33_payload(f"T {type_}", type_, None, 60, "x"))
    assert r.status_code == 200 and r.json()["type"] == expected


def test_out_of_range_numbers_clamped(client):
    p = hunter_v33_payload("N" * 300, "grant", None, 140, "x", priority=0)
    r = client.post("/api/opportunities?allow_duplicate=true", json=p).json()
    assert r["fit_score"] == 100 and r["priority"] == 1 and len(r["name"]) == 200


def test_unknown_status_coerced(client):
    p = hunter_v33_payload("Status Probe", "grant", None, 60, "x")
    p["status"] = "shortlisted"
    assert client.post("/api/opportunities", json=p).json()["status"] == "researching"


def test_tags_become_structured_fields(client):
    r = client.post("/api/opportunities", json=hunter_v33_payload(
        "Gov Digital Services RFP", "government", "2026-12-01", 75, "BD fit", category="bd",
        source="ICTworks", access_status="auth_required")).json()
    assert r["category"] == "bd"            # not 'startup' just because type=government
    assert r["source"] == "ICTworks"
    assert r["needs_check"] is True and r["access_status"] == "auth_required"
    blocked = client.post("/api/opportunities", json=hunter_v33_payload(
        "Blocked Tender X", "tender", None, 60, "x", category="bd", access_status="blocked",
        url="https://other.example/t")).json()
    assert blocked["access_status"] == "blocked" and blocked["needs_check"] is False


def test_v4_hunter_fields_win_over_tags(client):
    p = hunter_v33_payload("Explicit Fields Probe", "grant", None, 60, "x", category="startup")
    p.update({"category": "bd", "source": "Known:UNDP", "needs_check": True, "access_status": "auth_required"})
    r = client.post("/api/opportunities", json=p).json()
    assert (r["category"], r["source"], r["needs_check"]) == ("bd", "Known:UNDP", True)


def test_hunter_dedup_get_still_returns_everything(client):
    """Hunter fetches GET /api/opportunities with no params for its own dedup — must include expired rows."""
    client.post("/api/opportunities", json=hunter_v33_payload("Old Thing", "grant", "2020-01-01", 60, "x"))
    assert len(client.get("/api/opportunities").json()) == 1


def test_duplicate_post_is_2xx_for_hunter(client):
    p = hunter_v33_payload("Google for Startups Accelerator MENA 2026", "accelerator", None, 90, "x",
                           url="https://startup.google.com/mena")
    first = client.post("/api/opportunities", json=p)
    p2 = dict(p, name="Google for Startups Accelerator: MENA (Cohort 10)", url="https://news.example/google")
    second = client.post("/api/opportunities", json=p2)
    assert first.status_code == 200 and second.status_code == 200      # raise_for_status() passes
    assert second.headers["X-Duplicate-Of"] == str(first.json()["id"])
    assert len(client.get("/api/opportunities").json()) == 1
