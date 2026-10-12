"""
Hunter <-> FRD end-to-end check without network sources or an Anthropic key.

Runs the real hunter.py pipeline (dedup, dashboard GET, batch evaluation parsing,
DashboardClient POST with X-API-Key) against a running FRD. Only the scrapers and
the model call are stubbed with canned answers that exercise the contract edges
(BD-tagged government type, auth wall, 'Rolling' deadline, out-of-range numbers,
a duplicate of an existing programme).

    HUNTER_REPO=../rootrise-opportunity-hunter \
    DASHBOARD_API_URL=http://127.0.0.1:8765/api DASHBOARD_API_KEY=... \
    python scripts/hunter_e2e.py --mode rss-only [--dry-run]
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.environ.get("HUNTER_REPO", "../rootrise-opportunity-hunter")))
import json
import types
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-stub")
sys.argv = ["hunter.py"] + sys.argv[1:]
import hunter

RAW = [
    hunter.RawOpportunity(title="UNICEF Digital Feedback Platform TOR", source="ReliefWeb", url="https://reliefweb.int/job/e2e-unicef", description="TOR for digital feedback platform"),
    hunter.RawOpportunity(title="AI Founders Grant MENA 2026", source="WebSearch", url="https://grants.example.org/e2e-ai-founders", description="grant", access_status="auth_required"),
    hunter.RawOpportunity(title="Rolling Cloud Credits Program", source="Known:Example", url="https://cloud.example.org/e2e-credits", description="credits"),
    hunter.RawOpportunity(title="Google for Startups Accelerator: MENA (Cohort 11)", source="WebSearch", url="https://news.example/e2e-google", description="dup of existing"),
]
ANSWERS = [
    {"relevant": True, "category": "bd", "name": "UNICEF Digital Feedback Platform TOR", "type": "government", "fit_score": 82, "priority": 1, "deadline": "2026-11-30", "notes": "ICT4D fit", "funding_amount": "$40K"},
    {"relevant": True, "category": "startup", "name": "AI Founders Grant MENA", "type": "fellowship", "fit_score": 74, "priority": 2, "deadline": "Rolling", "notes": "women-led eligible"},
    {"relevant": True, "category": "startup", "name": "Rolling Cloud Credits Program", "type": "cloud_credits", "fit_score": 140, "priority": 9, "deadline": None, "notes": "credits"},
    {"relevant": True, "category": "startup", "name": "Google for Startups Accelerator MENA 2026", "type": "accelerator", "fit_score": 95, "priority": 1, "deadline": "2026-12-15", "notes": "dup"},
]

hunter.WebSearchScraper.get_opportunities = lambda self, **k: ([], hunter.SourceHealth("stub", "ok"))
hunter.RSSNetworkScraper.get_opportunities = lambda self: (list(RAW), [hunter.SourceHealth("stub-rss", "ok", len(RAW))])
hunter.cheap_pre_filter = lambda opp: True

class B:
    def __init__(s, t, x=None): s.type, s.text = t, x
def fake_create(**kw):
    prompt = kw["messages"][0]["content"]
    n = prompt.count("--- ITEM ")
    titles = [r.title for r in RAW]
    picked = [ANSWERS[titles.index(t)] for t in titles if f"Title: {t}\n" in prompt][:n]
    for i, a in enumerate(picked, 1): a["item"] = i
    return types.SimpleNamespace(stop_reason="end_turn", content=[B("thinking"), B("text", json.dumps(picked))])

orig_init = hunter.OpportunityEvaluator.__init__
def init(self, *a, **k):
    orig_init(self, *a, **k)
    self.client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=fake_create)))
hunter.OpportunityEvaluator.__init__ = init
hunter.fetch_page_text = lambda url, max_chars=3000: (None, "ok")

code = hunter.main()
print("EXIT", code)
