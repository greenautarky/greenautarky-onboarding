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
script before ``</body>``. The script retargets ``a.forgot-password``; both
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

# Two mechanisms, because the link is rendered by Lit AFTER this script runs
# and is re-rendered whenever the login step changes:
#  - a MutationObserver rewrites href (so hover/long-press shows OUR address)
#    and drops target=_blank (the reset page links back to the login itself);
#  - a capture-phase click handler catches a click on a link the observer has
#    not reached yet.
FORGOT_PASSWORD_SCRIPT = f"""<script {MARKER}>
(function () {{
  var TARGET = "{RESET_PAGE_URL}";
  var SELECTOR = "a.forgot-password";
  function retarget() {{
    var links = document.querySelectorAll(SELECTOR);
    for (var i = 0; i < links.length; i++) {{
      var a = links[i];
      if (a.getAttribute("href") === TARGET) continue;
      a.setAttribute("href", TARGET);
      a.removeAttribute("target");
      a.removeAttribute("rel");
    }}
  }}
  document.addEventListener("click", function (e) {{
    var a = e.target && e.target.closest ? e.target.closest(SELECTOR) : null;
    if (!a) return;
    e.preventDefault();
    window.location.assign(TARGET);
  }}, true);
  new MutationObserver(retarget).observe(document.documentElement,
    {{ childList: true, subtree: true }});
  retarget();
}})();
</script>"""


def inject_forgot_password_script(html: str) -> str:
    """Return ``html`` with the retarget script before ``</body>``.

    Idempotent. Without a ``</body>`` the script is appended — browsers run a
    trailing script all the same.
    """
    if MARKER in html:
        return html
    idx = html.rfind("</body>")
    if idx == -1:
        return html + FORGOT_PASSWORD_SCRIPT
    return html[:idx] + FORGOT_PASSWORD_SCRIPT + "\n" + html[idx:]


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
