"""The setup page in a real browser: a refused POST /complete is never swallowed.

Odoo #1695. Since 2.12.10 ``POST /complete`` answers 409 ``consent_missing`` when
no current GDPR consent is recorded. The page used to do
``try { await complete() } catch {}`` and then ``location.assign('/')``: the
resident landed on the start page with no message and ``completed`` still false.

The subject is the COMMITTED bundle (the bytes devices run), served from disk by
Playwright, with every API answer faked. The wizard is put on its last step
through the panel's own state, then driven by clicks, so ``_handleStep`` and the
real fetch code run. Not driven here: the steps before it (PIN, account) -- the
account step needs a real auth code and a websocket; see the PR description.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

if os.environ.get("GA_REQUIRE_BROWSER"):
    import playwright.async_api as playwright_async
else:
    playwright_async = pytest.importorskip("playwright.async_api", reason="playwright not installed")

pytestmark = [pytest.mark.browser, pytest.mark.asyncio]

ORIGIN = "http://ga-device.test"
BUNDLE = (pathlib.Path(__file__).resolve().parents[2]
          / "src/greenautarky_site/frontend_bundle")
STATIC = "/greenautarky_site_static/"
HOME = '<!DOCTYPE html><html><body><p id="home">start page</p></body></html>'
SERVER_MSG = "Required consent not recorded: gdpr. Accept the privacy policy (GDPR) before finishing the setup."


def _json(status: int, body: dict) -> dict:
    return {"status": status, "body": json.dumps(body), "content_type": "application/json"}


async def _run(complete_answers: list[dict], *, after_first: str | None = None):
    """Drive ethernet -> (complete answers in order). Returns observations."""
    seen: list[str] = []
    async with playwright_async.async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        answers = list(complete_answers)

        async def route(r):
            url = r.request.url
            path = url.replace(ORIGIN, "").split("?")[0]
            if path.startswith("/api/greenautarky_site/"):
                name = path.rsplit("/", 1)[-1]
                seen.append(name)
                if name == "complete":
                    await r.fulfill(**answers.pop(0))
                elif name == "status":
                    await r.fulfill(**_json(200, {"completed": False, "gdpr_accepted": False,
                                                  "steps_done": [], "pin_required": False}))
                else:
                    await r.fulfill(**_json(200, {"status": "ok"}))
            elif path == "/greenautarky-setup.html":
                await r.fulfill(body=(BUNDLE / "greenautarky-setup.html").read_text(),
                                content_type="text/html")
            elif path.startswith(STATIC):
                f = BUNDLE / path[len(STATIC):]
                if f.is_file():
                    await r.fulfill(body=f.read_bytes(), content_type="application/javascript")
                else:
                    await r.fulfill(status=404, body="")
            elif path == "/":
                seen.append("HOME")
                await r.fulfill(body=HOME, content_type="text/html")
            else:
                await r.fulfill(status=404, body="")

        await page.route("**/*", route)
        await page.goto(f"{ORIGIN}/greenautarky-setup.html")
        await page.wait_for_selector("ha-panel-greenautarky-setup", state="attached")
        await page.wait_for_function(
            "() => customElements.get('ha-panel-greenautarky-setup') && "
            "document.querySelector('ha-panel-greenautarky-setup').localize")
        # Last step of the wizard, set through the panel's own state.
        await page.evaluate(
            "() => { const p = document.querySelector('ha-panel-greenautarky-setup');"
            " p._currentStep = 'ethernet'; return p.updateComplete; }")
        await page.locator("ga-setup-ethernet ha-button:not(.back)").click()
        await page.wait_for_timeout(1200)
        out = {"path": page.url.replace(ORIGIN, ""), "seen": list(seen),
               "text": await page.evaluate("() => document.body.innerText") if "/greenautarky" in page.url else ""}
        if after_first == "reconsent" and "/greenautarky" in page.url:
            out["gdpr_visible"] = await page.locator("ga-setup-gdpr").is_visible()
            out["notice"] = await page.locator(".ga-notice").inner_text()
            await page.locator("ga-setup-gdpr ha-checkbox").click()
            await page.locator("ga-setup-gdpr ha-button:not(.back)").click()
            await page.wait_for_timeout(1200)
            out["path_after"] = page.url.replace(ORIGIN, "")
            out["seen_after"] = list(seen)
        elif "/greenautarky" in page.url:
            out["notice"] = (await page.locator(".ga-notice").inner_text()
                             if await page.locator(".ga-notice").count() else "")
            out["ethernet_visible"] = await page.locator("ga-setup-ethernet").is_visible()
        await browser.close()
        return out


async def test_happy_path_still_reaches_the_start_page():
    """Must-pass: a successful /complete leaves the wizard for '/'."""
    out = await _run([_json(200, {"status": "ok", "redirect": "/"})])
    assert out["path"] == "/", out
    assert out["seen"].count("complete") == 1


async def test_a_409_consent_missing_is_shown_and_leads_back_to_gdpr():
    out = await _run([_json(409, {"code": "consent_missing", "missing": ["gdpr"], "message": SERVER_MSG}),
                      _json(200, {"status": "ok", "redirect": "/"})],
                     after_first="reconsent")
    # No silent redirect, the server's message is on screen, GDPR step is shown.
    assert out["path"].startswith("/greenautarky-setup"), f"redirected away: {out}"
    assert SERVER_MSG in out["notice"], out
    assert "Datenschutzerklärung" in out["notice"], "the German UI text is missing"
    assert out["gdpr_visible"], out
    # Re-consenting finishes the setup and must not re-run the account step.
    assert "create_user" not in out["seen_after"], out
    assert out["seen_after"].count("gdpr") == 1 and out["seen_after"].count("complete") == 2, out
    assert out["path_after"] == "/", out


@pytest.mark.parametrize("status, body", [
    (409, {"message": "No resident account on this device yet"}),
    (403, {"message": "Onboarding already completed"}),
    (500, {"message": "boom"}),
])
async def test_any_other_refusal_shows_a_generic_error_and_stays(status, body):
    out = await _run([_json(status, body)])
    assert out["path"].startswith("/greenautarky-setup"), f"redirected away on {status}: {out}"
    assert "konnte nicht abgeschlossen werden" in out["notice"], out
    assert out["ethernet_visible"], out
    assert "HOME" not in out["seen"]
