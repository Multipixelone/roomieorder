# Dashboard — Home Assistant "Reorder" view

How the household ordering UI is wired in Home Assistant, and how to tweak or
redesign it. This documents the **live dashboard** (HA storage mode), which is
what you actually look at — not the YAML reference in `examples/`.

## Where it lives

- **HA instance:** `http://127.0.0.1:8123` (version 2026.6.x at time of writing)
- **Dashboard:** `main-home` (sidebar title "Main Home"), storage mode — i.e.
  editable in the UI (three-dot menu → Edit dashboard), not a YAML file.
- **View:** `Reorder`, path `/main-home/reorder` (it's `views[4]` in the raw
  config). Reached from the home view via the teal **Reorder** chip.

The view is a single full-width **section** containing a back chip, a heading,
and one card per orderable item.

## The item cards

Each staple is **one** `custom:mushroom-template-card`. Mushroom is installed
(HACS) and used across the whole dashboard, so these are guaranteed to render.
Shape:

```yaml
type: custom:mushroom-template-card
primary: Dish Soap
icon: mdi:bottle-tonic
# grey when on cooldown, teal when orderable
icon_color: >-
  {% if is_state_attr('sensor.roomieorder_dish_soap','on_cooldown',true)
  %}disabled{% else %}teal{% endif %}
# shows "3 days ago" only while on cooldown
secondary: >-
  {% set s = states('sensor.roomieorder_dish_soap') %}
  {% if is_state_attr('sensor.roomieorder_dish_soap','on_cooldown',true)
  and s not in ['unknown','unavailable',''] %}{{ relative_time(as_datetime(s)) }} ago{% endif %}
fill_container: true
tap_action: { action: perform-action, perform_action: script.order_dish_soap }
grid_options: { columns: 2, rows: 2 }   # columns: 12-col subgrid / 2 = 6 per row; rows: height
```

### The naming contract (don't break this)

For an item with key `dish_soap`, three things must line up:

| Thing                | Value                              | Created by            |
|----------------------|------------------------------------|-----------------------|
| Status sensor        | `sensor.roomieorder_dish_soap`     | `rest:` poll of `/items` |
| Order script         | `script.order_dish_soap`           | per-item HA script    |
| Card `tap_action`    | `perform-action` → `script.order_dish_soap` | this view    |

The card only ever fires the script; price/ASIN/cooldown all live server-side in
`catalog.json`, so a mis-typed card can't order the wrong thing or skip a guard.

## Layout: full-width 6-across grid

- The **section** has `column_span: 4` → it spans all 4 of the view's
  `max_columns`, i.e. the full page. (A section with no `column_span` is capped
  to ~one column ≈ half the page — that was the old "only takes up half a
  column" problem.)
- Each card gets `grid_options: { columns: 2 }`. HA sections lay cards on a
  12-column subgrid, so `12 / 2 = 6` cards per row.

To change the column count, change every card's `grid_options.columns`:

| Cards per row | `grid_options.columns` |
|---------------|------------------------|
| **4**         | **3** (current)        |
| 6             | 2                      |
| 12            | 1                      |

The back chip and heading have no `grid_options`, so they default to full width
and sit above the grid.

## How to edit

**Via the HA UI:** open `/main-home/reorder`, Edit dashboard, click a card. To
re-flow the grid, edit each card's layout (the resize handle) — or just edit the
view in YAML (pencil → Edit in YAML).

**Programmatically (MCP / API):** the dashboard is storage mode, so use
`ha_config_set_dashboard` with a `python_transform`. Pattern used to build this
view — clear the section and rebuild from a list:

```python
sec = config["views"][4]["sections"][0]
sec["column_span"] = 4
sec["cards"] = [ <back chip>, <heading> ]
for key, name, icon in items:
    ent = "sensor.roomieorder_" + key
    sec["cards"].append({
        "type": "custom:mushroom-template-card",
        "primary": name, "icon": icon,
        "icon_color": "{% if is_state_attr('" + ent + "','on_cooldown',true) %}disabled{% else %}teal{% endif %}",
        "secondary": "...{{ relative_time(as_datetime(s)) }} ago...",
        "tap_action": {"action": "perform-action", "perform_action": "script.order_" + key},
        "fill_container": True,
        "grid_options": {"columns": 2, "rows": 1},
    })
config["views"][4]["sections"] = [sec]
```

Notes on the `python_transform` sandbox: no `import`, no f-strings/`.format()`,
no `.replace()`. Build template strings with plain `+` concatenation (as above).
Always pass the current `config_hash` from `ha_config_get_dashboard` for
optimistic locking; it changes after every write.

## Adding / removing an item

1. Server side: add the item to `catalog.json` and make sure HA gets a
   `script.order_<key>` and a `sensor.roomieorder_<key>` (the Nix generator
   below emits both from the catalog).
2. Dashboard: add one `mushroom-template-card` to the Reorder section following
   the shape above (`primary`, `icon`, the two templates keyed to the sensor,
   `tap_action` → the script).

## Gotchas (why it's built this way)

- **Don't use `conditional` cards with `condition: template` here.** The old
  version of this view used a *pair* of `conditional` cards per item (button +
  "ordered N ago" markdown). HA's card editor flags template conditions as
  **"Conditions are invalid"** and the cards render as red error blocks, and 48
  stacked conditionals in a 1-column grid is what squeezed everything into a
  narrow strip. The single `mushroom-template-card` does the same gray-out +
  timestamp in one card with no conditions.
- **`perform-action`, not `call-service`.** HA 2024.8+ renamed the action; the
  key is `perform_action` (underscore) inside `tap_action`.
- **Cooldown is enforced server-side**, not by the dashboard. The gray-out is
  purely visual — tapping a greyed item still calls the script, but the
  roomieorder service rejects it (HTTP 200, no double order). So the worst case
  of a stale `on_cooldown` attribute is a harmless tap.
- **`unknown` sensor state** = never ordered (no `last_placed_at`). The
  `secondary` template guards for `unknown`/`unavailable`/`''` so
  `as_datetime()` never errors.
- **Bad MDI icon name = blank card / "missing render."** An icon that doesn't
  exist in Material Design Icons renders as nothing (e.g. `mdi:food-variant-outline`,
  `mdi:scrub-brush`, `mdi:box-tissue` are *not* real icons). Verify a name before
  using it — quickest check is whether the SVG exists:
  `curl -so /dev/null -w "%{http_code}" https://raw.githubusercontent.com/Templarian/MaterialDesign-SVG/master/svg/<name>.svg`
  (`200` = valid, `404` = doesn't exist), or search at pictogrammers.com.
- **Card height** is the `grid_options.rows` value (currently `2`). Bump it if
  the name + "ordered N ago" line gets cramped; `columns` controls width/count.

## Upstream: catalog + Nix generator

The items, icons, scripts and sensors are meant to come from a single source of
truth — `catalog.json` — fed through `lib.haButtons` (`nix/ha-buttons.nix`),
which emits the `rest_command`, per-item `script.order_<key>`, the
`sensor.roomieorder_<key>` poll, and a dashboard card. See `examples/home-assistant.yaml`
for the reference shape and `nix/ha-buttons.nix` for the generator.

The generator's `dashboardCardDynamic` now emits the **same Mushroom pattern**
documented here — one `custom:mushroom-template-card` per item, gray-out via
`icon_color`, "ordered N ago" via `secondary` — so regenerating from Nix
produces this layout rather than fighting it. Two knobs map to what's described
above: `dashboardColumns` (the grid's cards-per-row, default `2`) and a per-item
`category` field in `catalog.json`, which groups cards under a `## <category>`
heading. (The stateless `dashboardCard` output stays core-only for HACS-free
setups.) The live dashboard currently carries more items (25) than the repo's
sample `catalog.json` (3); its catalog is maintained wherever the deployed
instance is configured.
