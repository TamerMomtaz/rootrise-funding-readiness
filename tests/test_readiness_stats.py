from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_opp


def item(client, **kw):
    body = {"name": "Item", "category": "pitch"}
    body.update(kw)
    r = client.post("/api/readiness-items", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_readiness_crud_and_clearing(client):
    i = item(client, owner="Tee", due_date="2026-12-01", description="d")
    r = client.put(f"/api/readiness-items/{i['id']}", json={"owner": None, "due_date": None, "description": None}).json()
    assert r["owner"] is None and r["due_date"] is None and r["description"] is None
    assert client.put(f"/api/readiness-items/{i['id']}", json={"category": None}).status_code == 422
    assert client.delete(f"/api/readiness-items/{i['id']}").status_code == 200


@pytest.mark.parametrize("sent,expected", [
    ({"status": "complete", "completion_percentage": 0}, ("complete", 100)),
    ({"completion_percentage": 100}, ("complete", 100)),
    ({"completion_percentage": 40}, ("in_progress", 40)),
    ({"status": "not_started"}, ("not_started", 0)),
    ({"status": "in_progress", "completion_percentage": 100}, ("in_progress", 99)),
    ({"status": "blocked", "completion_percentage": 30}, ("blocked", 30)),
])
def test_status_and_percentage_stay_consistent(client, sent, expected):
    i = item(client, **sent)
    assert (i["status"], i["completion_percentage"]) == expected


def test_update_reconciles_both_ways(client):
    i = item(client, completion_percentage=50)
    assert i["status"] == "in_progress"
    i = client.put(f"/api/readiness-items/{i['id']}", json={"status": "complete"}).json()
    assert i["completion_percentage"] == 100
    i = client.put(f"/api/readiness-items/{i['id']}", json={"completion_percentage": 0}).json()
    assert i["status"] == "not_started"


def test_category_aliases(client):
    assert item(client, category="pitch_materials")["category"] == "pitch"
    assert item(client, category="technical")["category"] == "technical"


def test_readiness_score_formula(client):
    # P1 (w=5) at 100%, P5 (w=1) at 0%  → (5*100 + 1*0) / 6 = 83.3
    item(client, name="a", priority=1, status="complete")
    item(client, name="b", priority=5, completion_percentage=0)
    s = client.get("/api/dashboard-stats").json()
    assert s["overall_readiness_score"] == 83.3
    assert "6 − priority" in s["readiness_formula"]
    assert s["readiness_by_category"]["pitch"] == {"avg_completion": 50.0, "total": 2, "completed": 1}


def test_complete_with_zero_percent_counts_as_done(client, monkeypatch):
    """Legacy rows can be complete/0%; the score must treat them as 100%."""
    import main
    with main.db_session() as conn:
        conn.execute("""INSERT INTO readiness_items (name, category, status, completion_percentage, priority,
                        created_at, updated_at) VALUES ('legacy','legal','complete',0,3,'2026-01-01','2026-01-01')""")
    assert client.get("/api/dashboard-stats").json()["overall_readiness_score"] == 100.0


def test_dashboard_stats_exclude_expired(client):
    days = lambda n: (datetime.now(timezone.utc) + timedelta(days=n)).strftime("%Y-%m-%d")
    make_opp(client, name="Expired", deadline=days(-10))
    make_opp(client, name="Upcoming", deadline=days(10), category="bd", type="tender")
    make_opp(client, name="Far", deadline=days(90))
    make_opp(client, name="Rejected", status="rejected")
    make_opp(client, name="Check", needs_check=True)
    s = client.get("/api/dashboard-stats").json()
    assert s["total_opportunities"] == 5
    assert s["expired_opportunities"] == 1
    assert s["active_opportunities"] == 3              # Upcoming, Far, Check
    assert s["upcoming_deadlines"] == 1
    assert [o["name"] for o in s["upcoming_opportunities"]] == ["Upcoming"]
    assert s["opportunities_by_category"] == {"startup": 2, "bd": 1}
    assert s["needs_check_count"] == 1


def test_overdue_readiness_items_are_urgent(client):
    item(client, name="late", due_date="2020-01-01")
    s = client.get("/api/dashboard-stats").json()
    assert [u["name"] for u in s["urgent_items"]] == ["late"]
    assert client.get("/api/readiness-items").json()[0]["is_overdue"] is True
