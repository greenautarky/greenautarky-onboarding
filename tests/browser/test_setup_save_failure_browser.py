"""The setup page in a real browser: a save that fails is shown, not passed over.

The telemetry page saves over the websocket, the Ethernet page with a POST. When
that fails the page stays, says so in German and lets the resident try again; the
next page opens only after a save that succeeded, or after the explicit
"Ohne Speichern weiter". The subject is the COMMITTED bundle, as in
test_setup_complete_refusal_browser.py; only the answers are faked.

GA_WIZARD_BUNDLE points the tests at another built bundle.
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
BUNDLE = pathlib.Path(os.environ.get("GA_WIZARD_BUNDLE") or pathlib.Path(__file__).resolve().parents[2]
                      / "src/greenautarky_site/frontend_bundle")
STATIC = "/greenautarky_site_static/"
DE_FAILED = "Ihre Auswahl konnte nicht gespeichert werden. Bitte versuchen Sie es erneut."
DE_UNAVAILABLE = "Ihre Auswahl kann auf diesem Gerät nicht gespeichert werden."
DE_EXPIRED = ("Ihre Auswahl wurde nicht gespeichert, weil die Sitzung abgelaufen ist. "
              "Bitte starten Sie die Einrichtung neu.")
OK = (200, {"status": "ok"})
ABORT = "abort"


class Wizard:
    """The wizard on one step, with scripted answers (the last one repeats)."""

    def __init__(self, step: str, *, ws=(), post=(), complete=OK):
        self.step, self.ws, self.post, self.complete = step, list(ws), list(post), complete
        self.posts: list[dict] = []
        self.seen: list[str] = []

    async def __aenter__(self):
        self._pw = await playwright_async.async_playwright().start()
        self.browser = await self._pw.chromium.launch()
        self.page = page = await self.browser.new_page()
        await page.route("**/*", self._route)
        await page.goto(f"{ORIGIN}/greenautarky-setup.html")
        await page.wait_for_function(
            "() => customElements.get('ha-panel-greenautarky-setup') && "
            "document.querySelector('ha-panel-greenautarky-setup')?.localize")
        await page.evaluate(
            """([step, answers]) => {
                 const p = document.querySelector('ha-panel-greenautarky-setup');
                 window.sets = [];
                 p.hass = {language: 'de', callWS: async (m) => {
                   if (m.type !== 'greenautarky_telemetry/set') return {};
                   window.sets.push(m);
                   const a = answers[Math.min(window.sets.length, answers.length) - 1];
                   if (a === 'hang') return new Promise(() => {});
                   if (a === 'lost') throw 3;
                   if (a !== 'ok') throw {code: a, message: 'English server text'};
                   return {};
                 }};
                 p._currentStep = step;
                 return p.updateComplete;
               }""", [self.step, self.ws or ["ok"]])
        self.el = page.locator(f"ga-setup-{self.step}")
        await self.el.wait_for()
        return self

    async def __aexit__(self, *_):
        await self.browser.close()
        await self._pw.stop()

    async def _route(self, r):
        path = r.request.url.replace(ORIGIN, "").split("?")[0]
        if path.startswith("/api/greenautarky_site/"):
            name = path.rsplit("/", 1)[-1]
            self.seen.append(name)
            answer = OK
            if name == "ethernet":
                self.posts.append(json.loads(r.request.post_data or "{}"))
                if self.post:
                    answer = self.post[min(len(self.posts), len(self.post)) - 1]
            elif name == "complete":
                answer = self.complete
            elif name == "status":
                answer = (200, {"completed": False, "steps_done": [], "pin_required": False})
            if answer == ABORT:
                await r.abort("connectionreset")
            else:
                await r.fulfill(status=answer[0], body=json.dumps(answer[1]), content_type="application/json")
        elif path == "/greenautarky-setup.html":
            self.seen.append("SETUP")
            await r.fulfill(body=(BUNDLE / "greenautarky-setup.html").read_text(), content_type="text/html")
        elif path.startswith(STATIC) and (BUNDLE / path[len(STATIC):]).is_file():
            await r.fulfill(body=(BUNDLE / path[len(STATIC):]).read_bytes(), content_type="application/javascript")
        elif path == "/":
            self.seen.append("HOME")
            await r.fulfill(body="<p>start page</p>", content_type="text/html")
        else:
            await r.fulfill(status=404, body="")

    async def save(self):
        await self.el.locator(".footer ha-button:not(.back)").click()
        await self.page.wait_for_timeout(500)

    async def at(self) -> str:
        if not self.page.url.startswith(f"{ORIGIN}/greenautarky-setup"):
            return self.page.url.replace(ORIGIN, "")
        return await self.page.evaluate("() => document.querySelector('ha-panel-greenautarky-setup')._currentStep")

    async def sets(self) -> list[dict]:
        return await self.page.evaluate("() => window.sets")

    async def save_is_off(self) -> bool:
        return await self.el.locator("ha-button.save").evaluate("(b) => b.disabled")

    async def message(self) -> str:
        n = self.el.locator("ha-alert.save-failed")
        return " ".join((await n.inner_text()).split()) if await n.count() else ""

    async def count(self, cls: str) -> int:
        return await self.el.locator(f"ha-button.{cls}").count()

    async def label(self) -> str:
        return (await self.el.locator(".footer ha-button:not(.back)").inner_text()).strip()


# ------------------------------------------------------------ telemetry page ---


async def test_telemetry_a_successful_save_goes_on_with_the_chosen_values():
    async with Wizard("analytics") as w:
        await w.el.locator("ha-switch[name=ga_error_logs]").click()   # the NON-default choice
        await w.el.locator("ha-switch[name=ga_metrics]").click()
        await w.save()
        assert await w.at() == "ethernet"
        (sent,) = await w.sets()
        assert (sent["error_logs"], sent["metrics"]) == (False, True)


@pytest.mark.parametrize("answer", ["unknown_error", "unauthorized", "lost"])
async def test_telemetry_a_failed_save_stays_and_says_so(answer):
    async with Wizard("analytics", ws=[answer]) as w:
        await w.save()
        assert await w.at() == "analytics", "went on although nothing was saved"
        assert await w.message() == DE_FAILED
        assert await w.label() == "Erneut versuchen"
        assert await w.count("continue-unsaved") == 0, "the way past is not offered on the first failure"


async def test_telemetry_a_retry_sends_the_same_choice_and_goes_on():
    async with Wizard("analytics", ws=["unknown_error", "ok"]) as w:
        await w.el.locator("ha-switch[name=ga_error_logs]").click()
        await w.save()
        assert await w.at() == "analytics"
        await w.save()
        assert await w.at() == "ethernet"
        assert [s["error_logs"] for s in await w.sets()] == [False, False], "the choice changed on retry"


async def test_telemetry_going_on_unsaved_is_explicit_and_sends_nothing_more():
    async with Wizard("analytics", ws=["unknown_error"]) as w:
        await w.save()
        await w.save()
        assert await w.at() == "analytics"
        assert await w.count("continue-unsaved") == 1
        await w.el.locator("ha-button.continue-unsaved").click()
        await w.page.wait_for_timeout(300)
        assert await w.at() == "ethernet"
        assert len(await w.sets()) == 2


async def test_telemetry_a_device_without_the_setting_is_not_a_dead_end():
    async with Wizard("analytics", ws=["unknown_command"]) as w:
        await w.save()
        assert await w.at() == "analytics"
        assert await w.message() == DE_UNAVAILABLE
        assert await w.count("continue-unsaved") == 1


async def test_telemetry_a_save_that_never_answers_ends_in_the_message():
    async with Wizard("analytics", ws=["hang"]) as w:
        await w.save()
        assert await w.save_is_off(), "a running save can be sent twice"
        await w.page.wait_for_timeout(15500)
        assert await w.at() == "analytics"
        assert await w.message() == DE_FAILED



async def test_telemetry_enter_does_not_bypass_the_check():
    async with Wizard("analytics", ws=["unknown_error"]) as w:
        await w.el.evaluate("(e) => e.dispatchEvent(new KeyboardEvent('keypress', {key: 'Enter', bubbles: true}))")
        await w.page.wait_for_timeout(500)
        assert await w.at() == "analytics"
        assert await w.message() == DE_FAILED


# ------------------------------------------------------------- ethernet page ---


async def test_ethernet_a_successful_save_finishes_with_the_chosen_value():
    async with Wizard("ethernet") as w:
        await w.el.locator("ha-switch[name=enable_ethernet]").click()
        await w.save()
        assert await w.at() == "/"
        assert w.posts == [{"enable_ethernet": True}]
        assert w.seen.count("complete") == 1


@pytest.mark.parametrize("answer", [(500, {"message": "English server text"}), (409, {}), ABORT])
async def test_ethernet_a_failed_save_stays_and_does_not_finish_the_setup(answer):
    async with Wizard("ethernet", post=[answer]) as w:
        await w.save()
        assert await w.at() == "ethernet", "went on although nothing was saved"
        assert "complete" not in w.seen, "the setup was finished although the save failed"
        assert await w.message() == DE_FAILED
        assert await w.count("continue-unsaved") == 0


async def test_ethernet_a_retry_goes_on():
    async with Wizard("ethernet", post=[(500, {}), OK]) as w:
        await w.save()
        assert await w.at() == "ethernet"
        await w.save()
        assert await w.at() == "/"
        assert len(w.posts) == 2 and w.seen.count("complete") == 1


async def test_ethernet_going_on_unsaved_is_explicit():
    async with Wizard("ethernet", post=[(500, {})]) as w:
        await w.save()
        await w.save()
        assert await w.at() == "ethernet"
        await w.el.locator("ha-button.continue-unsaved").click()
        await w.page.wait_for_timeout(600)
        assert await w.at() == "/" and len(w.posts) == 2


async def test_ethernet_an_expired_session_offers_only_the_restart():
    async with Wizard("ethernet", post=[(403, {"error": "PIN verification required"})]) as w:
        await w.save()
        assert await w.at() == "ethernet"
        assert "complete" not in w.seen
        assert await w.message() == DE_EXPIRED
        assert await w.count("continue-unsaved") == 0 and await w.count("save-restart") == 1
        before = w.seen.count("SETUP")
        await w.el.locator("ha-button.save-restart").click()
        await w.page.wait_for_timeout(800)
        assert w.seen.count("SETUP") == before + 1


async def test_ethernet_a_setup_finished_elsewhere_leaves_for_the_start_page():
    done = (403, {"code": "already_completed", "message": "Onboarding already completed"})
    async with Wizard("ethernet", post=[done], complete=done) as w:
        await w.save()
        assert await w.at() == "/"
