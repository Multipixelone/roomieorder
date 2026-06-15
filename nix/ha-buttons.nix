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
#   dashboardCard → a `type: grid` of button cards, drop into any Lovelace view
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
}:
let
  catalog = builtins.fromJSON (builtins.readFile catalogFile);
  keys = builtins.attrNames catalog;

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
    name = if (catalog.${key}.button or "") != "" then catalog.${key}.button else catalog.${key}.title;
    icon = if (catalog.${key}.icon or "") != "" then catalog.${key}.icon else defaultIcon;
    tap_action = {
      action = "perform-action";
      perform_action = "script.order_${key}";
    };
  };
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

  dashboardCard = {
    type = "grid";
    columns = 2;
    square = false;
    cards = map buttonFor keys;
  };
}
