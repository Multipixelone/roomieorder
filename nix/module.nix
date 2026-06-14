{ config, lib, pkgs, ... }:
let
  cfg = config.services.roomieorder;

  # roomieorder must run headed against the logged-in graphical session to
  # reach $DISPLAY / $WAYLAND_DISPLAY (PLAN §4). A *system* service can't see
  # those, so this is a systemd **user** service bound to graphical-session.
  stateDir = "%S/roomieorder"; # systemd expands %S → ~/.local/state under user units

  baseEnv = {
    PLAYWRIGHT_BROWSERS_PATH = "${pkgs.playwright-driver.browsers}";
    PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
    ROOMIEORDER_DB = "${stateDir}/state.sqlite";
    ROOMIEORDER_PROFILE_DIR = "${stateDir}/profile";
    ROOMIEORDER_SHOTS_DIR = "${stateDir}/shots";
    ROOMIEORDER_CATALOG = cfg.catalogFile;
    DRY_RUN = lib.boolToString cfg.dryRun;
    ROOMIEORDER_WAYLAND = lib.boolToString cfg.wayland;
    OPENCLAW_BIN = "${cfg.openclaw.package}/bin/openclaw";
    OPENCLAW_CHANNEL = cfg.openclaw.channel;
  }
  // lib.optionalAttrs (cfg.openclaw.target != "") { OPENCLAW_TARGET = cfg.openclaw.target; }
  // cfg.extraEnvironment;
in
{
  options.services.roomieorder = {
    enable = lib.mkEnableOption "roomieorder Amazon auto-buy service (user session)";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
      description = "The roomieorder package.";
    };

    catalogFile = lib.mkOption {
      type = lib.types.path;
      description = "Path to catalog.json (item_key → ASIN/price/cooldown).";
    };

    environmentFile = lib.mkOption {
      type = lib.types.path;
      description = ''
        Path to an env file (e.g. agenix/sops-decrypted) readable by the user.

        Holds the secrets the unit's world-readable Environment= must not:
          GOOGLE_SERVICE_ACCOUNT_JSON  (path to the service-account key)
          ROOMIEORDER_SHEET_ID
          OPENCLAW_TARGET              (if you'd rather keep the chat id secret)

        Amazon is NOT a credential here — the login lives in the persistent
        Chromium profile under the state dir after a one-time manual sign-in.
      '';
    };

    dryRun = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Stop before the final "Place your order" click. Keep true until you've
        watched every item reach its review page (PLAN §5, §8).
      '';
    };

    wayland = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Pass --ozone-platform=wayland to Chromium.";
    };

    openclaw = {
      package = lib.mkOption {
        type = lib.types.package;
        description = "Package providing bin/openclaw for notifications.";
      };
      target = lib.mkOption {
        type = lib.types.str;
        default = "";
        example = "-987654321";
        description = ''
          Telegram chat id passed to `openclaw message send --target`.
          Rendered into the (user-readable) unit Environment=; if it must stay
          secret, leave empty and set OPENCLAW_TARGET in environmentFile.
        '';
      };
      channel = lib.mkOption {
        type = lib.types.str;
        default = "telegram";
        description = "openclaw channel name.";
      };
    };

    extraEnvironment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = lib.literalExpression ''{ ROOMIEORDER_DAILY_CAP = "150.00"; ROOMIEORDER_PORT = "8723"; }'';
      description = "Extra non-secret env vars merged into the unit (wins over defaults).";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.user.services.roomieorder = {
      description = "roomieorder — HA button → Amazon order → Google Sheets";
      wantedBy = [ "graphical-session.target" ];
      partOf = [ "graphical-session.target" ];
      after = [ "graphical-session.target" ];

      # Inherit the graphical session env (DISPLAY / WAYLAND_DISPLAY); a headed
      # browser can't launch without it.
      environment = baseEnv;

      serviceConfig = {
        Type = "simple";
        ExecStartPre = "${cfg.package}/bin/roomieorder init-db";
        ExecStart = "${cfg.package}/bin/roomieorder serve";
        EnvironmentFile = cfg.environmentFile;
        Restart = "on-failure";
        RestartSec = "10s";
        # %S → ~/.local/state; systemd creates and owns it for the user.
        StateDirectory = "roomieorder";
        StateDirectoryMode = "0700";
        WorkingDirectory = stateDir;
      };
    };

    # CLI on PATH so the operator can run `roomieorder dry-run paper_towels`,
    # `roomieorder resume`, etc. from their shell with the same env file.
    environment.systemPackages = [ cfg.package ];
  };
}
