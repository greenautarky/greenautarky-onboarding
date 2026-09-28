"""Point the login page's "Forgot password?" link at our PIN reset (ADR-0040).

Home Assistant's login page (``/auth/authorize``) is stock. Its "Forgot
password?" link is hard-coded in the frontend (``ha-auth-flow``) to
``home-assistant.io/docs/locked_out`` — container command lines, written for
the person who runs the server. A resident who forgot the password landed
there, while this component has shipped a PIN-gated reset page
(``/greenautarky-password-reset``) since 1.x that nothing linked to.

Why a server-side injection and not ``add_extra_js_url``: Core injects extra
module URLs only into the authenticated dashboard HTML. ``/auth/authorize``
never carries them (same reason the `/` → wizard redirect patches IndexView).

``/auth/authorize`` is registered by the ``frontend`` component as a plain GET
route whose handler is ``partial(_serve_file, <path to authorize.html>)``. We
swap that route's handler for one that serves the same file with a small
script before ``</body>``. The script retargets ``a.forgot-password`` and
handles the footer's "Help" button (``HELP_URL``: removed while empty); both
``ha-authorize`` and ``ha-auth-flow`` render into light DOM, so a plain
``querySelector`` reaches the link.

Gated on the PIN file: the reset page is useless without it (it answers 404
"No PIN configured"), so a device without a PIN keeps Home Assistant's link.

Everything that can go wrong here goes wrong LOUDLY (WARNING) and leaves the
stock page in place: a missing link to a reset page is a support call, a broken
login page is an outage.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web
from homeassistant.core import HomeAssistant

from .password_reset import GAPasswordResetPageView
from .pin import _pin_required

_LOGGER = logging.getLogger(__name__)

AUTHORIZE_PATH = "/auth/authorize"
RESET_PAGE_URL = GAPasswordResetPageView.url

# Marks both the script tag (idempotent injection) and the wrapped handler
# (idempotent patch across an integration reload).
MARKER = "data-ga-forgot-password"
_PATCHED_ATTR = "_ga_forgot_password_patched"

# Where the login page's "Help" button points (ADR-0040 D7). Home Assistant's
# stock button (``ha-button`` in ``ha-authorize``'s footer) goes to
# home-assistant.io/docs/authentication — never right for a resident.
# EMPTY = remove the button: GA hosts no help page yet, and a link to a domain
# that does not answer is worse than no button. Set it only once the page is
# live; it then opens in the same tab.
HELP_URL = ""
HELP_SELECTOR = '[href^="https://www.home-assistant.io/docs/authentication"]'


def build_login_page_script(help_url: str | None = None) -> str:
    """The script injected into ``/auth/authorize``.

    ``help_url`` defaults to :data:`HELP_URL`; the parameter exists so the
    browser tier can prove both behaviours (removed / retargeted).

    Two mechanisms, because the links are rendered by Lit AFTER this script
    runs and re-rendered whenever the login step changes:
     - a MutationObserver rewrites href (so hover/long-press shows OUR address)
       and drops target=_blank (the reset page links back to the login
       itself) — or removes the Help button when there is no help page;
     - a capture-phase click handler catches a click on a link the observer
       has not reached yet.
    """
    help_target = HELP_URL if help_url is None else help_url
    return f"""<script {MARKER}>
(function () {{
  var TARGET = {json.dumps(RESET_PAGE_URL)};
  var SELECTOR = "a.forgot-password";
  var HELP_TARGET = {json.dumps(help_target)};
  var HELP_SELECTOR = {json.dumps(HELP_SELECTOR)};
  function point(el, url) {{
    el.setAttribute("href", url);
    el.removeAttribute("target");
    el.removeAttribute("rel");
  }}
  function retarget() {{
    var links = document.querySelectorAll(SELECTOR);
    for (var i = 0; i < links.length; i++) {{
      if (links[i].getAttribute("href") !== TARGET) point(links[i], TARGET);
    }}
    var help = document.querySelectorAll(HELP_SELECTOR);
    for (var j = 0; j < help.length; j++) {{
      if (HELP_TARGET) point(help[j], HELP_TARGET);
      else help[j].remove();
    }}
  }}
  document.addEventListener("click", function (e) {{
    var t = e.target;
    if (!t || !t.closest) return;
    if (t.closest(SELECTOR)) {{
      e.preventDefault();
      window.location.assign(TARGET);
      return;
    }}
    var h = t.closest(HELP_SELECTOR);
    if (!h) return;
    e.preventDefault();
    e.stopPropagation();
    if (HELP_TARGET) window.location.assign(HELP_TARGET);
    else h.remove();
  }}, true);
  new MutationObserver(retarget).observe(document.documentElement,
    {{ childList: true, subtree: true }});
  retarget();
}})();
</script>"""


FORGOT_PASSWORD_SCRIPT = build_login_page_script()


def inject_forgot_password_script(html: str, help_url: str | None = None) -> str:
    """Return ``html`` with the login-page script before ``</body>``.

    Idempotent. Without a ``</body>`` the script is appended — browsers run a
    trailing script all the same. ``help_url``: see
    :func:`build_login_page_script`.
    """
    if MARKER in html:
        return html
    script = (
        FORGOT_PASSWORD_SCRIPT if help_url is None else build_login_page_script(help_url)
    )
    idx = html.rfind("</body>")
    if idx == -1:
        return html + script
    return html[:idx] + script + "\n" + html[idx:]


def _find_authorize_route(hass: HomeAssistant):
    http = getattr(hass, "http", None)
    if http is None:
        return None
    for route in http.app.router.routes():
        resource = route.resource
        if (
            route.method == "GET"
            and resource is not None
            and resource.canonical == AUTHORIZE_PATH
        ):
            return route
    return None


def _authorize_html_path(handler: Callable[..., Awaitable]) -> Path | None:
    """The file behind Core's ``partial(_serve_file, path)`` handler, or None."""
    if not isinstance(handler, functools.partial) or not handler.args:
        return None
    path = handler.args[0]
    if not isinstance(path, str) or not path.endswith("authorize.html"):
        return None
    return Path(path)


async def async_patch_authorize_page(hass: HomeAssistant) -> bool:
    """Wrap the ``/auth/authorize`` route. Returns True if the patch is live.

    Safe to call after the server has started: the router is frozen then, but
    the route object is not — its handler is read on every request.
    """
    route = _find_authorize_route(hass)
    if route is None:
        _LOGGER.warning(
            "greenautarky_site: %s is not registered — the login page's "
            "'Forgot password?' link still points at home-assistant.io, not at %s",
            AUTHORIZE_PATH,
            RESET_PAGE_URL,
        )
        return False

    original = route.handler
    if getattr(original, _PATCHED_ATTR, False):
        return True

    html_path = _authorize_html_path(original)
    if html_path is None:
        _LOGGER.warning(
            "greenautarky_site: %s is served by %r, not by Core's file handler "
            "this patch knows — the 'Forgot password?' link is left pointing at "
            "home-assistant.io",
            AUTHORIZE_PATH,
            original,
        )
        return False

    try:
        stock_html = await hass.async_add_executor_job(
            html_path.read_text, "utf-8"
        )
    except OSError as err:
        _LOGGER.warning(
            "greenautarky_site: could not read %s (%s) — the 'Forgot password?' "
            "link is left pointing at home-assistant.io",
            html_path,
            err,
        )
        return False

    patched_html = inject_forgot_password_script(stock_html)

    async def handler(request: web.Request) -> web.StreamResponse:
        if not await hass.async_add_executor_job(_pin_required, hass):
            return await original(request)
        return web.Response(text=patched_html, content_type="text/html")

    setattr(handler, _PATCHED_ATTR, True)
    # aiohttp has no public setter for a route handler; see module docstring.
    route._handler = handler
    _LOGGER.info(
        "greenautarky_site: %s 'Forgot password?' now leads to %s (on devices "
        "with an onboarding PIN)",
        AUTHORIZE_PATH,
        RESET_PAGE_URL,
    )
    return True
