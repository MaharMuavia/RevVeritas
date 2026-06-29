"""E2E Playwright tests for RevVeritas.

Spawns the FastAPI application in a background thread and uses Playwright
to navigate and test the UI flows.
"""
import re
import time
import threading
import urllib.request
import pytest
from playwright.sync_api import sync_playwright, expect

from backend.main import app


class UvicornServer(threading.Thread):
    """Programmatically run FastAPI app via Uvicorn in a background thread."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8011):
        super().__init__()
        import uvicorn
        self.config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.daemon = True

    def run(self):
        self.server.run()

    def stop(self):
        self.server.should_exit = True


@pytest.fixture(scope="session")
def server_url():
    # Start the server in a background thread
    server = UvicornServer()
    server.start()

    # Wait for the server to be up
    timeout = 15
    start_time = time.time()
    url = "http://127.0.0.1:8011"
    while time.time() - start_time < timeout:
        try:
            with urllib.request.urlopen(f"{url}/api/health") as response:
                if response.status == 200:
                    break
        except Exception:
            pass
        time.sleep(0.3)
    else:
        raise RuntimeError("FastAPI server failed to start in background thread")

    yield url
    server.stop()
    server.join(timeout=3)


def test_revveritas_e2e_flow(server_url):
    with sync_playwright() as p:
        # Launch browser
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            # 1. Load the landing page
            page.goto(server_url)
            expect(page).to_have_title("RevVeritas — Autonomous Revenue Leakage Hunter")
            expect(page.locator("h2")).to_contain_text("Find the revenue")

            # 2. Click "Continue as guest"
            page.click("text=Continue as guest")

            # 3. Redirection should lead to /app (Dashboard)
            page.wait_for_url("**/app")
            expect(page).to_have_title("Dashboard · RevVeritas")
            expect(page.locator("#userName")).to_have_text("Guest")
            expect(page.locator("#headline")).to_have_text("$0")
            expect(page.locator("tbody#rows")).to_contain_text("No audit run yet")

            # 4. Click "Run Audit"
            page.click("button#runBtn")

            # 5. Wait for the audit to finish (headline changes from $0)
            # Timeout is generous because with a live Gemini API key, each
            # candidate triggers a real LLM round-trip.
            page.wait_for_function("document.getElementById('headline').innerText !== '$0'", timeout=180000)

            # Headline should be populated
            headline_text = page.locator("#headline").inner_text()
            assert headline_text != "$0"
            assert "$" in headline_text

            # 6. Verify that prioritized findings ledger displays rows
            rows = page.locator("tbody#rows tr")
            row_count = rows.count()
            assert row_count > 0, "No audit finding rows were rendered"

            # 7. Click the first row to open forensic drawer
            rows.first.click()

            # 8. Verify the drawer slides open and displays sections
            expect(page.locator("#drawer")).to_have_class(re.compile(r"open"))
            expect(page.locator("#dImpact")).to_contain_text("$")
            expect(page.locator("#dBody")).to_contain_text("Agent explanation")
            expect(page.locator("#dBody")).to_contain_text("Conflicting evidence")
            expect(page.locator("#dBody")).to_contain_text("Agent reasoning trace")

            # 9. Test approval gate on the first finding
            approve_btn = page.locator("button.approve")
            expect(approve_btn).to_be_visible()
            approve_btn.click()

            # 10. Verify that it was successfully resolved
            expect(page.locator("#dBody")).to_contain_text("Approved & resolved in case memory")
            expect(page.locator("#toast")).to_have_class(re.compile(r"show"))
            expect(page.locator("#toastMsg")).to_contain_text("Approved & resolved")

            # 11. Close the drawer
            page.click("button.close")
            expect(page.locator("#drawer")).not_to_have_class(re.compile(r"open"))

        finally:
            context.close()
            browser.close()
