# Turnkey Home Assistant module: enable it on the HA host and the buttons are
# generated from catalog.json — no hand-maintained script/rest_command lists.
#
# This wires into the UPSTREAM `services.home-assistant.config.{rest_command,script}`
# path. If your HA host splits Nix vs UI scripts via a custom `!include` (as the
# infra repo does with `iotHass.nixScripts`), don't enable this module — call
# `lib.haButtons` directly and feed `.scripts` / `.restCommand` into your own
# options instead (see PLAN-ROOMIE.md §3).

{ config, lib, ... }:
let
  cfg = config.services.roomieorder.homeAssistant;
  buttons = import ./ha-buttons.nix {
    inherit (cfg)
      catalogFile
      endpoint
      requester
      restCommandName
      defaultIcon
      pollSeconds
      statusSensorPrefix
      dashboardColumns
      ;
  };
in
{
  options.services.roomieorder.homeAssistant = {
    enable = lib.mkEnableOption "roomieorder Home Assistant buttons generated from catalog.json";

    catalogFile = lib.mkOption {
      type = lib.types.path;
      description = "The same catalog.json the roomieorder service uses.";
    };

    endpoint = lib.mkOption {
      type = lib.types.str;
      example = "http://192.168.6.6:8723";
      description = "Base URL of the roomieorder intake service (no trailing slash).";
    };

    requester = lib.mkOption {
      type = lib.types.str;
      default = "household";
      description = "Value logged as the order's requester for every button.";
    };

    restCommandName = lib.mkOption {
      type = lib.types.str;
      default = "roomieorder_reorder";
      description = "rest_command key + the service each script calls.";
    };

    defaultIcon = lib.mkOption {
      type = lib.types.str;
      default = "mdi:cart";
      description = "mdi icon used for items whose catalog entry sets no icon.";
    };

    pollSeconds = lib.mkOption {
      type = lib.types.int;
      default = 30;
      description = "How often the status sensors re-poll GET /items.";
    };

    statusSensorPrefix = lib.mkOption {
      type = lib.types.str;
      default = "roomieorder_";
      description = "Entity-id prefix for the per-item status sensors (sensor.<prefix><key>).";
    };

    dashboardColumns = lib.mkOption {
      type = lib.types.int;
      default = 2;
      description = "Cards per row in the generated Reorder grid.";
    };

    dashboard = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        When true, set services.home-assistant.lovelaceConfig to a single
        "Reorder" view holding the generated button grid. The dynamic grid uses
        Mushroom cards, so the HACS `mushroom` frontend must be installed. This
        forces the default dashboard into YAML mode (disables UI editing of it),
        so leave it false if you manage dashboards in the UI and instead drop
        `lib.haButtons {...}.dashboardCard` into a view yourself.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    services.home-assistant.config = {
      rest_command = buttons.restCommand;
      script = buttons.scriptsAttrs;
      # Per-item status sensors so the buttons can gray out inside the cooldown
      # window. `rest` is a top-level list; merges with any other rest sensors.
      rest = buttons.sensors;
    };

    services.home-assistant.lovelaceConfig = lib.mkIf cfg.dashboard {
      title = "Home";
      views = [
        {
          title = "Reorder";
          path = "reorder";
          icon = "mdi:cart";
          cards = [ buttons.dashboardCardDynamic ];
        }
      ];
    };
  };
}
