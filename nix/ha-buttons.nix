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
#   dashboardCard        → a `type: grid` of plain button cards (no live state)
#   dashboardCardDynamic → the same grid, but each item grays out and shows when
#                   it was last ordered while inside its `cooldown_days` window.
#                   Needs `sensors` wired into config.rest (above).
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
}:
let
  catalog = builtins.fromJSON (builtins.readFile catalogFile);
  keys = builtins.attrNames catalog;

  nameFor = key: if (catalog.${key}.button or "") != "" then catalog.${key}.button else catalog.${key}.title;
  iconFor = key: if (catalog.${key}.icon or "") != "" then catalog.${key}.icon else defaultIcon;
  statusEntity = key: "sensor.${statusSensorPrefix}${key}";

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
      "on_cooldown"
      "cooldown_until"
      "cooldown_days"
      "last_placed_at"
    ];
  };

  # An item inside its cooldown swaps its button for a grayed-out card naming
  # when it was last ordered. Two `conditional` cards (no HACS): one renders.
  dynCardsFor = key: [
    {
      type = "conditional";
      conditions = [
        {
          condition = "template";
          value_template = "{{ not is_state_attr('${statusEntity key}', 'on_cooldown', true) }}";
        }
      ];
      card = buttonFor key;
    }
    {
      type = "conditional";
      conditions = [
        {
          condition = "template";
          value_template = "{{ is_state_attr('${statusEntity key}', 'on_cooldown', true) }}";
        }
      ];
      card = {
        type = "markdown";
        content = ''
          <ha-icon icon="${iconFor key}"></ha-icon> **${nameFor key}**
          <br>⏳ ordered {{ relative_time(as_datetime(states('${statusEntity key}'))) }} ago
        '';
      };
    }
  ];
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
    columns = 2;
    square = false;
    cards = map buttonFor keys;
  };

  dashboardCardDynamic = {
    type = "grid";
    columns = 2;
    square = false;
    cards = builtins.concatLists (map dynCardsFor keys);
  };
}
