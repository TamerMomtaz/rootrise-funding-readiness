from datetime import datetime, timedelta, timezone

from conftest import make_opp


def iso_days(n):
    return (datetime.now(timezone.utc) + timedelta(days=n)).strftime("%Y-%m-%d")


def test_crud_roundtrip(client):
    o = make_opp(client, name="EBRD Star Venture", type="accelerator", url="https://ebrd.com/sv",
                 funding_amount="Up to €30K", deadline=iso_days(40), fit_score=90, priority=1)
    assert o["category"] == "startup" and o["source"] == "manual" and o["needs_check"] is False
    assert o["funding_amount"] == "Up to €30K"
    assert client.get(f"/api/opportunities/{o['id']}").json()["name"] == "EBRD Star Venture"
    r = client.put(f"/api/opportunities/{o['id']}", json={"status": "preparing"})
    assert r.json()["status"] == "preparing" and r.json()["fit_score"] == 90
    assert client.delete(f"/api/opportunities/{o['id']}").status_code == 200
    assert client.get(f"/api/opportunities/{o['id']}").status_code == 404
    assert client.delete(f"/api/opportunities/{o['id']}").status_code == 404


def test_explicit_null_clears_fields(client):
    o = make_opp(client, deadline=iso_days(10), url="https://x.example/a", notes="n", contact_info="c")
    r = client.put(f"/api/opportunities/{o['id']}", json={"deadline": None, "url": None, "notes": None, "contact_info": None})
    body = r.json()
    assert body["deadline"] is None and body["url"] is None and body["notes"] is None and body["contact_info"] is None


def test_required_fields_cannot_be_cleared(client):
    o = make_opp(client)
    r = client.put(f"/api/opportunities/{o['id']}", json={"name": None})
    assert r.status_code == 422


def test_deadline_does_not_drift_on_resave(client):
    """P0-4: loading and saving an untouched deadline must be a no-op."""
    o = make_opp(client, deadline="2026-04-15")
    assert o["deadline"].startswith("2026-04-15T00:00:00")
    for _ in range(3):
        o = client.put(f"/api/opportunities/{o['id']}", json={"deadline": o["deadline"][:10]}).json()
    assert o["deadline"].startswith("2026-04-15T00:00:00")


def test_timezone_values_normalized_to_utc(client):
    o = make_opp(client, deadline="2026-04-15T02:00:00+02:00")
    assert o["deadline"] in ("2026-04-15T00:00:00Z", "2026-04-15T00:00:00+00:00")
    o2 = make_opp(client, name="Z", deadline="2026-04-15T00:00:00Z")
    assert o2["deadline"] == o["deadline"]


def test_min_fit_score_zero_is_respected(client):
    make_opp(client, name="A", fit_score=0)
    make_opp(client, name="B", fit_score=40)
    assert len(client.get("/api/opportunities?min_fit_score=0").json()) == 2
    assert len(client.get("/api/opportunities?min_fit_score=1").json()) == 1


def test_deadline_sort_puts_nulls_last(client):
    make_opp(client, name="Rolling", deadline=None)
    make_opp(client, name="Soon", deadline=iso_days(5))
    make_opp(client, name="Later", deadline=iso_days(50))
    asc = [o["name"] for o in client.get("/api/opportunities?sort_by=deadline&sort_order=asc").json()]
    desc = [o["name"] for o in client.get("/api/opportunities?sort_by=deadline&sort_order=desc").json()]
    assert asc == ["Soon", "Later", "Rolling"]
    assert desc == ["Later", "Soon", "Rolling"]


def test_invalid_sort_rejected(client):
    assert client.get("/api/opportunities?sort_by=name;DROP TABLE x").status_code == 422


def test_expired_flag_and_filter(client):
    old = make_opp(client, name="Old", deadline=iso_days(-3))
    applied = make_opp(client, name="Applied", deadline=iso_days(-3), status="submitted")
    fresh = make_opp(client, name="Fresh", deadline=iso_days(3))
    assert old["is_expired"] is True and old["days_left"] < 0
    assert applied["is_expired"] is False  # already submitted: not "missed"
    assert fresh["is_expired"] is False and fresh["days_left"] in (2, 3)
    names = lambda q: sorted(o["name"] for o in client.get(f"/api/opportunities?expired={q}").json())
    assert names("include") == ["Applied", "Fresh", "Old"]
    assert names("exclude") == ["Applied", "Fresh"]
    assert names("only") == ["Old"]


def test_search_and_structured_filters(client):
    make_opp(client, name="Women in AI Grant", notes="MENA founders", category="startup")
    make_opp(client, name="UNDP Tender", type="tender", needs_check=True, source="ReliefWeb")
    assert [o["name"] for o in client.get("/api/opportunities?q=mena").json()] == ["Women in AI Grant"]
    assert [o["name"] for o in client.get("/api/opportunities?q=100%25").json()] == []
    assert [o["name"] for o in client.get("/api/opportunities?category=bd").json()] == ["UNDP Tender"]
    assert [o["name"] for o in client.get("/api/opportunities?pipeline=bd").json()] == ["UNDP Tender"]
    assert [o["name"] for o in client.get("/api/opportunities?needs_check=true").json()] == ["UNDP Tender"]
    assert [o["name"] for o in client.get("/api/opportunities?source=ReliefWeb").json()] == ["UNDP Tender"]


def test_star_toggle_and_alias(client):
    o = make_opp(client)
    r = client.put(f"/api/opportunities/{o['id']}/toggle-save").json()
    assert r["is_saved"] is True and r["is_starred"] is True
    assert [x["id"] for x in client.get("/api/opportunities?is_starred=true").json()] == [o["id"]]
    assert client.put(f"/api/opportunities/{o['id']}", json={"is_starred": False}).json()["is_starred"] is False


def test_counts_by_type_live_counts(client):
    make_opp(client, name="A", type="accelerator")
    make_opp(client, name="B", type="government", category="bd", needs_check=True)
    make_opp(client, name="C", type="tender", deadline=iso_days(-1))
    c = client.get("/api/opportunities/counts-by-type?pipeline=bd&expired=exclude").json()
    assert c["counts"] == {"government": 1}
    assert c["needs_check_count"] == 1 and c["auth_count"] == 1
    assert c["expired_count"] == 1
    assert c["by_category"] == {"all": 2, "startup": 1, "bd": 1}
