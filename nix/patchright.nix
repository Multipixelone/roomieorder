# patchright — a drop-in, undetected fork of playwright-python. Not in nixpkgs,
# so we package the published wheel ourselves. roomieorder auto-prefers it at
# runtime (purchase._playwright_api): it closes the CDP `Runtime.enable` leak
# that --disable-blink-features=AutomationControlled can't reach, which Akamai
# uses to unmask automation mid-flow.
#
# patchright is self-contained: it depends only on pyee + greenlet (NOT on
# playwright) and bundles its own patched Node driver in the wheel. That driver
# ships a prebuilt `driver/node` ELF that will not run on NixOS — we never use
# it: the module sets PLAYWRIGHT_NODEJS_PATH to a Nix nodejs, which
# patchright/_impl/_driver.py honours over the bundle. And because the buy flow
# launches *real* Google Chrome via executable_path, we don't fetch patchright's
# Chromium-for-Testing build at all (PLAYWRIGHT_BROWSERS_PATH stays unused).
{ lib
, stdenv
, buildPythonPackage
, fetchPypi
, pyee
, greenlet
}:

let
  version = "1.60.1";

  # patchright publishes per-platform wheels because each bundles a Node binary.
  # The wheel only has to match the *build* platform to install; the bundled
  # node is inert (see header). Hashes from PyPI digests for ${version}.
  wheels = {
    x86_64-linux = {
      platform = "manylinux1_x86_64";
      hash = "sha256-VH57+4ExAjCXicxCkzeA5f33xHJ95Z+yeR5kvRKYp/M=";
    };
    aarch64-linux = {
      platform = "manylinux_2_17_aarch64.manylinux2014_aarch64";
      hash = "sha256-AjlFov0wIZooRyHKNjhb1EB1rntTBx3B2jgDa22+iOw=";
    };
  };

  wheel = wheels.${stdenv.hostPlatform.system}
    or (throw "patchright: no wheel packaged for ${stdenv.hostPlatform.system}");
in
buildPythonPackage {
  pname = "patchright";
  inherit version;
  format = "wheel";

  src = fetchPypi {
    pname = "patchright";
    inherit version;
    format = "wheel";
    dist = "py3";
    python = "py3";
    inherit (wheel) platform hash;
  };

  propagatedBuildInputs = [ pyee greenlet ];

  # The wheel carries a prebuilt foreign `driver/node`; don't let fixup try to
  # patchelf/strip it — it's never executed (PLAYWRIGHT_NODEJS_PATH wins).
  dontStrip = true;
  autoPatchelfIgnoreMissingDeps = true;

  pythonImportsCheck = [ "patchright" "patchright.sync_api" ];

  meta = {
    description = "Undetected drop-in replacement for playwright-python";
    homepage = "https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python";
    license = lib.licenses.asl20;
  };
}
