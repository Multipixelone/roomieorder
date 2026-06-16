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
    # Vanilla Playwright. The stealth win that's wired up here is driving real
    # Google Chrome via executable_path (ROOMIEORDER_CHROME_PATH, set by the
    # module) — that alone fixes the codec/Sec-CH-UA brand tells. `patchright`
    # (the `stealth` extra in pyproject) additionally closes the CDP
    # Runtime.enable leak and is auto-preferred at runtime when importable
    # (purchase._playwright_api), but it isn't in nixpkgs — packaging its
    # PyPI-fetched patched driver is a follow-up. Until then the code falls back
    # to this build cleanly.
    playwright
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
