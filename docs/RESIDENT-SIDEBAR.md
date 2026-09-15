# What a resident sees in the sidebar — and what we can and cannot remove

*Written 2026-09-15, from a canary measurement and from Home Assistant's own
source. Code truth for the Core/frontend claims: `homeassistant/components/`
(frontend, lovelace, todo, onboarding) and `home-assistant/frontend`
`src/panels/lovelace/hui-root.ts`, `src/components/ha-sidebar.ts`,
`src/state/sidebar-mixin.ts`.*

The GA tenant flow wants a resident's sidebar to hold their home and nothing
else. Three different mechanisms decide what is in there, and only two of them
are ours. This file records which is which, so the next person does not reach
for the wrong lever.

## 1. Stock HA panels — ours, and now an invariant rather than a sweep

`GA_HIDDEN_DEFAULT_PANELS` in `__init__.py` lists the six stock panels the flow
does not use; `_hide_default_ha_panels` removes them.

It used to run twice — at setup, and again on `EVENT_HOMEASSISTANT_STARTED`.
Measured on a canary on 2026-09-15, four of the six were gone and `map` and
`todo` were still there, which is to say: the second run existed *because of*
`todo` and did not catch it.

Neither startup ordering nor a Core rename explains that. Both surviving panels
are registered by **Home Assistant's own onboarding completion**, which on a GA
device happens while the resident walks through our wizard — long after the last
sweep:

| panel | registered by | when |
|---|---|---|
| `energy`, `logbook`, `history`, `media-browser` | `default_config` dependencies | boot, before we set up |
| `todo` | `shopping_list` config entry → `todo` platform → `todo.async_setup` | when `POST /api/onboarding/core_config` finishes HA onboarding (`onboarding/views.py`, `onboard_integrations`) |
| `map` | `lovelace`'s onboarding listener → `_create_map_dashboard` → dashboards collection → `_register_panel` | same moment |

So the defect class is a **one-shot mutation of a registry that other
integrations keep writing to**. Any fixed number of sweeps loses the same race
at the next registration.

The fix keeps the invariant instead of repeating the mutation: subscribe to
`frontend.EVENT_PANELS_UPDATED` — the signal `async_register_built_in_panel`
and `async_remove_panel` both fire — and re-establish the invariant whenever the
registry changes. Edge-triggered on the exact mutation; not a timer polling for
one. It terminates because removing a panel fires the event once more and the
next pass finds nothing to remove.

The switch the code had promised in a comment since day one,
`greenautarky_site: hide_default_panels: false`, is now actually read. Before,
`CONFIG_SCHEMA` was `cv.empty_config_schema(DOMAIN)`, which does not raise — it
logs *"the greenautarky_site integration does not support any configuration
parameters"* at ERROR and hands the config on. An operator who followed the
comment therefore got a config error telling them the key does not exist, plus a
sweep that ran anyway.

**A Core rename is still silent.** `async_remove_panel` cannot tell a renamed
panel from an absent one. What catches that is the e2e assertion on the device
(`ha-operating-system`, `tests/e2e/tests/resident-sidebar.spec.ts`), which asks
the running frontend what a resident's sidebar holds rather than asking this
list what it declares.

## 2. The per-user dashboard — ours, and it was never a leak

`ga-home-<slug>` is a personal Lovelace storage dashboard this component creates
per master/sub-user (ADR-0006) and registers itself. It appeared in the sidebar
because **we put it there**, with a sidebar title and an icon.

It is now registered with `sidebar_title=None` / `sidebar_icon=None`. HA's
frontend drops any non-default panel whose `title` is falsy
(`ha-sidebar.ts`, `computePanels`) — the oldest and most portable of the three
filters there; `show_in_sidebar` and `default_visible` are newer. The panel
stays registered, so `/ga-home-<slug>` still resolves and the board still
renders; it is reached from the master console, which is the surface that
manages these boards anyway.

**Do not write a `ga-home*` pattern into the panel sweep.** Two unrelated things
share that prefix: `ga-home` is the dashboard *strategy* that HA's default
Overview ("Übersicht", `url_path` `None`) renders through — not a panel at all —
and `ga-home-<slug>` is a per-user board. A prefix match reads as if it covered
the Übersicht, and the day someone "tidies" it, it will.

## 3. The search control in the header — not ours, and not cleanly removable

The magnifying glass at the top right is not a panel. It is a toolbar item
inside `hui-root`'s shadow DOM:

```ts
{
  icon: mdiMagnify,
  key: "ui.panel.lovelace.menu.search_home_assistant",
  buttonAction: this._showQuickBar,
  visible: !this._editMode && !this.hass.kioskMode,
  overflow: this.narrow,
}
```

There is exactly one supported flag that hides it — `hass.kioskMode` — and it is
**client-side only**: it is set by a `hass-kiosk-mode` window event or by the
companion app (`src/state/sidebar-mixin.ts`, `src/external_app/`). No
server-side, per-user or per-dashboard setting exists, so a custom component
cannot set it. And kiosk mode is not a targeted fix: it also removes the sidebar
and the menu button entirely.

**Verdict: not cleanly possible. We are not doing it.** What it would cost if we
did:

- a JS module on the existing `frontend.add_extra_js_url` channel that walks
  shadow roots to find the toolbar and deletes or hides one button;
- a `MutationObserver`, because `hui-root` re-renders the toolbar (edit mode,
  narrow/wide, dashboard switch) and would put it straight back;
- a device e2e check pinned to the frontend version, because the thing above
  breaks silently — no error, no log line — whenever HA reorders or renames a
  toolbar entry;
- that maintenance forever, for a cosmetic gain.

If the real worry is not the icon but **what a resident can find through it**,
that is an access question and it already has an answer: the quick bar is built
from the calling user's own `hass.states`, which for a scoped sub-user is
already narrowed by Stage A (native entity permissions) and Stage B (the leak
guard) — see `docs/STAGE-B-LEAK-WRAPPER.md`. A master is deliberately not
scoped. Fixing the icon would change nothing about that; weakening the scoping
would change everything.
