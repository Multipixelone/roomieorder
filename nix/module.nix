{ config, lib, pkgs, ... }:
let
  cfg = config.services.roomieorder;

  # roomieorder must run headed against the logged-in graphical session to
  # reach $DISPLAY / $WAYLAND_DISPLAY (PLAN §4). A *system* service can't see
  # those, so this is a systemd **user** service bound to graphical-session.
  # systemd expands %S → the user's state dir (~/.local/state); StateDirectory=
  # creates and owns roomieorder/ inside it. Specifiers resolve in
  # WorkingDirectory=, so anchor there and use *relative* state paths — that
  # avoids depending on %S being expanded inside Environment= (which is murky).
  stateDir = "%S/roomieorder";

  baseEnv = {
    PLAYWRIGHT_BROWSERS_PATH = "${pkgs.playwright-driver.browsers}";
    PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
    # Drive *real* Google Chrome, not Playwright's bundled Chromium: Akamai
    # reads Chromium's missing proprietary codecs and "Chromium" Sec-CH-UA brand
    # as a bot. Pins the exact binary so Playwright/patchright launch it via
    # executable_path (see purchase._launch_context).
    ROOMIEORDER_CHROME_PATH = lib.getExe cfg.chromePackage;
    # Relative to WorkingDirectory (= stateDir below).
    ROOMIEORDER_DB = "state.sqlite";
    ROOMIEORDER_PROFILE_DIR = "profile";
    ROOMIEORDER_SHOTS_DIR = "shots";
    ROOMIEORDER_CATALOG = cfg.catalogFile;
    DRY_RUN = lib.boolToString cfg.dryRun;
    ROOMIEORDER_WAYLAND = lib.boolToString cfg.wayland;
    OPENCLAW_BIN = "${cfg.openclaw.package}/bin/openclaw";
    OPENCLAW_CHANNEL = cfg.openclaw.channel;
  }
  // lib.optionalAttrs (cfg.openclaw.target != "") { OPENCLAW_TARGET = cfg.openclaw.target; }
  // cfg.extraEnvironment;

  # A terminal `roomieorder` that carries the *same* context the unit does, so
  # `roomieorder dry-run …`, `catalog`, `queue`, `status`, … read the catalog,
  # state.sqlite and profile the service uses — not whatever's under $PWD.
  # Three things to reproduce: (1) baseEnv, (2) cd into the unit's
  # StateDirectory so the *relative* ROOMIEORDER_DB / PROFILE_DIR / SHOTS_DIR
  # resolve to the unit's files, (3) source the secret env file when readable.
  exportBaseEnv = lib.concatStringsSep "\n"
    (lib.mapAttrsToList (k: v: "export ${k}=${lib.escapeShellArg v}") baseEnv);

  cliEnvFile = lib.escapeShellArg (toString cfg.environmentFile);

  wrappedCli = pkgs.writeShellScriptBin "roomieorder" ''
    ${exportBaseEnv}
    # %S for a *user* unit → $XDG_STATE_HOME (default ~/.local/state). Match the
    # unit's WorkingDirectory so the relative state paths above line up.
    state="''${XDG_STATE_HOME:-$HOME/.local/state}/roomieorder"
    mkdir -p "$state"
    cd "$state" || exit 1
    # systemd EnvironmentFile is KEY=value; `set -a` exports each sourced name.
    if [ -r ${cliEnvFile} ]; then
      set -a
      . ${cliEnvFile}
      set +a
    fi
    exec ${cfg.package}/bin/roomieorder "$@"
  '';
in
{
  options.services.roomieorder = {
    enable = lib.mkEnableOption "roomieorder Costco auto-buy service (user session)";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
      description = "The roomieorder package.";
    };

    catalogFile = lib.mkOption {
      type = lib.types.path;
      description = "Path to catalog.json (item_key → item_number/price/cooldown).";
    };

    environmentFile = lib.mkOption {
      type = lib.types.path;
      description = ''
        Path to an env file (e.g. agenix/sops-decrypted) readable by the user.

        Holds the secrets the unit's world-readable Environment= must not:
          GOOGLE_SERVICE_ACCOUNT_JSON  (path to the service-account key)
          ROOMIEORDER_SHEET_ID
          OPENCLAW_TARGET              (if you'd rather keep the chat id secret)

        Costco is NOT a credential here — the login lives in the persistent
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

    chromePackage = lib.mkOption {
      type = lib.types.package;
      default = pkgs.google-chrome;
      defaultText = lib.literalExpression "pkgs.google-chrome";
      description = ''
        The browser the buy flow drives, exposed to it via ROOMIEORDER_CHROME_PATH.

        Defaults to real Google Chrome (unfree — needs nixpkgs.config.allowUnfree
        or an allowUnfreePredicate for "google-chrome"): Akamai fingerprints the
        actual browser build, and Playwright's bundled Chromium fails that check
        (no proprietary H.264/AAC codecs, "Chromium" rather than "Google Chrome"
        in the Sec-CH-UA brand). Override with any Chrome/Chromium-family package
        whose mainProgram is the browser binary if you can't or won't ship Chrome.
      '';
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
      description = "roomieorder — HA button → Costco order → Google Sheets";
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

    # Wrapped CLI on PATH so the operator can run `roomieorder dry-run
    # paper_towels`, `roomieorder resume`, etc. from their shell against the
    # same catalog, env file and state the service uses (see wrappedCli above).
    environment.systemPackages = [ wrappedCli ];
  };
}
