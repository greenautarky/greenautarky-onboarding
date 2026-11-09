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
# The page shows FIXED text keyed on the code, never the server's English message.
DE_CONSENT = ("Die Einrichtung konnte nicht abgeschlossen werden, weil die Zustimmung zur "
              "Datenschutzerklärung fehlt. Bitte stimme ihr erneut zu.")
CONSENT_409 = {"code": "consent_missing", "missing": ["gdpr"], "message": SERVER_MSG}
NETWORK_ERROR = "abort"


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
                    answer = answers.pop(0)
                    if answer == NETWORK_ERROR:
                        await r.abort("connectionreset")
                    else:
                        await r.fulfill(**answer)
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
        elif after_first == "back" and "/greenautarky" in page.url:
            out["notice"] = await page.locator(".ga-notice").inner_text()
            out["gdpr_visible"] = await page.locator("ga-setup-gdpr").is_visible()
            await page.go_back()
            await page.wait_for_timeout(600)
            out["path_after"] = page.url.replace(ORIGIN, "")
            out["ethernet_visible_after"] = await page.locator("ga-setup-ethernet").is_visible()
            out["notice_count_after"] = await page.locator(".ga-notice").count()
            out["recovery_after"] = await page.evaluate(
                "() => document.querySelector('ha-panel-greenautarky-setup')._consentRecovery")
            out["seen_after"] = list(seen)
        elif "/greenautarky" in page.url:
            out["notice"] = (await page.locator(".ga-notice").inner_text()
                             if await page.locator(".ga-notice").count() else "")
            out["home_link"] = (await page.locator(".ga-notice a[href='/']").count()
                                if out["notice"] else 0)
            out["ethernet_visible"] = await page.locator("ga-setup-ethernet").is_visible()
        await browser.close()
        return out


async def test_happy_path_still_reaches_the_start_page():
    """Must-pass: a successful /complete leaves the wizard for '/'."""
    out = await _run([_json(200, {"status": "ok", "redirect": "/"})])
    assert out["path"] == "/", out
    assert out["seen"].count("complete") == 1


async def test_a_409_consent_missing_is_shown_and_leads_back_to_gdpr():
    out = await _run([_json(409, CONSENT_409), _json(200, {"status": "ok", "redirect": "/"})],
                     after_first="reconsent")
    # No silent redirect, fixed German text is on screen, GDPR step is shown.
    assert out["path"].startswith("/greenautarky-setup"), f"redirected away: {out}"
    assert out["notice"].strip() == DE_CONSENT, out
    assert SERVER_MSG not in out["notice"], "the server's English message must not be shown"
    assert out["gdpr_visible"], out
    # Re-consenting finishes the setup and must not re-run the account step.
    assert "create_user" not in out["seen_after"], out
    assert out["seen_after"].count("gdpr") == 1 and out["seen_after"].count("complete") == 2, out
    assert out["path_after"] == "/", out


async def test_a_403_already_completed_is_success():
    """The finish landed earlier (e.g. a dropped response): the retry's 403 must
    leave for '/', not trap the resident on the last step."""
    out = await _run([_json(403, {"code": "already_completed", "message": "Onboarding already completed"})])
    assert out["path"] == "/", out
    assert out["seen"].count("complete") == 1


async def test_a_403_from_the_pin_gate_is_not_success():
    out = await _run([_json(403, {"error": "PIN verification required"})])
    assert out["path"].startswith("/greenautarky-setup"), out
    assert "HOME" not in out["seen"]


@pytest.mark.parametrize("status, body", [
    (409, {"message": "No resident account on this device yet"}),
    (409, {"code": "consent_missing", "missing": ["other"], "message": "x"}),
    (500, {"message": "boom"}),
    (500, "plain text, not json"),
])
async def test_any_other_refusal_shows_a_generic_error_with_a_way_out(status, body):
    answer = _json(status, body) if isinstance(body, dict) else {
        "status": status, "body": body, "content_type": "text/plain"}
    out = await _run([answer])
    assert out["path"].startswith("/greenautarky-setup"), f"redirected away on {status}: {out}"
    assert "konnte nicht abgeschlossen werden" in out["notice"], out
    assert out["ethernet_visible"], out
    assert out["home_link"] == 1, f"no way out of the error notice: {out}"
    assert "HOME" not in out["seen"]


async def test_a_network_error_shows_a_generic_error_with_a_way_out():
    out = await _run([NETWORK_ERROR])
    assert out["path"].startswith("/greenautarky-setup"), out
    assert "konnte nicht abgeschlossen werden" in out["notice"], out
    assert out["ethernet_visible"], out
    assert out["home_link"] == 1, out
    assert "HOME" not in out["seen"]


async def test_back_after_the_recovery_clears_the_notice_and_the_recovery_flag():
    out = await _run([_json(409, CONSENT_409)], after_first="back")
    assert out["gdpr_visible"] and out["notice"].strip() == DE_CONSENT, out
    assert out["path_after"].startswith("/greenautarky-setup"), out
    assert out["ethernet_visible_after"], f"Back did not return to the last step: {out}"
    assert out["notice_count_after"] == 0, f"the stale notice survived Back: {out}"
    assert out["recovery_after"] is False, f"the recovery flag survived Back: {out}"
