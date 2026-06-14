{
  description = "roomieorder — HA button → automatic Amazon order → Google Sheets log";

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

      overlays.default = final: prev: {
        roomieorder = final.callPackage ./nix/package.nix { };
      };
    };
}
