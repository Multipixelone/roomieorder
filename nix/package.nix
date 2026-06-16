{ lib, python3Packages, ... }:

python3Packages.buildPythonApplication rec {
  pname = "roomieorder";
  version = "0.1.0";
  format = "pyproject";

  src = ./..;

  nativeBuildInputs = with python3Packages; [ hatchling ];

  propagatedBuildInputs = with python3Packages; [
    pydantic
    click
    fastapi
    uvicorn
    httpx
    gspread
    google-auth
    # Two stealth layers, both active in this build:
    #  1. Real Google Chrome via executable_path (ROOMIEORDER_CHROME_PATH, set by
    #     the module) fixes the codec / "Chromium" Sec-CH-UA brand tells.
    #  2. patchright (packaged from its PyPI wheel in ./patchright.nix) closes the
    #     CDP Runtime.enable leak that AutomationControlled can't reach. It's a
    #     drop-in for playwright, auto-preferred at runtime by
    #     purchase._playwright_api; `playwright` stays as the import fallback.
    # patchright's bundled Node driver needs a Nix node — the module sets
    # PLAYWRIGHT_NODEJS_PATH for that.
    playwright
    (python3Packages.callPackage ./patchright.nix { })
  ];

  # The buy flow drives a real headed Chromium against a persistent profile;
  # there's nothing to import-check beyond the package itself, and no test
  # suite that can run without a display.
  doCheck = false;

  pythonImportsCheck = [ "roomieorder" ];

  meta = {
    description = "HA button → automatic Costco order → Google Sheets log";
    mainProgram = "roomieorder";
    license = lib.licenses.mit;
  };
}
