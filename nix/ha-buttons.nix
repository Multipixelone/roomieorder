# Pure function: catalog.json → Home Assistant config fragments.
#
# Single source of truth — you maintain item keys, titles, and (optional)
# button/icon in catalog.json; this derives the HA rest_command, the per-item
# scripts, and a Lovelace button grid from it, so there is no second list to
# keep in sync.
#
# Uses only builtins (no pkgs / nixpkgs lib), so it can be called from a flake's
# top-level `lib` output without threading a system through.
#
# Returns:
#   restCommand   → an attrset for services.home-assistant.config.rest_command
#   scripts       → a LIST of { id; alias; sequence; } — the shape infra's
#                   `iotHass.nixScripts` wants (and trivially mappable to the
#                   upstream `config.script` attrset; see scriptsAttrs)
#   scriptsAttrs  → the same scripts as { "order_<key>" = { ... }; } for
#                   `services.home-assistant.config.script`
#   sensors       → a `rest:` integration list (one fetch, one sensor per item)
#                   that polls `GET /items`, for services.home-assistant.config.rest.
#                   Each item gets sensor.<statusSensorPrefix><key> with an
#                   `on_cooldown` attribute the buttons read to gray out.
#   dashboardCard        → a `type: grid` of plain (core) button cards, no live
#                   state — the HACS-free option.
#   dashboardCardDynamic → a grid of `custom:mushroom-template-card`s (HACS
#                   `mushroom` REQUIRED) that gray out and show when each item
#                   was last ordered inside its `cooldown_days` window. Needs
#                   `sensors` wired into config.rest (above). When catalog items
#                   set a `category`, cards are grouped under a heading per
#                   category. Card count per row is `dashboardColumns`. Holds
#                   EVERY item (owned or not) — unchanged shared-catalog grid.
#   dashboardCardHousehold → same dynamic grid as dashboardCardDynamic but with
#                   owner-tagged (personal) items filtered out — the grid to put
#                   on the SHARED household dashboard so personal items don't
#                   show there.
#   dashboardCardsByOwner → { "<owner>" = <dynamic grid of that owner's items>; }
#                   keyed by the catalog `owner` field. Splice e.g.
#                   `dashboardCardsByOwner."Finn"` into a personal dashboard.
#                   Empty attrset when no item sets an `owner`.
#
# Example:
#   buttons = inputs.roomieorder.lib.haButtons {
#     catalogFile = ./catalog.json;
#     endpoint = "http://192.168.6.6:8723";
#   };

{
  # Path to catalog.json (the same file the app loads).
  catalogFile,
  # Base URL of the roomieorder intake service (no trailing slash, no /reorder).
  endpoint,
  # Logged against every order (no per-roommate attribution by default).
  requester ? "household",
  # rest_command key + the service the scripts call.
  restCommandName ? "roomieorder_reorder",
  # Fallback mdi icon when an item has no `icon`.
  defaultIcon ? "mdi:cart",
  # How often (seconds) the status sensors re-poll `GET /items`.
  pollSeconds ? 30,
  # Entity-id prefix for the per-item status sensors (sensor.<prefix><key>).
  statusSensorPrefix ? "roomieorder_",
  # Cards per row in the generated dashboard grids (the `grid` card `columns`).
  dashboardColumns ? 2,
}:
let
  catalog = builtins.fromJSON (builtins.readFile catalogFile);
  keys = builtins.attrNames catalog;

  nameFor = key: if (catalog.${key}.button or "") != "" then catalog.${key}.button else catalog.${key}.title;
  iconFor = key: if (catalog.${key}.icon or "") != "" then catalog.${key}.icon else defaultIcon;
  categoryFor = key: catalog.${key}.category or "";
  ownerFor = key: catalog.${key}.owner or "";
  statusEntity = key: "sensor.${statusSensorPrefix}${key}";

  # Items in stable display order: by name (the button label), so a category's
  # cards read alphabetically regardless of item_key.
  sortedKeys = builtins.sort (a: b: nameFor a < nameFor b) keys;

  # Keys (in display order) for a given owner, and the unowned/shared remainder.
  # An item's `owner` marks it as one roommate's personal buy: it moves off the
  # shared household grid onto that owner's own grid (dashboardCardsByOwner).
  keysOwnedBy = owner: builtins.filter (key: ownerFor key == owner) sortedKeys;
  unownedKeys = builtins.filter (key: ownerFor key == "") sortedKeys;

  # Ordered, de-duplicated, non-empty owner list (sorted). Drives the per-owner
  # grids; empty when no item sets an owner.
  orderedOwners = builtins.foldl' (
    acc: o: if o == "" || builtins.elem o acc then acc else acc ++ [ o ]
  ) [ ] (builtins.sort (a: b: a < b) (map ownerFor keys));

  scriptFor = key: {
    id = "order_${key}";
    alias = "Order ${catalog.${key}.title}";
    sequence = [
      {
        action = "rest_command.${restCommandName}";
        data = {
          item_key = key;
          requester = requester;
        };
      }
    ];
  };

  buttonFor = key: {
    type = "button";
    name = nameFor key;
    icon = iconFor key;
    tap_action = {
      action = "perform-action";
      perform_action = "script.order_${key}";
    };
  };

  # The per-item status sensor (one `rest:` sensor entry). State is the last
  # *placed* order timestamp; `on_cooldown` is the boolean the dynamic buttons
  # gate on.
  sensorFor = key: {
    name = "${statusSensorPrefix}${key}";
    unique_id = "${statusSensorPrefix}${key}";
    device_class = "timestamp";
    # `/items` is keyed by item_key, so pull this one item out of the payload.
    value_template = "{{ value_json['${key}']['last_placed_at'] }}";
    json_attributes_path = "$['${key}']";
    json_attributes = [
      "title"
      "category"
      "on_cooldown"
      "cooldown_until"
      "cooldown_days"
      "last_placed_at"
    ];
  };

  # One Mushroom card per item (HACS `mushroom`). Outside its cooldown it's a
  # teal tappable button; inside it grays out (`disabled`) and the secondary
  # line names when it was last ordered. A single card — no `conditional` swap.
  mushroomCardFor = key: {
    type = "custom:mushroom-template-card";
    primary = nameFor key;
    icon = iconFor key;
    fill_container = true;
    icon_color = "{% if is_state_attr('${statusEntity key}', 'on_cooldown', true) %}disabled{% else %}teal{% endif %}";
    secondary = "{% set s = states('${statusEntity key}') %}{% if is_state_attr('${statusEntity key}', 'on_cooldown', true) and s not in ['unknown', 'unavailable', ''] %}{{ relative_time(as_datetime(s)) }} ago{% endif %}";
    tap_action = {
      action = "perform-action";
      perform_action = "script.order_${key}";
    };
  };

  # A `grid` card of Mushroom buttons for the given keys.
  mushroomGrid = ks: {
    type = "grid";
    columns = dashboardColumns;
    square = false;
    cards = map mushroomCardFor ks;
  };

  # A markdown heading (portable across view types, unlike the `heading` card
  # which only renders in sections views).
  headingCard = label: {
    type = "markdown";
    content = "## ${label}";
  };

  # A dynamic (cooldown-aware) Mushroom dashboard card for an arbitrary key set.
  # When any key in the set carries a `category`, items are grouped under a
  # markdown heading per category (a `vertical-stack` of heading + grid);
  # otherwise it's a single flat grid. Grouping is computed from *this* key set,
  # so a filtered subset (household / one owner) groups by its own categories.
  dynamicGridFor =
    ks:
    let
      orderedCategories = builtins.foldl' (
        acc: c: if builtins.elem c acc then acc else acc ++ [ c ]
      ) [ ] (builtins.sort (a: b: a < b) (map categoryFor ks));
      hasCategories = builtins.any (c: c != "") orderedCategories;
      keysInCategory = cat: builtins.filter (key: categoryFor key == cat) ks;
    in
    if hasCategories then
      {
        type = "vertical-stack";
        cards = builtins.concatLists (
          map (cat: [
            (headingCard (if cat == "" then "Other" else cat))
            (mushroomGrid (keysInCategory cat))
          ]) orderedCategories
        );
      }
    else
      mushroomGrid ks;
in
{
  restCommand.${restCommandName} = {
    url = "${endpoint}/reorder";
    method = "POST";
    content_type = "application/json";
    # requester is baked in (constant), item_key comes from the calling script.
    payload = ''{"item_key": "{{ item_key }}", "requester": "${requester}"}'';
  };

  scripts = map scriptFor keys;

  scriptsAttrs = builtins.listToAttrs (
    map (key: {
      name = "order_${key}";
      value = removeAttrs (scriptFor key) [ "id" ];
    }) keys
  );

  sensors = [
    {
      resource = "${endpoint}/items";
      method = "GET";
      scan_interval = pollSeconds;
      sensor = map sensorFor keys;
    }
  ];

  dashboardCard = {
    type = "grid";
    columns = dashboardColumns;
    square = false;
    cards = map buttonFor keys;
  };

  # Mushroom grid that grays items out inside their cooldown window — every item
  # (owned or not), so existing consumers see the whole catalog unchanged.
  dashboardCardDynamic = dynamicGridFor sortedKeys;

  # The shared household grid: same dynamic Mushroom card, but with owner-tagged
  # (personal) items filtered out. Put this on the shared dashboard so a roommate
  # can't see or tap someone's personal items.
  dashboardCardHousehold = dynamicGridFor unownedKeys;

  # Per-owner grids: { "<owner>" = <dynamic Mushroom grid of that owner's items>; }.
  # Splice e.g. dashboardCardsByOwner."Finn" into a personal dashboard. Empty
  # attrset when no catalog item sets an `owner`.
  dashboardCardsByOwner = builtins.listToAttrs (
    map (owner: {
      name = owner;
      value = dynamicGridFor (keysOwnedBy owner);
    }) orderedOwners
  );
}
