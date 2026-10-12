from conftest import make_opp


def post(client, **kw):
    body = {"name": "x", "type": "accelerator"}
    body.update(kw)
    return client.post("/api/opportunities", json=body)


def test_same_url_and_similar_title_dedups_and_fills(client):
    a = post(client, name="Flat6Labs Cairo Accelerator", url="https://www.flat6labs.com/cairo/").json()
    r = post(client, name="Flat6Labs Cairo Accelerator Program", url="https://flat6labs.com/cairo?utm=x",
             funding_amount="$20K", deadline="2026-12-01")
    assert r.headers["X-Duplicate-Of"] == str(a["id"])
    assert r.json()["funding_amount"] == "$20K" and r.json()["deadline"].startswith("2026-12-01")
    log = client.get("/api/activity-log?limit=1").json()[0]
    assert log["action"] == "deduplicated"


def test_canonical_name_dedups(client):
    a = post(client, name="Microsoft for Startups Founders Hub 2026", url="https://a.example").json()
    r = post(client, name="Microsoft Startups Founders Hub", url="https://b.example")
    assert r.headers.get("X-Duplicate-Of") == str(a["id"])


def test_distinct_programmes_are_not_merged(client):
    post(client, name="Techstars Boston Accelerator", url="https://techstars.com/boston")
    r = post(client, name="Techstars London Accelerator", url="https://techstars.com/london")
    assert "X-Duplicate-Of" not in r.headers
    post(client, name="Seedstars Open Calls", url="https://seedstars.com/programs/#open")
    r = post(client, name="Seedstars Demo Days", url="https://seedstars.com/programs/#demo-days")
    assert "X-Duplicate-Of" not in r.headers
    post(client, name="Flat6Labs Cairo", url="https://f.example/1")
    r = post(client, name="Flat6Labs KSA", url="https://f.example/2")
    assert "X-Duplicate-Of" not in r.headers


def test_fuzzy_requires_same_type(client):
    post(client, name="Innovation Grants Egypt", type="grant", url="https://a.example")
    r = post(client, name="Innovation Grant Egypt", type="competition", url="https://b.example")
    assert "X-Duplicate-Of" not in r.headers
    r = post(client, name="Innovation Grant Egypt", type="grant", url="https://c.example")
    assert "X-Duplicate-Of" in r.headers


def test_allow_duplicate_bypasses(client):
    make_opp(client, name="Same", url="https://s.example")
    make_opp(client, name="Same", url="https://s.example")
    assert len(client.get("/api/opportunities").json()) == 2


def test_merge_script_plans_groups():
    from merge_duplicates import group_duplicates, plan_group
    opps = [
        {"id": 1, "name": "TechCrunch Startup Battlefield 200", "type": "competition", "url": "https://tc/1",
         "status": "researching", "fit_score": 80, "priority": 2, "notes": "a", "is_saved": False},
        {"id": 2, "name": "TechCrunch Startup Battlefield 200", "type": "competition", "url": "https://tc/2",
         "status": "preparing", "fit_score": 85, "priority": 1, "notes": "b", "is_saved": False,
         "deadline": "2026-05-27T00:00:00"},
        {"id": 3, "name": "Techstars Boston", "type": "accelerator", "url": "https://ts/b",
         "status": "researching", "fit_score": 50, "priority": 3, "notes": None, "is_saved": False},
    ]
    groups, _ = group_duplicates(opps)
    assert len(groups) == 1 and {o["id"] for o in groups[0]} == {1, 2}
    keeper, dupes, patch = plan_group(groups[0])
    assert keeper["id"] == 2 and [d["id"] for d in dupes] == [1]   # further-along status wins
    assert "[merged #1] a" in patch["notes"]
