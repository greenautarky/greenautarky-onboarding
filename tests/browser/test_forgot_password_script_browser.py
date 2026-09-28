"""The retarget script, run in a real browser — no device, no network.

``test_forgot_password_link.py`` proves the script reaches the login page. It
cannot prove the script DOES anything: the link is rendered by Lit after the
script has run, and re-rendered on every login step. Only a browser can show
that. Playwright answers every request itself (``page.route``), so nothing
leaves the machine and pytest_socket stays untouched.

The fake page renders the link the way ``ha-auth-flow`` does — same class,
same stock ``href``/``target``/``rel`` — late, and then replaces it.

    pytest tests/browser -m browser -o addopts=""

CI runs this in the ``browser`` job, with ``GA_REQUIRE_BROWSER=1`` so a missing
Playwright FAILS instead of skipping (a skipped proof is no proof).
"""

from __future__ import annotations

import os

import pytest

if os.environ.get("GA_REQUIRE_BROWSER"):
    import playwright.async_api as playwright_async
else:
    playwright_async = pytest.importorskip(
        "playwright.async_api", reason="playwright not installed"
    )

from greenautarky_site.onboarding.forgot_password_link import (
    HELP_URL,
    RESET_PAGE_URL,
    inject_forgot_password_script,
)

pytestmark = [pytest.mark.browser, pytest.mark.asyncio]

ORIGIN = "http://ga-device.test"
STOCK_HREF = "https://www.home-assistant.io/docs/locked_out/#forgot-password"

HELP_HREF = "https://www.home-assistant.io/docs/authentication/"
GA_HELP_PATH = "/ga-help-for-this-test"

# Renders the link and the footer 150 ms after load, then replaces both with
# NEW nodes at 400 ms — the second render is what a login-step change does.
# ``ha-button`` is modelled as Home Assistant's is: a custom element whose
# ``href``/``target`` end up on an anchor inside its shadow root, so a click on
# it navigates (or opens a tab) like the real one.
FAKE_AUTHORIZE = f"""<!DOCTYPE html><html><head><title>Home Assistant</title></head>
<body><div class="content"><ha-authorize></ha-authorize></div>
<script>
  customElements.define("ha-button", class extends HTMLElement {{
    static get observedAttributes() {{ return ["href", "target", "rel"]; }}
    constructor() {{
      super();
      this.attachShadow({{mode: "open"}}).innerHTML = "<a part=base><slot></slot></a>";
    }}
    attributeChangedCallback() {{
      var a = this.shadowRoot.querySelector("a");
      ["href", "target", "rel"].forEach(function (n) {{
        var v = this.getAttribute(n);
        if (v === null) a.removeAttribute(n); else a.setAttribute(n, v);
      }}, this);
    }}
  }});
  function render(label, n) {{
    document.querySelector("ha-authorize").innerHTML =
      '<ha-auth-flow><a class="forgot-password" href="{STOCK_HREF}" ' +
      'target="_blank" rel="noreferrer noopener">' + label + '</a></ha-auth-flow>' +
      '<div class="footer"><ha-button appearance="plain" href="{HELP_HREF}" ' +
      'target="_blank" rel="noreferrer noopener">Hilfe ' + n + '</ha-button></div>';
  }}
  setTimeout(function () {{ render("Passwort vergessen?", 1); }}, 150);
  setTimeout(function () {{ render("Passwort vergessen? (2)", 2); }}, 400);
</script>
</body></html>"""

RESET_PAGE = "<html><head><title>Passwort zurücksetzen</title></head><body></body></html>"


@pytest.fixture
async def browser_page():
    async with playwright_async.async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        yield context
        await browser.close()


async def _serve(context, authorize_html: str):
    async def handle(route):
        url = route.request.url
        if url.startswith(f"{ORIGIN}/auth/authorize"):
            await route.fulfill(body=authorize_html, content_type="text/html; charset=utf-8")
        elif url.startswith(f"{ORIGIN}{RESET_PAGE_URL}"):
            await route.fulfill(body=RESET_PAGE, content_type="text/html; charset=utf-8")
        elif url.startswith(f"{ORIGIN}{GA_HELP_PATH}"):
            await route.fulfill(body="<html><title>GA Hilfe</title></html>",
                                content_type="text/html; charset=utf-8")
        else:  # home-assistant.io and anything else: never leave the machine
            await route.fulfill(body="<html><title>elsewhere</title></html>",
                                content_type="text/html")

    await context.route("**/*", handle)
    page = await context.new_page()
    await page.goto(f"{ORIGIN}/auth/authorize?client_id={ORIGIN}/")
    return page


async def test_stock_page_reproduces_the_defect(browser_page):
    """Without our script the fake page must behave like the device did: the
    link opens home-assistant.io in a new tab. If this ever passes differently,
    the fixture no longer models Home Assistant and the tests below prove
    nothing."""
    page = await _serve(browser_page, FAKE_AUTHORIZE)
    await page.get_by_text("Passwort vergessen? (2)").wait_for()
    link = page.locator("a.forgot-password")
    assert await link.get_attribute("href") == STOCK_HREF
    async with browser_page.expect_page() as popup:
        await link.click()
    assert (await popup.value).url.startswith("https://www.home-assistant.io/")


async def test_stock_page_help_button_points_at_home_assistant(browser_page):
    """Same guard for the footer's Help button (ADR-0040 D7): without our
    script it must behave like Home Assistant's — href on home-assistant.io,
    opening a new tab — or the Help tests below prove nothing."""
    page = await _serve(browser_page, FAKE_AUTHORIZE)
    await page.get_by_text("Hilfe 2", exact=True).wait_for()
    help_button = page.locator("ha-button")
    assert await help_button.get_attribute("href") == HELP_HREF
    async with browser_page.expect_page() as popup:
        await help_button.click()
    assert (await popup.value).url.startswith("https://www.home-assistant.io/")


async def test_help_button_is_removed_while_there_is_no_help_page(browser_page):
    """The shipped default: HELP_URL is empty, so the button is gone — also
    after the re-render, which creates a NEW button."""
    assert HELP_URL == ""  # precondition: this test is about the shipped default
    page = await _serve(browser_page, inject_forgot_password_script(FAKE_AUTHORIZE))
    await page.get_by_text("Passwort vergessen? (2)").wait_for()
    await page.wait_for_function("document.querySelector('ha-button') === null")
    assert await page.locator("[href*='home-assistant.io']").count() == 0


async def test_help_button_points_at_the_help_page_once_there_is_one(browser_page):
    help_url = f"{ORIGIN}{GA_HELP_PATH}"
    page = await _serve(
        browser_page, inject_forgot_password_script(FAKE_AUTHORIZE, help_url=help_url)
    )
    await page.get_by_text("Hilfe 2", exact=True).wait_for()
    await page.wait_for_function(
        f"document.querySelector('ha-button').getAttribute('href') === '{help_url}'"
    )
    help_button = page.locator("ha-button")
    assert await help_button.get_attribute("target") is None
    await help_button.click()
    await page.wait_for_url(help_url)
    assert await page.title() == "GA Hilfe"
    assert len(browser_page.pages) == 1, "the help page opened in a new tab"


async def test_the_late_link_points_at_the_reset_page(browser_page):
    page = await _serve(browser_page, inject_forgot_password_script(FAKE_AUTHORIZE))
    link = page.locator("a.forgot-password")
    await link.wait_for()
    await page.wait_for_function(
        f"document.querySelector('a.forgot-password').getAttribute('href') === '{RESET_PAGE_URL}'"
    )
    assert await link.get_attribute("target") is None, (
        "target=_blank survived — the reset page would open in a second tab "
        "and its 'Zur Anmeldung' link would lead nowhere useful"
    )


async def test_a_rerendered_link_is_retargeted_too(browser_page):
    """The second render is a new node: a one-shot rewrite would miss it."""
    page = await _serve(browser_page, inject_forgot_password_script(FAKE_AUTHORIZE))
    await page.get_by_text("Passwort vergessen? (2)").wait_for()
    await page.wait_for_function(
        f"document.querySelector('a.forgot-password').getAttribute('href') === '{RESET_PAGE_URL}'"
    )


async def test_clicking_opens_the_reset_page_in_the_same_tab(browser_page):
    page = await _serve(browser_page, inject_forgot_password_script(FAKE_AUTHORIZE))
    await page.get_by_text("Passwort vergessen? (2)").wait_for()
    await page.locator("a.forgot-password").click()
    await page.wait_for_url(f"{ORIGIN}{RESET_PAGE_URL}")
    assert await page.title() == "Passwort zurücksetzen"
    assert len(browser_page.pages) == 1, "the reset page opened in a new tab"
