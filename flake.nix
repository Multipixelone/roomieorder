{
  description = "roomieorder — HA button → automatic Costco order → Google Sheets log";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    git-hooks.url = "github:cachix/git-hooks.nix";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      git-hooks,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        roomieorder = pkgs.callPackage ./nix/package.nix { };
        pythonEnv = pkgs.python313.withPackages (
          ps: with ps; [
            pip
            pytest
            pydantic
            click
            fastapi
            uvicorn
            httpx
            gspread
            google-auth
            playwright
            mypy
          ]
        );

        # Playwright's own downloaded browsers won't run on NixOS (dynamic-link
        # mismatch). Point both the dev shell and the runtime at the nixpkgs
        # build, and skip the host-requirements check. The python `playwright`
        # version must match this browser build — both come from the same
        # nixpkgs pin, so a `nixpkgs` bump moves them together.
        playwrightEnv = {
          PLAYWRIGHT_BROWSERS_PATH = "${pkgs.playwright-driver.browsers}";
          PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
        };

        pre-commit-check = git-hooks.lib.${system}.run {
          src = ./.;
          hooks = {
            ruff.enable = true;
            mypy = {
              enable = true;
              settings.binPath = "${pythonEnv}/bin/mypy";
              # mypy must analyse all modules in one pass. With the default
              # require_serial = false, pre-commit partitions the file list
              # across parallel mypy processes; a partial file set both
              # mis-resolves imports and (mypy 1.20.1) crashes with an
              # INTERNAL ERROR. Serial = one invocation over every file.
              require_serial = true;
            };
            pytest = {
              enable = true;
              name = "pytest";
              entry = "${pythonEnv}/bin/pytest -q";
              language = "system";
              pass_filenames = false;
              types = [ "python" ];
            };
          };
        };
      in
      {
        packages.default = roomieorder;
        packages.roomieorder = roomieorder;

        checks = {
          inherit pre-commit-check;

          # Guard the catalog → HA generator: one script + one button per item,
          # rest_command present, every script id well-formed. Runs in
          # `nix flake check` (CI), so a catalog or generator change that breaks
          # the shape fails the build.
          ha-buttons =
            let
              b = import ./nix/ha-buttons.nix {
                catalogFile = ./catalog.json;
                endpoint = "http://example:8723";
              };
              n = builtins.length (builtins.attrNames (builtins.fromJSON (builtins.readFile ./catalog.json)));
              # Second instance over examples/catalog.json, which carries an
              # owner-tagged item (finn_protein_bars, owner "Finn") so the
              # household/per-owner partition is exercised in CI.
              b2 = import ./nix/ha-buttons.nix {
                catalogFile = ./examples/catalog.json;
                endpoint = "http://example:8723";
              };
              n2 = builtins.length (
                builtins.attrNames (builtins.fromJSON (builtins.readFile ./examples/catalog.json))
              );
              ok =
                (builtins.length b.scripts == n)
                && (b.restCommand ? roomieorder_reorder)
                && (builtins.length b.dashboardCard.cards == n)
                && (builtins.all (s: builtins.substring 0 6 s.id == "order_") b.scripts)
                # Repo catalog has no owners → no per-owner grids, household == all.
                && (b ? dashboardCardHousehold)
                && (b.dashboardCardsByOwner == { })
                # Example catalog: Finn's one item lands on his grid only, and the
                # household grid holds every other (all-but-one) item.
                && (b2.dashboardCardsByOwner ? Finn)
                && (builtins.length b2.dashboardCardsByOwner.Finn.cards == 1)
                && (builtins.length b2.dashboardCardHousehold.cards == n2 - 1);
            in
            if ok then
              pkgs.runCommand "ha-buttons-check" { } "touch $out"
            else
              throw "ha-buttons: generated scripts/cards don't match catalog.json";
        };

        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            roomieorder
            pythonEnv
            ruff
            mypy
            pre-commit
          ] ++ pre-commit-check.enabledPackages;
          inherit (playwrightEnv) PLAYWRIGHT_BROWSERS_PATH PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS;
          shellHook = pre-commit-check.shellHook;
        };
      }
    )
    // {
      nixosModules.default = import ./nix/module.nix;
      nixosModules.roomieorder = import ./nix/module.nix;
      # Turnkey HA buttons generated from catalog.json (upstream config.script
      # path). For custom script plumbing, use lib.haButtons below instead.
      nixosModules.homeAssistant = import ./nix/ha-module.nix;

      # Pure, system-independent: catalog.json → HA config fragments
      # (rest_command, scripts, dashboard card). Single source of truth.
      lib.haButtons = import ./nix/ha-buttons.nix;

      overlays.default = final: prev: {
        roomieorder = final.callPackage ./nix/package.nix { };
      };
    };
}
