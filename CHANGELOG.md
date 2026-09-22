## 2.9.4

- **A sub-user invite is a link now.** `POST …/sub_user/invite` returns
  `invite_url` next to the PIN — `…/greenautarky-join?pin=…` — built from Home
  Assistant's external URL, which on a GA device is the resident URL. If HA
  knows no external URL the field is absent and the reason is logged at
  WARNING; a guessed address the recipient cannot reach is worse than six
  digits. The PIN stays in the answer, because a link is useless when it has to
  be read out over the phone.
- **Redirects keep what the caller sent.** This shipped once as the `/` → wizard
  redirect dropping the QR code's `?pin=&device=` (fixed in 2.7.1 — for that
  route). A sweep found the same shape twice more, untouched: `/greenautarky-join`
  and `/greenautarky-setup`. The join one is what stood between an invite LINK
  and six typed digits: the frontend has read `?pin=` from the URL since it was
  written. All three now go through one `redirect_keeping_query`, the route's own
  parameters win over the caller's, and a gate fails the build when a redirect is
  built without it — because a fix applied per site is a fix that comes back.

## 2.9.2 — 2026-09-17

### fix(wizard): the telemetry step records a consent, not two flat booleans

The step wrote `error_logs` / `metrics` as flat keys into
`greenautarky_telemetry`'s preferences dict and saved that. The v2 record's
truth is `tiers.<tier>.value`, which the flat write never touched, and the OS
gate (`ga-telemetry-gate`) reads the tiers. Measured on a bench device on
2026-09-16: the resident said yes to Tier 2, the store said `tier2: false`
next to `metrics: true`. Tier 1 looked right only because its default is `True`.

The step now calls `greenautarky_telemetry.async_set_preferences` (0.2.4). On
a device with an older telemetry component it keeps the flat write and logs a
WARNING saying the tier record is partial — a silent fallback is the same
defect under another name. Tests: three, two red on 2.9.1.

## 2.9.1 — 2026-09-16

### fix(wizard): telemetry, ethernet and complete stand behind the PIN too

The wizard's PIN proves physical access, and the account and GDPR steps refused
to run without it. Three other steps did not ask: `telemetry`, `ethernet` and
`complete`. An unauthenticated, empty `POST /api/greenautarky_site/complete`
answered 200, set `completed: true` and removed the wizard — on a device that
had no resident account yet. From then on nobody could log in, and only an
admin token could reset it.

Found on a bench device on 2026-09-16 by a scripted onboarding that sent empty
payloads by mistake: every gated step answered 403, `complete` answered 200.

All three now call the same `_check_pin_verified` the account step does. A
device without a PIN file keeps working as before. Tests: three red on 2.9.0,
plus one proving the gate still opens and one for the no-PIN-file case.

## 2.9.0 — 2026-09-16

### feat(home-model): a room offers a chosen set of controls, not everything else

The room view offered residents a switch labelled "Smart temperature control" —
English, unexplained, one per thermostat, and not ours: a Sonoff TRV publishes
it, zigbee2mqtt forwards it, Home Assistant makes a switch, and the room listed
it because nothing said not to. It changes how the valve regulates.

Its two siblings on the same device are the argument: `child_lock` and
`open_window` carry `entity_category: config` and were filtered, while this one
carries `None`. Whether a resident saw a vendor knob depended on how the vendor
had labelled it — a race against firmware nobody here controls.

The device's ROLE now decides and each role names what it offers (ADR-0014
Amendment 1): climate → {climate}, light → {light}, switch → {switch}. The role
is read from what the device exposes, never from a model table.

The allow-list governs CONTROLS only: a valve's temperature reading is not a
control and keeps feeding the room's history charts. An entity with no device is
kept — firmware brings devices, a person brings helpers.
## 2.8.0 — 2026-09-16

### feat(rooms-sync): a device placed in a room gets a name a resident can read

Every paired device kept the name zigbee2mqtt gave it — its IEEE address — so
the resident's screen showed `0x0cae5ffff…` wherever a name belonged. The
24-hour humidity chart was worse than ugly: three series from ONE sensor, all
three labelled with the same address, so the legend carried no information.

Placement is the one moment where the name can be derived: the room is known
and the device's entities exist. The kind comes from what the device CAN DO — a
climate entity makes a Thermostat, temperature + humidity a Klimasensor — never
from a model table, which would be wrong the day different hardware is sourced
and wrong silently.

A name a human chose is never touched, a device with a real name from its
integration keeps it, an undeterminable kind stays unnamed rather than guessed,
and numbering is per room so two valves read "Thermostat 1" and "Thermostat 2".

## 2.7.5 — 2026-09-15

### fix(rooms-sync): a re-sync must not collide with the rooms it installed itself

Measured on a canary on 2026-09-15:
`rooms-sync: 3 of 3 room(s) FAILED: Office (ValueError), Schlafzimmer
(ValueError), Wohnzimmer (ValueError)` — on **every** re-sync. Two of four
thermostats therefore never reached a room, and the heating engine had no valve
to work with.

`claimed` is documented and read as "areas taken **during this run**" — the
name-collision guard raises on it, and `_sweep_unclaimed` refuses to delete on
it. It was seeded from `installed`, the ref map of **earlier** syncs. So on a
re-sync every room collided with the area it had installed itself, and said
`the area already holding this name belongs to another room in this sync`,
which was true of none of them.

It bit the flats installed by a pre-2.5.0 build and only those: back then
`_find_area` matched BY NAME, so the ref map was written under the areas'
name-derived ids and no ref alias was ever recorded (`_remember_ref` did not
exist). Ref-only matching cannot find those areas, the handler falls through to
the name lookup, and the guard sees an id that is in the ref map. Worse, the
guard was **self-perpetuating**: it raised before `_remember_ref` could write
the alias down, so the state that triggered it was never repaired and the next
sync failed identically.

* `claimed` now holds only what this run took. The sweep is unaffected by
  construction: it runs only when `first_sync`, i.e. only when the ref map is
  empty — exactly the case where the old seeding contributed nothing.
* The message no longer lies. A genuine collision (two rooms in ONE request
  asking for one name) now names the request and the ref of the room that took
  the name; the adoption path says out loud what it found — which area, that it
  carries no ref alias, and whether an earlier sync had installed it. Area ids
  stay in the log, because for a resident-made room the id is their room name.
* The first re-sync now **heals** the flat: it adopts by name, records the ref
  as an alias, and every sync after that matches on `ref_alias`.

Three new tests, all red against 2.7.4: the real field shape (areas under
name-derived ids with no alias + a pre-populated ref map + separate `ref`/`kind`),
the heal, and the honest message. The pre-2.5.0 `type` test that passed through
all of this is kept — it describes older GACI builds correctly, it was merely
insufficient.

## 2.7.4 — 2026-09-15

### fix(sidebar): the stock-panel sweep is an invariant, not a schedule

Measured on a canary on 2026-09-15, logged in as the resident account the
wizard creates: four of the six panels in `GA_HIDDEN_DEFAULT_PANELS` were gone
and `map` and `todo` were still in the sidebar — the two the sweep had been
*extended for*. Its docstring named `todo` as the reason the second run existed.

Neither startup ordering nor a Core rename explains it. **Both survivors are
registered by Home Assistant's own onboarding completion**, which on a GA device
happens while the resident walks through our wizard, long after the last sweep:
`POST /api/onboarding/core_config` creates a `shopping_list` config entry whose
`todo` platform registers the `todo` panel, and lovelace's onboarding listener
creates the `map` dashboard, whose collection listener registers the `map`
panel. The four that did disappear arrive via `default_config` at boot, which is
why the first run caught them and the defect looked like "two panels are
special".

So the defect class is a one-shot mutation of a registry other integrations keep
writing to; any fixed number of sweeps loses the same race at the next
registration. The component now subscribes to `frontend.EVENT_PANELS_UPDATED` —
the registry's own change signal — and re-establishes the invariant whenever the
registry changes. Edge-triggered on the exact mutation, not a timer polling for
one, and it terminates: a removal fires the event once more and the next pass
finds nothing.

The tests assert the panel registry's CONTENTS at each of the three moments a
panel can appear (before setup, between setup and started, after started), not
that a function was called — the previous design proved the call, and the call
was never the outcome.

### fix(config): `hide_default_panels` is read, after being documented for months

The sweep's comment had promised *"reversible without a redeploy: set
`greenautarky_site: hide_default_panels: false`"* since the day it was written.
Nothing read the key. `CONFIG_SCHEMA` was `cv.empty_config_schema(DOMAIN)`,
which does not raise — it logs *"the greenautarky_site integration does not
support any configuration parameters"* at ERROR and hands the config on. So an
operator who followed the comment got a config error saying the key does not
exist **and** a sweep that ran anyway.

The schema now accepts the documented key (and still accepts the bare
`greenautarky_site:` that ga_manager's converge writes), rejects a typo inside
the block instead of ignoring it, and the option gates the sweep.

### fix(dashboards): the personal board is not a sidebar entry

A resident's sidebar carried a panel named after their own username,
`ga-home-<slug>`. That is not a stock-HA leak: **we** register it, with a
sidebar title and an icon. It is now registered with `sidebar_title=None` /
`sidebar_icon=None`, which is HA's own mechanism — `ha-sidebar.ts`'s
`computePanels` skips any non-default panel with a falsy title, the oldest and
most portable of the three filters there.

The board itself is untouched: the panel stays registered, `/ga-home-<slug>`
still resolves, its config still loads, and the master console still manages it.
`ga-home` (the strategy HA's default Overview renders through) and
`ga-home-<slug>` (a per-user board) share a prefix and nothing else — a test
asserts the Übersicht survives, so a future prefix match cannot quietly take it.

### docs: `docs/RESIDENT-SIDEBAR.md`

The three mechanisms that decide what a resident's sidebar holds, which are
ours, and why the header search control is **not cleanly removable** from a
custom component — HA's only supported flag for it, `hass.kioskMode`, is
client-side only and also removes the sidebar and the menu button. Written down
with the cost of doing it anyway, so the question does not get re-opened from
memory.

## 2.7.3

### fix(wizard): the bundle asks for the paths the component actually serves

A freshly flashed device rendered a **blank page** for the setup wizard: the
vendored bundle requested a retired static mount and a retired API namespace,
every request 404'd, the panel element never upgraded, and onboarding was
impossible.

**The cause is structural, and that is the part worth remembering.** The
component's namespace had never existed in the producer repository — not on any
branch, not in any commit. The bundle that had been correct for months was
correct because its paths were rewritten *after* vendoring. So nothing was
stale: **every rebuild reproduced the defect**, and the day someone rebuilt, the
wizard died. Correcting the bytes again would have held until the next rebuild.

Fixed in the producer instead: every GA URL now derives from one domain
constant, so the two sides cannot drift.

### fix(bundle): stamp a real version before building

The wizard footer's build id and the frontend logger name carried the
`0.0.0.dev0` placeholder. The producer's version resolver reads its own project
metadata and accepts a CalVer; injecting one used to be the retired Core fork
CI's step and left with the fork, so every bundle produced since carried the
placeholder — while an e2e test on the device already asserts the rendered
version is *not* that placeholder. `--regen` now stamps a CalVer into the clone
**before** the build, validates the format, and refuses rather than guesses if
the placeholder it expects is absent. It is never patched into the built bytes:
hand-correcting a vendored artifact downstream is the habit that caused the
defect above.

### ci(bundle): the bundle and the component must agree on where things live

`--check` verified that the committed bytes were unchanged and the file set
intact. Both were true; neither answers whether the bytes are *right*. Two
sources claimed the same truth side by side in this repo and nothing compared
them. It now asserts that every static prefix matches `URL_BASE`, every
`greenautarky*` API namespace matches `DOMAIN`, every namespace token is the
component's own (the panel's i18n namespace is derived from the entry name;
sibling components are an explicit, commented list), and that no dev placeholder
is present. Constants are read from the live source, never restated. If a
constant cannot be read, or the scan inspects zero files or matches zero
references, the gate **fails** — it never skips.

`tests/gates/bundle_paths/selftest.sh` drives the real script over throwaway
fixtures in both directions — 13 cases, 8 must-fail and 5 must-pass — and runs
on every PR. must-pass is not padding: a gate that flags everything is
overridden by reflex, so the stock Home Assistant namespaces, the panel's own
i18n keys, a WebSocket call to a real sibling component and a
consistently-applied future rename are all pinned as cases that must *not* fire.

**A comparison gate catches two sources that disagree. It cannot catch two that
agree because someone keeps correcting one of them** — there, remove the second
source instead.

### fix(rooms-sync): one unmatchable room must not leave the whole flat unplaced

`POST /api/greenautarky_site/rooms/sync` answered **500** for the entire
request whenever a single room could not be created, and it did so *before*
reaching the device placement at the end of the loop — so one room took every
room's devices with it, and nothing downstream that reads an `area_id` (the
heating engine, for one) had anything to read.

**Cause.** Rooms/sync is not the only thing that creates areas; the
device-placement path creates them too, and it has no ref to work with, so its
areas carry `id = slug(name)`. `_find_area` matches **by ref only** — on
purpose, and that decision stands: the name fallback it replaced made a room's
identity depend on whatever text the installer happened to type. The ref
therefore does not match, the handler falls into its `created` branch, and Home
Assistant refuses a second area under a name already in use — by **raising**.
`async_create` and `async_update` both raise it, the raise escaped the per-room
loop, and the whole request died with it.

**Fix, in two halves.**

- **A room fails alone.** The per-room body is wrapped: a failure is counted,
  named and reported *for that room*, and the batch carries on and still places
  every device it can. The response gained `ok` / `failed` and a per-room
  `ok` / `error`, and answers **207** rather than 200 when anything failed — a
  batch that did not fully apply must not read as success. A batch with a
  failure in it also sweeps nothing, because a failed room claims no area and
  the sweep deletes unclaimed empty ones.
- **A name collision is resolved, not thrown.** An area whose name *is* the
  room's name is that room; it simply carries a slug id. Home Assistant fixes
  an `area_id` at creation and offers no way to change it (`async_update` has
  no `id` parameter), so the ref can never become the id — the mapping has to
  be written down instead. It is written to the area's **alias**, using HA's
  own indexed lookup: it lives in `core.area_registry` next to the id, survives
  restarts and a reset of this component's store, and carries no uniqueness
  constraint, so recording it cannot itself raise. `_find_area` gained that one
  extra lookup — still keyed on the **ref**, still never on the name.

The same recording repairs a sibling the code previously only warned about: a
ref that does not survive HA's slugify round-trip (`room__1a4` → id
`room_1a4`) produced a room its own ref could not address, and the next sync
built a duplicate beside it or raised on the name.

Nine tests, each demonstrated failing against the unfixed handler first. They
assert the **outcome** — that devices end up carrying an `area_id` — not the
status code, because a 200 with nothing placed is the exact failure being
fixed here; a mutation that adopts the room, answers 200 and skips the
placement turns five of them red. `_find_area`'s ref-only rule, the resident's
rename signal, the sweep's safety properties and the legacy `type` contract are
all unchanged and still covered.

## 2.7.2

### feat(wizard): dark-mode fix, a "Zurück" step, and real DE/EN localisation

Re-vendors the setup-wizard bundle from greenautarky/frontend with three
fixes from Ahmad's feedback (frontend PR `ga/wizard-feedback-fixes-512`):

- **Dark-mode readability.** The "Betriebsnotwendige Daten" (tier-0) consent
  card used a CSS variable defined nowhere, so its light fallback won in dark
  mode and the heading rendered light-on-light. It now uses
  `--secondary-background-color`, which is themed in both light and dark.
- **A "Zurück" (back) step.** The wizard was forward-only. It now keeps a
  step history and a `ga-setup-back` event, wires the browser Back button to
  the same transition, and adds a back button to gdpr / info_pages /
  analytics / ethernet — while structurally refusing to step back across the
  PIN gate or back into account creation (which would mint a second account).
  Returning to the analytics step restores the consent toggles.
- **Real DE/EN localisation.** Every step now renders through `localize()`
  from bundled German/English tables (the picker previously changed nothing
  visible); the picker is restricted to German/English with German the
  default. GDPR / DSGVO Art. 6 legal lines are **not** machine-translated —
  they still show the reviewed German text pending a lawyer's English sign-off.

Bundle provenance updated in `frontend.lock.yaml` + `frontend_bundle/BUILD-INFO.txt`;
`scripts/build_bundle.sh --check` re-verifies the committed bytes.

## 2.7.1

### fix(wizard): `/` must reach the setup page, and must carry the label's PIN

Two defects on the customer's very first hit. The device label's QR code points
at `/`, so both of them land a customer somewhere other than where the label
promised.

**The redirect could silently never fire.** `IndexView._route` is a
`cached_property` returning `ResourceRoute("GET", self.get, self)`: it binds
`self.get` at FIRST ACCESS and caches the finished route on the instance, and
`resolve()` touches it on every request. Any hit before this component finishes
setting up — a monitoring probe, a fleet poll, a browser, Home Assistant's own
startup traffic — freezes the ORIGINAL handler into the route, and re-assigning
`IndexView.get` on the class afterwards is a no-op: no error, no log line, the
redirect simply never happens. Whether it happens depends on startup timing, so
the defect appears and disappears between boots, which is why testing by hand
calls it fixed. Setup now drops the cached `_route` from the live IndexView
instances after patching, so the next access rebuilds it against the patched
function — correct whether we arrive early (nothing cached) or late (stale
route discarded).

**The redirect threw the scanned PIN away.** The label's QR code encodes
`/?pin=<pin>&device=<id>`, and the setup panel reads both back out of
`window.location` to auto-fill the six digits and name the unit. The redirect
answered with a bare `/greenautarky-setup.html`, so a customer who had just
scanned the code still had to read the PIN off the label and type it in — the
one thing the QR code exists to avoid. This was true on every device where the
redirect fired at all, including ones that looked healthy, because every check
asked only for the status and the path and never whether the parameters
survived. `/` now forwards `pin` and `device`. An allowlist, not a
pass-through: the query string is customer-controlled and lands in a `Location`
header, so only the two parameters the panel actually reads are carried over,
re-encoded rather than pasted.

Tests drive the real `homeassistant.components.frontend.IndexView` — a stub
would test our idea of Core rather than Core — and pin both directions: the
redirect must fire while the wizard is pending and must NOT fire once it is
completed, so a patch that always redirects cannot pass. The forwarding tests
pin the label's parameters through, the bare path when there is no query, and
that a header-injection or open-redirect shaped parameter is dropped.

## 2.7.0

### fix(dashboards): restrict an unassigned personal dashboard to masters (fail closed)

The per-view visibility reconcile now fails closed for a storage dashboard with no
`sub_user_dashboards` assignment. With no assignment the owner is unknown, so on a
managed device (masters configured) the board is visible to masters only — who can
reassign it — and a scoped resident sees a board only when it is explicitly assigned
to them. On an unmanaged device (no masters) visibility is left open, so the device's
sole user keeps access to their own board.

Tests: the reconcile test asserts the unassigned board is masters-only; a new test
pins the unmanaged-device carve-out.

## 2.6.0

### fix(setup): ensure `core.uuid` exists so a fresh device has an identity

Home Assistant creates `core.uuid` lazily — only the first time something calls
`instance_id.async_get()`. Nothing on a GA device ever asked, so a freshly
flashed, fully onboarded device shipped cloud telemetry with an EMPTY instance
id: all three identity sources (`core.uuid`, the HA analytics id, the reported
device id) came back blank, and the device was invisible under its own identity
in the fleet backends.

Setup now calls `instance_id.async_get(hass)` once in `_async_setup_common`,
right after the component decides it is the first load. That is the earliest GA
code that runs on every device, so the id is materialised before anything reads
it. The call is best-effort: if it raises, setup logs a warning and continues —
a missing instance id must never be able to stop the component coming up.

Guarded by two tests: one asserts setup requests the instance id (so `core.uuid`
gets created), one asserts setup still succeeds if the request fails.

## 2.5.0

### feat(rooms): split a room's identity from its type — `ref` + `kind`

`type` was doing two jobs at once: it CLASSIFIED a room and it was used verbatim
as the Home Assistant `area_id`. area_ids are unique, so a flat with two
bedrooms could not be expressed — the second overwrote the first, both rooms'
sensors landed in one area, and the endpoint answered 200 with no warning and no
test covering it.

`ref` now identifies (opaque, stable, becomes the area_id) and `kind`
classifies (a catalogue slug, carried as an HA label that ga_manager reads).
`kind_name` carries a human title only where the device's own derivation would
get it wrong. Backward compatible: `type` still means ref AND kind at once, so
nothing that works today breaks.

Three further changes that come with it, each because the old behaviour tied
identity to something that moves:

* **`_find_area` matches by ref only.** The name fallback made a room's identity
  depend on whatever the installer typed, so a rename could split one room into
  two or merge two into one.
* **The first sync sweeps the areas HA Core seeds by itself.** It runs AFTER
  creating, so the registry is never empty and `site_defaults` cannot re-seed
  into the gap. It is not `replace`: at first sync no resident exists, so
  nothing empty can be theirs. A room holding a device is never swept.
* **A lost ref map re-adopts instead of duplicating**, and a resident's rename
  survives that re-adoption.

Re-measured by the integrator rather than taken on trust: 9 tests fail against
the unchanged source — the ones describing two-rooms-of-one-kind, the label
carrier, the first-sync sweep and the re-adoption — and 181 pass with the
change.

`site_defaults.async_seed_default_areas` was deliberately NOT deleted. It looks
like dead code because Core seeds first on a fresh flash, but the tenant wipe
clears `core.area_registry` while leaving `.storage/onboarding` marked done — so
after a reset Core does not re-seed and the GA seeder is the only one that runs.
Removing it would have reintroduced the documented "a reset device came back
with ZERO rooms" defect.

## 2.4.0

### fix(onboarding): the account step is a dead end that leaks users

Two failures in one handler, both reachable by a dropped connection.

**The orphan.** `async_create_user` ran before `async_add_auth`. When the
username already existed the handler returned 400 and left the user it had just
created standing — credential-less, invisible to the wizard, counted by
nothing. Anything that stops the credential now also removes that user.

**The dead end.** The panel offers no way past the account step, so a resident
whose first attempt half-succeeded could only press the button again, and the
server answered 400 every time. If the username already belongs to someone, the
step now adopts that account and continues.

Measured on a bench device 2026-08-27: five attempts, thirteen users named
"resident", exactly one able to log in, and the device stuck at
`completed: false` with `account` already in `steps_done` — set up and locked
out. Onboarding is a flow a person walks once; it has to survive being walked
twice.

## 2.3.0 — 2026-08-25

### Added
- `POST /api/greenautarky_site/rooms/sync` — GACI pushes the flat's rooms in at
  installation and the component makes them real: it creates the areas and
  places the Zigbee devices in them by `ieee_address` (KB #184, ADR-0008).

  Replaces a GitHub Actions workflow that SSHed into the device as root to do
  the same thing, authenticated by a token compiled into the installer app.

  Merge is the only mode. GACI owns the rooms at installation; the resident owns
  them afterwards, so a `replace` mode would be a way to delete a resident's
  rooms from the cloud. A request asking for one is refused, not downgraded.

  Two details that are load-bearing rather than cosmetic:

  - A new area is created under the ENGLISH catalogue name so Home Assistant
    derives `area_id` from the type, then immediately renamed to the installer's
    name. `area_id` is fixed at creation and survives renames, so the id stays
    catalogue-shaped — which is the only thing the fleet-wide room type is
    derived from — while the resident sees a name in their own language.
  - Matching prefers `area_id == type` over the name. A freshly flashed device
    already carries living_room / kitchen / bedroom (measured on K31 after a
    reflash), so adopting those three is the normal case; matching on name first
    would have produced six rooms in every flat.

  A resident rename is reported as a single boolean. The new name is personal
  data and never leaves the device — the endpoint stores what GACI installed and
  compares, so the cloud can show "Wohnzimmer (renamed)" without ever learning
  what it was renamed to.

  No pseudonym here: `area_ref` is derived with a salt in ga_manager's add-on
  volume, which this container cannot read. This returns the plain `area_id` and
  ga_manager converts it before anything leaves the device.

## 2.2.0 — 2026-08-24
- **feat(home-model): one thermostat per room, derived rather than by hiding
  valves.** The home model now derives a single thermostat per room from the
  radiators in it, instead of presenting valves and hiding the ones that should
  not be touched. Cut together with `ga-heating` 0.2.0 — they are two halves of
  the same feature (room thermostats), and shipping one without the other ships
  it half-built. Released for canary testing, not as a fleet release.

## 2.1.1 — 2026-07-28
- **fix(scoping): registry filtering that actually installs, and actually
  filters.** A scoped sub-user had NO dashboard at all on rc36 — blank page.
  Two defects: the guard patched `connection.send_result`, which
  `ActiveConnection` forbids (`__slots__`), so every filtered command answered
  `unknown_error` and the frontend's registry collections stayed null, taking
  HA's own sidebar down with them; and even installed, the filter matched
  nothing, because registry handlers emit `json_fragment` / pre-serialised
  bytes while `_filter_result` deliberately keeps rows it cannot inspect — so
  the naive fix would have leaked the WHOLE registry to scoped users.
  The original handler now runs against a stand-in connection we own, and its
  output is normalised through HA's own encoder before filtering. Fail-soft:
  anything unexpected yields an empty result instead of raising.
  Verified on K0: a scoped user gets 58 entities / 2 devices / **2 areas** —
  exactly their two assigned rooms — against 176 / 24 / 6 for the master.
- **test:** the suite now asserts the invariant ("exactly the user's own rooms,
  no more AND no less") against a real `ActiveConnection`, and a browser test
  asserts a scoped user's dashboard actually renders. The previous tests drove
  a hand-written stub that permitted the very monkey-patch the real object
  forbids, and would have accepted an empty answer as readily as a correct one.

## 2.1.0 — 2026-07-27
- **feat(household): resident self-service reset — the Danger Zone** (KB #169).
  Two master-only actions, so a household can finally unmake itself instead of
  filing a ticket for an operator-only tenant wipe:
  - `POST /api/greenautarky_site/household/reset` — remove every sub-user of
    the calling master (accounts, persons, refresh tokens, parent map, room
    grants, personal dashboards, consents, that master's open invite PINs).
    Pure Core-side, no restart; the master, rooms, devices and automations
    survive. Recorder history is entity-bound and therefore NOT touched — only
    the site reset can remove it, and the UI copy says so.
  - `POST /api/greenautarky_site/site_reset/request` — file a tenant-wipe
    request for ga_manager (Core cannot stop Core). Returns 202; the addon
    re-validates and owns the WIPE/KEEP manifest.
  - `GET /api/greenautarky_site/site_reset/status` — marker still pending +
    the addon's last verdict.
- **The seam is a marker file** (`/config/ga/reset-request.json`, written
  atomically, nonce + `expires_at`), deliberately not an HTTP call with the
  addon's bearer token: that token has no scopes today, so handing it to Core
  would trade a `/config` read for full device control (OTA, docker exec). The
  marker grants exactly one capability — "ask for a wipe".
- **Fresh PIN gate**: `async_verify_pin_fresh()` re-reads the sticker PIN per
  attempt with its own backoff counters (`site_reset_pin_*`). The existing
  `_check_pin_verified` was NOT reusable — it reads the sticky `pin_verified`
  flag that stays true forever after onboarding, which would have waved any
  master session straight through to a wipe.
- **feat(site): German is the site default, and it is actually applied.**
  Nothing in the GA flow ever set `hass.config.language` — the wizard read a
  `language` field and dropped it on the floor (a literal no-op statement), so
  every device ran on HA's built-in `"en"`. Invisible most of the time, because
  the wizard, the strategy views and the master card are hard-coded German, but
  it decides every SERVER-side translation. New `SITE_DEFAULT_LANGUAGE = "de"`,
  applied in two places: `site_defaults.async_ensure_site_language()` at boot
  while onboarding is incomplete, and the wizard's create-user step (which now
  persists what it is sent, defaulting to German).
- **fix(rooms): a wiped device comes back with the DEFAULT rooms again.** HA
  creates its three default areas in exactly one place — its own onboarding
  step — and the tenant wipe deliberately keeps `.storage/onboarding` marked
  done (so the incoming tenant sees only the GA wizard) while wiping
  `core.area_registry` (room names are tenant data: a tenant can rename any
  room from the Verwalten tab, and "Omas Zimmer" says something about the
  household). Nothing then recreated them, so a reset device had ZERO rooms and
  the room-scoped dashboards had nothing to render.
  `site_defaults.async_seed_default_areas()` recreates Wohnzimmer / Küche /
  Schlafzimmer from HA's own `DEFAULT_AREAS` constant + translations, icons
  included. This is where the language mattered: the wipe removes
  `.storage/core.config` too, so without the default above the rooms would have
  come back as "Living Room".
- **fix(rooms): handle both shapes of HA's `DEFAULT_AREAS`.** 2025.11 (what
  the fleet runs) ships plain strings; 2026.2 (what the dev venv resolves)
  ships `DefaultArea(key, icon)`. Written against the venv only, the seeding
  silently degraded to the hardcoded fallback on every real device — the K31
  bench caught it, the test suite could not. Now normalised, with a
  parametrised test over both shapes.
- Both defaults are applied ONLY while GA onboarding is incomplete — a device
  is in that state exactly twice, fresh from the flasher and just after a
  reset. An operator who switched a live device to another language, or a
  tenant who deleted a room on purpose, is not overruled on the next restart.
- **refactor**: sub-user deletion extracted to `_async_remove_sub_user()` and
  shared by the single-user endpoint and the bulk reset, so the two can never
  drift on what "removed" means.

## 2.0.0 — 2026-07-23
- **feat!(rename): `greenautarky_onboarding` → `greenautarky_site`** (Odoo
  #574). The component is the deployment-SITE management plane (setup wizard +
  master/sub-users + room scoping + home_model) for homes AND offices — "site"
  is the einsatzneutral term. DOMAIN, `/api/greenautarky_site/*`,
  `.storage/greenautarky_site`, static prefix and the OCI artifact
  (`ghcr.io/greenautarky/greenautarky-site`) all renamed. **CLEAN BREAK — no
  alias routes** (canary-ring decision): callers (ga-frontend-bundle,
  ga_manager, fleet-manager `GA_COMPONENTS`, OS bake `version.yaml` + e2e
  suites, wizard frontend source) move in the same rollout wave.
  `/api/ga_remote_login` and the HTML page paths are unchanged.
- **One-time storage migration**: `.storage/greenautarky_onboarding` is MOVED
  to `.storage/greenautarky_site` on first boot (envelope key rewritten, old
  file deleted — one source of truth; a stale copy would hold personal data
  the tenant-wipe could miss). Without it a provisioned device would re-enter
  the wizard and lose its sub-user maps.
- **refactor(structure): the 2243-line `http.py` monolith is gone.** New
  layout: `onboarding/` (wizard, pin, password_reset) · `household/` (masters,
  sub_users, dashboards_admin) · `scoping/` (rooms, entity_scope, leak_guard)
  · `store.py` · `console_login.py` · `consent_views.py`. Pure code moves
  (AST-verified identical bodies); the old http↔rooms import cycle is
  structurally gone (shared accessors live in `store.py`/`household.masters`).
- **docs**: new `docs/ARCHITECTURE.md` (the plane's map, request→scope→model
  flow, seams table) + `docs/API.md` (every endpoint: method, auth, payload —
  AST-extracted from code truth); README layout + SECURITY refs updated; the
  root docstring now describes the site plane.
- Wizard frontend bundle: hardcoded `/api` + static path literals renamed in
  the built JS, SHA256SUMS regenerated (1085 files). The bundle SOURCE
  (frontend fork) needs the same rename before the next rebuild — BUILD.md.

## 1.9.0 — 2026-07-23
- feat(scoping): server-side home model (Odoo #569). New
  `GET /api/greenautarky_onboarding/home_model` computes the READY, already-
  scoped, states-validated dashboard model with the server's full `hass` and
  returns only entities the calling user can actually see — a live state AND
  (for a scoped sub-user) `user.permissions.check_entity(read)`. The ga-home
  strategy (ga-frontend-bundle 1.6.0) now renders straight from it instead of
  re-deriving rooms/entities/scope in the browser from the device+entity
  registries. That client re-derivation crashed for a room-scoped sub-user: the
  leak-guard-filtered registry lists entities absent from the user's scoped
  `hass.states` (e.g. a device `update.*` config entity) → a tile read
  `hass.states[id]` = null → the whole board never rendered (K0, 2026-07-22).
  Nothing null can now reach a card. The endpoint re-runs `async_scope_for` per
  call and never trusts the client. Seam pinned from both ends (test_home_model_*
  here + test_seam_*_read_verbatim in the bundle).

## 1.8.0 — 2026-07-21
- change(privacy): entity scoping is now **default ON** (Odoo #516). It only
  ever restricts a real sub-user (reconcile iterates the sub_users map — empty
  => no-op; admins/owner/master bypass), so arming it by default costs nothing
  on a device without sub-users, and it is the fail-CLOSED choice: a new
  sub-user sees nothing until the master grants rooms rather than the whole
  house until restricted. Only an UNSET flag defaults on — the admin toggle
  still disables it explicitly. Convergence of pre-existing / old devices onto
  this state = Odoo #560 (separate session).

## 1.7.0 — 2026-07-20
- feat(privacy): Stage B increment 3 — close the last leak, REST
  `GET /api/history/period` (Odoo #516). `HistoryPeriodView.get` has no entity
  permission check, so a scoped sub-user could read any entity's history over
  REST even with every websocket path closed. The guard class-patches `get`
  (idempotent) to reduce the REQUIRED `filter_entity_id` to the entities the
  user may read (empty → `[]`), delegating untouched for admins and every
  non-scoped user. Logbook has no REST view; REST states/call_service are
  already Stage-A-enforced. With inc 1–3 the room-scoping boundary is airtight.

## 1.6.1 — 2026-07-20
- fix(console-login): read the HMAC secret off the event loop.
  `GAConsoleLoginView.get()` called `_read_console_secret()` (a synchronous
  `Path.read_text`) directly in the async handler, which HA's
  `homeassistant.util.loop` blocking-call detector flags on every
  `/api/ga_remote_login` hit. It now runs in an executor via
  `hass.async_add_executor_job`; `hass` is resolved once at the top of the
  handler (the duplicate later lookup is removed). No behaviour change — the
  missing-secret path still returns 503, a bad signature still 403.

## 1.6.0 — 2026-07-20
- feat(privacy): Stage B increment 2 — the streaming variants. For a
  room-scoped sub-user, `history/stream` and `logbook/event_stream` requests
  are PRUNED to permitted entities before delegation (deny when nothing
  remains); a whole-home `logbook/event_stream` (no entity/device filter) gets
  the permitted entity list injected, so the logbook panel keeps working —
  scoped to their rooms. Completes the #516 leak surface.

## 1.5.0 — 2026-07-20
- feat(privacy): Stage B leak-guard (Odoo #516). Closes the read paths Core
  does not check against the Stage-A entity policy for a room-scoped sub-user:
  `render_template` is denied; `history/history_during_period`,
  `logbook/get_events` and the entity/device/area registry-list commands have
  their results filtered to permitted entities via
  `user.permissions.check_entity`. Installed at boot alongside Stage A;
  idempotent and a pass-through for admins and every non-scoped user, so it is
  safe with scoping OFF (the default). Streaming variants (`history/stream`,
  `logbook/event_stream`) are increment 2. Design: docs/STAGE-B-LEAK-WRAPPER.md.

# Changelog

## 1.4.0 — 2026-07-20
- feat(rooms): Stage A entity scoping — native per-user room boundary (Odoo #516, PR #17).
  Sub-users with assigned rooms get an HA-native per-entity read/control policy
  (get_states / subscribe_entities / call_service / REST). **Default OFF** — enable via
  admin view `/api/greenautarky_onboarding/entity_scoping`. Known Stage-B gaps
  (history/logbook/template) documented in Odoo #516.
- style: ruff fixes (unused noqa, import sort).

## 1.3.0 — 2026-07-14

### feat(rooms): room-scoped dashboards — the master grants ROOMS, the dashboard is generated

The master no longer hands out dashboards; he grants **rooms**. Each user's dashboard is
generated in the browser on every load, from the rooms he may see — by the `ga-home`
Lovelace strategy (ga-frontend-bundle 1.1.0), which asks the new `my_rooms` endpoint who
the logged-in user is and what he may see.

There is exactly ONE dashboard on the device: HA's default Overview, whose stored config
becomes nothing but `{"strategy": {"type": "custom:ga-home"}}`. That panel is the only
one HA cannot remove or hide, so we own its config instead of fighting it.

No per-user dashboard is stored anymore ⇒ no panel registration, no boot re-registration,
no per-view `visible` reconcile — and none of the orphan-board failure modes that class
of code had (the 1.2.2 fix removes the symptom; this removes the cause).

The scope decision is made server-side and returned WITH its reason:

    no master AND no sub-users -> all    (device was never put into household mode)
    master / admin             -> all
    IS a sub-user              -> rooms  (empty grant = honest empty state)
    tenant without a parent    -> all    (legacy device; it is his house)

Only a real sub-user is ever restricted. Most of the fleet has neither a master flag nor
a single HA area — such a device MUST keep showing its whole house.

⚠️ Presentation scoping, not isolation: HA serves every entity to any authenticated
non-admin over the WebSocket API. Measured on K0: a non-admin `get_states` returns all
212 entities.

New: `GET /api/greenautarky_onboarding/my_rooms`,
`POST /api/greenautarky_onboarding/sub_user/assign_room`.


## 1.2.2 — 2026-07-13

### fix(sub-user): removal deletes the personal dashboard (privacy leak)

Removing a sub-user left their auto-created personal dashboard orphaned —
and because the removal also cleared the matrix entry, the visibility
reconcile STRIPPED the per-view `visible` list, making the removed user's
private board visible to EVERYONE (found live on K0, KB #149 §5a).
`sub_user/remove` now deletes the board entirely: frontend panel, lovelace
config store, and both bookkeeping entries (idempotent branch included).
Regression test asserts every trace is gone.

## 1.2.1 — 2026-07-11

### fix(bundle): join/setup wizard UI repaired — dedicated compilation on the component mount (#512)

The vendored wizard bundle only shipped the entry chunk; every code-split
`import()` (ha-form field renderers etc.) requested `/frontend_latest/…`
from the STOCK Core and 404'd — the account-creation step rendered no
name/password fields, so a customer with an invite PIN could not join via
the browser (caught by the new Playwright e2e tier's #512 regression gate).

- Frontend fork `2d0609e2e`: new `gulp build-ga-wizard` — a DEDICATED
  compilation for the wizard entry whose publicPath is the component's own
  static mount (`/greenautarky_onboarding_static/frontend_{latest,es5}/`).
  Its output dirs contain exactly the wizard's chunk set.
- `build_bundle.sh --regen` vendors the whole compilation dirs (complete by
  construction; no source maps — dev-only, ~20 MB) and the component serves
  them as directory statics on its mount. No collision with the stock
  Core's `/frontend_latest` is possible anymore.
- Legacy per-file registrations kept so a pre-#512 HTML keeps working
  during the transition.
- Bundle produced by the produce-bundle CI workflow (runner-built, not
  locally). A single-chunk variant was tried first and OOM-killed 7–16 GB
  hosts; the dedicated-compilation approach keeps the app build's proven
  memory profile.

## 1.2.0 — 2026-07-11

### feat(sub-user): personal dashboards — auto-created per user (ADR-0006 matrix)

Every tenant user now gets a personal storage dashboard, automatically:

- **create_user (onboarding account step)**: the auto-elected master gets
  `ga-home-<name>` seeded with a welcome view, assigned in the
  `sub_user_dashboards` matrix, per-view `visible` reconciled.
- **sub_user/join**: every joining sub-user gets the same treatment —
  visible to them + the masters only.
- **Boot**: component-owned dashboards are re-registered on
  EVENT_HOMEASSISTANT_STARTED (runtime panels don't survive restarts), and
  masters/sub-users that predate this feature are **backfilled**
  (self-healing; skips admins/inactive users; no-op without masters).
- Dashboards are component-owned (LovelaceStorage + panel registered by us,
  like lovelace treats YAML dashboards) because the running
  `DashboardsCollection` is unreachable from a custom component and a second
  collection instance would clobber user-created dashboards. Trade-off:
  they don't appear in Settings → Dashboards; managed via the master console.
- Best-effort everywhere: dashboard failures never break user creation.

### test: device + e2e tiers

- `tests/device` (`-m device`): invite → join → auto-dashboard → panel
  serves, against a REAL canary via the HA HTTP API (env-gated, self-cleaning).
- `tests/e2e` (`-m e2e`): the same use-case driven through a real browser
  (Playwright) — master console → join page → sub-user sees their board.
- CI runs `-m 'not device and not e2e'`; manual `device-tests` workflow for
  a mesh-attached self-hosted runner. See `tests/device/README.md`.

## 1.0.5 — 2026-07-08

### feat(build): reproducible frontend-bundle producer + #498 copy fixes

The wizard's `frontend_bundle/` was a hand-captured snapshot that had drifted
from the frontend source (the telemetry 3-tier redesign + copy fixes never
reached devices). This release ships the bundle **committed + content-hashed**, decoupled from
the archived frontend fork:

- `frontend_bundle/` now carries a freshly built bundle + `SHA256SUMS` +
  `BUILD-INFO.txt`. The bytes are the source of truth.
- `scripts/build_bundle.sh --check` verifies the committed bytes against
  `SHA256SUMS` **offline** (no fork clone/build). `--hash` recomputes the
  manifest; `--regen` is an optional local rebuild from source.
- `release.yml` + `ci.yml` run `--check` on a fresh checkout, so a
  stale/frozen or tampered bundle fails the build.

- **Real GreenAutarky logo**: the placeholder "GA" circle + the HA favicon (shown on every step header) are replaced by the official CI logo (inline data-URIs); user-facing wordmark is now "GreenAutarky".

The rebuilt bytes include the Odoo #498 onboarding copy pass (consistent
Siezen, real umlauts, grammar/button/link fixes, German "Fertig" instead of
the leaked English "Next") **and** the telemetry 3-tier redesign that had been
stranded in source. See ga-ihost-docs ADR (generic component delivery) + KB #143.
## 1.1.0 — 2026-07-09

### feat(sub-user): Datenschutz consent at join + orphan-disable on master revocation (ADR-0006 open points, best-effort)

Best-effort closure of two ADR-0006 open points, **provisional pending the
privacy review** (implemented so the review can adjust, not so it is
pre-empted):

- **Sub-user consent capture at join.** A sub-user is a separate data subject;
  the join previously asked only invite-PIN + password (+ display name). The
  wizard's join mode now shows a **required Datenschutz checkbox** (link to
  <https://greenautarky.com/datenschutz>; the consent text notes that a profile
  without location data is created — the empty linked Person). Enforced
  **server-side** too: `sub_user/join` rejects without `datenschutz_consent`
  (400), so the UI is never the only gate. The consent is **recorded durably**
  (who = the sub-user id / when / policy version / policy URL) under
  `state["sub_users"][<uid>]["consent"]["datenschutz"]` in the onboarding Store
  (`.storage/greenautarky_onboarding`) — alongside the parent bookkeeping, so
  the review can audit or relocate it. `SUB_USER_CONSENT_VERSION` (const.py)
  triggers re-consent when bumped.
- **Orphaned sub-users on master revocation → DISABLE, never delete.**
  Un-flagging a master via `sub_user/set_master` now sets `is_active=False` on
  that master's sub-users (accounts, Persons and dashboard assignments are
  kept — fully reversible; response reports `disabled_sub_users`). Provisional
  policy: the review still owns the final fate (keep-disabled / reassign /
  delete). **Known gap:** the production revocation path (ga-fleet-manager
  rewriting `/config/ga/ga-master-users.json` directly) does not notify this
  component — that path needs its own reconcile hook (follow-up for the
  ga_manager / fleet-manager stream). Deliberately NOT wired to startup
  flag-file reads: a transient missing/malformed file reads as "no masters"
  (fail-closed) and must not mass-disable a household.
- **Storage decision (best-effort):** matrix/parent/consent state **stays in
  the onboarding Store** (`.storage/greenautarky_onboarding`); the master
  *authorization* flag stays a plain file at `/config/ga/ga-master-users.json`
  (per ADR-0006). Moving the Store to a `/config/ga/` plain file was assessed
  and rejected for now: the Store is written from ~10 code paths and a plain
  file would be writable by every `config:rw` add-on — worse for consent-record
  integrity, not better. Documented for the privacy review to bless or move.

Frontend: the wizard bundle is rebuilt from branch `ga/subuser-join-consent`
(off `ga/onboarding-498-plus-subuser`); `frontend.lock.yaml` ref updated.
5 new tests (consent required server-side / consent recorded / revoke disables
own children only / no-op on never-flagged) — 51 total.

### fix(sub-user): flag read off the event loop (canary finding)

Canary smoke test on K7 (real HA 2025.11.3) surfaced a blocking-call warning:
`_read_master_user_ids` did `path.read_text()` in the event loop. Added
`_async_is_master` + wrapped every in-loop flag read in
`hass.async_add_executor_job`; `_require_master` is now async. The full authed
flow (set_master → invite → join → assign_dashboard → rename_area) verified
end-to-end on-device. **Known gap (not a code fix):** GA OS does not load the
`person` integration, so the join's linked-Person creation is skipped (User +
parent still correct) — pending a design decision (ship `person`, or accept
User-only).

### feat(sub-user): master management plane — prototype (ADR-0006)

Builds on the join foundation. Scoped, master-authenticated, in-process
privileged ops (HA's Lovelace write WS is admin-only, so a Non-Admin master
cannot do these from the browser — the component does):

- `POST .../sub_user/set_master` — **admin-only** add/remove the master flag in
  `/config/ga/ga-master-users.json` (prototype/manual provisioning; production
  writes this via ga_manager).
- `GET .../sub_user/list` — master-gated; returns the master's own sub-users +
  available dashboards + areas.
- `POST .../sub_user/assign_dashboard` — master-gated, parent-enforced; updates
  the `[sub-user × dashboard]` matrix and reconciles native **per-view
  `visible`** (assigned sub-users + masters visible; empty → stripped).
- `POST .../sub_user/rename_area` — master-gated room rename via the area
  registry.
- `GET /greenautarky-master` — prototype Master console page (the production UI
  is a Lovelace custom card in ga-frontend-bundle).

**Entity (sensor) rename is intentionally deferred.** 9 new tests (set_master
admin-gate, master-gated list scoped to own children, dashboard assign +
real `visible` reconcile against a LovelaceStorage, room rename). Not deployed —
privacy-review-gated.

### feat(sub-user): household sub-user join foundation (ADR-0006)

First slice of the Master-User Management Plane. A "Master-User" (a HA
Non-Admin flagged in `/config/ga/ga-master-users.json`, written by ga_manager
— read-only here, fail-closed) can mint **one-time, TTL-bounded invite PINs**.
Sub-users self-register via the **same link**, post-completion, through a new
**repeatable** route (not gated on `completed`, unlike the one-shot device
wizard), entering only **invite-PIN + password + display name**.

On redeem we mirror native HA onboarding: create a **Non-Admin** user
(`GROUP_ID_USER`) **and a linked Person** (empty — no `device_trackers`, so no
location; presence stays opt-in), auto-link the new user to the **issuing
master** (parent map in the onboarding Store), and consume the invite. Bad
invite attempts hit an exponential backoff; a revoked master invalidates
pending invites.

New endpoints: `POST /api/greenautarky_onboarding/sub_user/invite`
(master-only, authenticated), `GET /greenautarky-join` (page),
`POST /api/greenautarky_onboarding/sub_user/join` (invite-gated). Dashboard
assignment + the scoped management ops are a later increment (see ADR-0006).

Not deployed — design is privacy-review-gated before any device rollout.
## 1.0.4 — 2026-06-24

### feat(led): customer LED on/off endpoint (`GALedConfigView`)

New `GET`/`POST /api/greenautarky_onboarding/led` endpoint that reads and
persists a `led_disabled` preference into the onboarding HA Store
(`.storage/greenautarky_onboarding`). ga_manager's status-LED driver
(ga_manager 0.53.0) reads this flag and sets the iHost ring to `Off` when
the customer turns the status LED off; otherwise it drives the ring to
reflect device state (starting/connected/error). Settable any time
post-install (no onboarding-completion guard). The GACI app POSTs here
from its LED toggle.

## 1.0.3 — 2026-06-09

### Security — onboarding PIN moved to /config/.storage/

The physical-access PIN that gates wizard sign-up + password recovery
was stored at `/config/ga-onboarding-pin` in v1.0.0..1.0.2. That path
is readable by any Home Assistant addon that declares
`map: [config:rw]` in its `config.yaml` — a real exfil risk, and the
same threat-model issue that drove the v1.0.1 console-login secret move.

This release:

- Moves the PIN file from `/config/ga-onboarding-pin` to
  `/config/.storage/greenautarky_secrets/onboarding_pin` (= same
  directory used since v1.0.1 for the console-login HMAC secret).
- Adds an idempotent migration that runs once at integration setup
  (`_migrate_legacy_pin`), copies the legacy file over, removes it,
  and `chmod`s the new file 0600.
- If both files exist (= operator wrote the new one manually after a
  PIN rotation), the legacy file is removed and the new one preserved.
- Best-effort: a permission failure logs a warning, does NOT block
  integration setup. The `pin_required` view will reflect "no PIN" if
  the file ends up unreadable — operator can fix permissions.

The `.storage/` location is convention-private to HA Core. Addons that
respect HA's convention (= the overwhelming majority) won't see it
even with `[config:rw]`. Technically the only filesystem barrier
remains the same as before — but the convention drops the risk to
"deliberately misbehaving addon" instead of "any addon with config
access."

### Companion changes outside this repo
- `ha-operating-system` `tests/ga_tests/e2e_user_flows/test.sh` ships
  in the same Buildroot OS image that pins this release; its PIN_FILE
  path moved to the new location in the same commit.
- `ha-operating-system` `version.yaml` pins this release for the next
  OS build (BOSv1.2.8).

## 1.0.2 — 2026-06-09

### Fixed — missing Store migration handler crashed setup
Setup of `greenautarky_onboarding` crashed with `NotImplementedError`
out of `homeassistant.helpers.storage._async_migrate_func` whenever a
v1 storage entry existed on disk. The integration shipped
`STORAGE_VERSION=2` and a `_migrate_v1_to_v2()` helper, but never wired
the helper into the `Store` class — HA's base implementation just raises
when it sees a stale version.

A new `_MigratableStore(Store)` subclass now overrides
`_async_migrate_func` to call `_migrate_v1_to_v2` for any `<2` major
version. `_async_setup_common` constructs that subclass instead of the
bare `Store`.

Caught by the new on-device E2E suite
`tests/ga_tests/e2e_user_flows/test.sh` on K31 BOSv1.2.6 (2026-06-09):
the suite wrote a v1 state file to simulate a fresh-provisioned device,
which exposed the missing migration handler.

## 1.0.1 — 2026-06-08

### Security
- **Move console-login HMAC secret to the HA-Core-only `/config/`** —
  previously stored at `/share/ga/console-login-secret`, which is
  mounted into every customer-installed addon (HACS or otherwise). Any
  addon could have read the secret and minted valid auto-login URLs
  for the device. The new path is
  `/config/.storage/greenautarky_secrets/console_login_secret` (0600,
  inside the HA Core container — not visible to addons).
- **Auto-migration on first boot of 1.0.1+** — `_migrate_legacy_console_secret`
  runs at integration setup, copies the legacy file into the new
  location, chmods it to 0600, and unlinks the legacy file so it can no
  longer be read from `/share/`. Idempotent (no-op if already migrated).
  Failure to migrate logs a warning, does NOT block setup — operators
  see a 503 from the auto-login view until they finish the move.
- Operator follow-up: re-issue the secret via fleet-manager → ga_manager
  converge if devices in the wild may have had it exfiltrated. The new
  location is also where fresh devices receive their seed.

## 1.0.0 — 2026-06-05 (planned)

Initial extraction of `greenautarky_onboarding` from
`ha-operating-system/buildroot-external/rootfs-overlay/...` into its own
repo, paired with the move to a Tier-2 component pattern (see
[ha-operating-system docs / decoupling proposal]).

### Carried over from the rootfs-overlay version
- Wizard flow (PIN → GDPR → Account → Telemetry → Ethernet → Complete).
- `GAPasswordReset*` views — PIN-gated tenant-user password reset
  (admin accounts explicitly protected).
- `GAOnboardingPageView`, `GAAdminBypassView`, status/GDPR/Telemetry/
  Ethernet/Complete/CreateUser/Reset/PinVerify views.
- `GAConsoleLoginView` — signed-token operator auto-login (shipped
  2026-06-05 from `feat/console-login-view`).
- `_hide_default_ha_panels` — drops `energy`/`logbook`/`history`/
  `media-browser`/`todo`/`map` from the sidebar.
- `_patch_index_view_for_wizard_redirect` — server-side `/` →
  `/greenautarky-setup.html` while wizard is incomplete.
- GDPR consent tracking + repair-issue helpers.

### Not yet
- Option D (custom login page + recovery flow) — target `1.1.0`.
- Secret refactor `/share/` → `/config/.storage/` — target `1.0.1`.

### Architectural notes
- Stops shipping the integration through two paths (the legacy
  `greenautarky/ha-core` fork copy is now deprecated; the rootfs-overlay
  copy will be removed once `ha-operating-system` consumes the OCI
  artifact published by this repo's release CI).
- Tests no longer require building the full OS image — `pytest tests/`
  runs in seconds against `pytest-homeassistant-custom-component`
  fixtures.
