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
    playwright
  ];

  # The buy flow drives a real headed Chromium against a persistent profile;
  # there's nothing to import-check beyond the package itself, and no test
  # suite that can run without a display.
  doCheck = false;

  pythonImportsCheck = [ "roomieorder" ];

  meta = {
    description = "HA button → automatic Amazon order → Google Sheets log";
    mainProgram = "roomieorder";
    license = lib.licenses.mit;
  };
}
