"""
Headless UI smoke test: every view at desktop (1440) and phone (375) width.

Fails if any view logs a console error, a request fails, the page scrolls
horizontally, or a core interaction stops working. Screenshots go to --out.

    uvicorn backend.main:app --port 8765 &
    python scripts/ui_smoke.py --base http://127.0.0.1:8765 --out docs/screenshots/after
    # with AUTH_MODE=full on the server:
    python scripts/ui_smoke.py --base ... --passcode <TEAM_PASSCODE>

Chromium: uses PLAYWRIGHT_CHROMIUM (path) if set, else Playwright's bundled browser.
"""
import argparse
import os
import sys

from playwright.sync_api import sync_playwright

VIEWS = [
    ("dashboard", "#dashboard"),
    ("opportunities", "#opportunities/all"),
    ("startup", "#opportunities/startup"),
    ("bd", "#opportunities/bd"),
    ("starred", "#opportunities/starred"),
    ("needs-check", "#opportunities/needs_check"),
    ("expired", "#opportunities/expired"),
    ("readiness", "#readiness"),
    ("activity", "#activity"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--out", default="docs/screenshots/after")
    ap.add_argument("--passcode")
    ap.add_argument("--interact", action="store_true", help="also exercise star/edit/search (writes to the DB!)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    failures = []

    with sync_playwright() as p:
        launch = {"executable_path": os.environ["PLAYWRIGHT_CHROMIUM"]} if os.environ.get("PLAYWRIGHT_CHROMIUM") else {}
        browser = p.chromium.launch(**launch)
        for label, vp in (("desktop", {"width": 1440, "height": 900}), ("mobile", {"width": 375, "height": 812})):
            ctx = browser.new_context(viewport=vp, device_scale_factor=1 if label == "desktop" else 2)
            page = ctx.new_page()
            errors = []
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("requestfailed", lambda r: errors.append(f"requestfailed: {r.url} {r.failure}")
                    if "fonts.g" not in r.url and "ERR_ABORTED" not in (r.failure or "") else None)
            page.on("response", lambda r: errors.append(f"HTTP {r.status}: {r.url}")
                    if r.status >= 400 and r.status != 401 else None)

            page.goto(args.base + "/", wait_until="networkidle")
            if page.is_visible("#lock.open"):
                if not args.passcode:
                    failures.append(f"{label}: lock screen shown but no --passcode given")
                    continue
                page.fill("#lock-form input[name=passcode]", args.passcode)
                page.click("#lock-form button[type=submit]")
                page.wait_for_selector("#lock.open", state="hidden")
            for name, hash_ in VIEWS:
                page.evaluate(f"location.hash = '{hash_}'; location.reload()")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(400)
                if page.locator(".skel").count():
                    failures.append(f"{label}/{name}: still showing loading skeletons")
                sw = page.evaluate("document.documentElement.scrollWidth")
                if sw > vp["width"]:
                    failures.append(f"{label}/{name}: horizontal scroll ({sw}px > {vp['width']}px)")
                page.screenshot(path=f"{args.out}/{label}-{name}.png", full_page=(label == "desktop" and name == "dashboard"))

            # interactions (read-only unless --interact)
            page.evaluate("location.hash = '#opportunities/all'; location.reload()")
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("#type-tabs .tab", timeout=10000)
            tabs = page.locator("#type-tabs .tab")
            if tabs.count() < 4:
                failures.append(f"{label}: expected live-count filter tabs, got {tabs.count()}")
            else:
                tabs.nth(1).click(); page.wait_for_load_state("networkidle"); page.wait_for_timeout(200)
                n_tab = int(tabs.nth(1).locator(".n").inner_text())
                n_cards = page.locator("#opp-list .card").count()
                if n_tab != n_cards:
                    failures.append(f"{label}: tab badge says {n_tab} but {n_cards} cards rendered")
            page.click(".pipe-btn[data-pipe=bd]"); page.wait_for_load_state("networkidle"); page.wait_for_timeout(200)
            badge = page.locator("#type-tabs .tab").first.locator(".n").inner_text()
            cards = page.locator("#opp-list .card").count()
            if int(badge) != cards:
                failures.append(f"{label}: BD 'All' badge {badge} != {cards} cards")
            page.fill("#search", "zzzz-no-match"); page.wait_for_timeout(600); page.wait_for_load_state("networkidle")
            if "Nothing matches" not in page.inner_text("#opp-list"):
                failures.append(f"{label}: search empty state missing")
            page.fill("#search", ""); page.wait_for_timeout(600); page.wait_for_load_state("networkidle")
            if page.locator("#opp-list .card").count():
                page.locator("#opp-list .card").first.click()
                if not page.is_visible("#opp-modal.open"):
                    failures.append(f"{label}: edit modal did not open")
                page.screenshot(path=f"{args.out}/{label}-edit-modal.png")
                if args.interact:
                    page.click("#opp-form button[type=submit]"); page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(300)
                    if page.is_visible("#opp-modal.open"):
                        failures.append(f"{label}: save did not close the modal")
                    page.locator("#opp-list .star").first.click(); page.wait_for_load_state("networkidle")
                else:
                    page.keyboard.press("Escape")
            if errors:
                failures.extend(f"{label}: {e}" for e in errors)
            ctx.close()
        browser.close()

    if failures:
        print("UI SMOKE FAILED")
        print("\n".join(f"  - {f}" for f in failures))
        sys.exit(1)
    print(f"UI smoke passed: {len(VIEWS)} views x 2 viewports, 0 console errors, no horizontal scroll. Screenshots in {args.out}")


if __name__ == "__main__":
    main()
