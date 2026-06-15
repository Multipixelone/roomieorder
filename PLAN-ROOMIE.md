# roomieorder — infra integration handoff

A guide for a fresh Claude (or human) to deploy `roomieorder` into the
`Multipixelone/infra` flake. The app itself is built and tested; this is purely
the NixOS/secrets/Home-Assistant wiring. Read this top-to-bottom before editing.

The app repo: `github:Multipixelone/roomieorder` (locally
`~/Documents/Git/roomieorder`). It ships `nixosModules.default`
(`nix/module.nix`) and an `overlays.default`.

---

## 0. Facts that decide the wiring

| Thing | Value | Why it matters |
|---|---|---|
| Runs on host | **`link`** | `link` is the Hyprland desktop (imports `pc`) and already runs `openclaw` + `commutecompass` as user `tunnel`. The buy flow is **headed Chromium**, so it must run in a graphical session — a system service can't reach `$WAYLAND_DISPLAY` (PLAN §4). |
| Service type | **systemd *user* service** | `services.roomieorder` defines `systemd.user.services.roomieorder`, bound to `graphical-session.target`. |
| Session | Hyprland / **Wayland** | set `wayland = true` so Chromium gets `--ozone-platform=wayland`. |
| HA host | **`iot`** | Home Assistant is Nix-managed there (`services.home-assistant.config`). It reaches roomieorder **cross-host** over the LAN. |
| link LAN addr | `192.168.6.6` (wg `10.100.0.1`) | the rest_command URL + the bind address. |
| Notifier | **OpenClaw**, reused | same `openclaw` wrapper `commutecompass.nix` builds. |
| Secrets | **agenix** via `inputs.secrets` (`nix-secrets`) | one env file; optionally a second secret for the Google key. |

This is the same shape as `modules/link/commutecompass.nix` — copy its idioms.

---

## 1. Secrets — `nix-secrets` repo

The module takes a single **`environmentFile`** (an env file) plus a
**`catalogFile`**. The Amazon login is *not* a secret — it lives in the
persistent Chromium profile after a one-time manual sign-in.

### 1a. Create the env-file secret

Add to `nix-secrets/secrets.nix` (mirrors the existing `commutecompass` entry):

```nix
"roomieorder/env.age".publicKeys = users ++ systems;   # or just [ tunnel link ]
```

Then create `nix-secrets/roomieorder/env.age` (`agenix -e roomieorder/env.age`)
containing:

```sh
# Sheets — the service account JSON path is itself a second secret (see 1b).
GOOGLE_SERVICE_ACCOUNT_JSON=/run/agenix/roomieorder-gcp.json
ROOMIEORDER_SHEET_ID=<google-sheet-id>
# OpenClaw delivery target (kept out of /nix/store). Reuse commutecompass's chat
# id if you want the same Telegram chat.
OPENCLAW_TARGET=<telegram-chat-id>
```

`EnvironmentFile=` is applied *after* `Environment=` in the generated unit, so
anything in this file overrides the module's defaults — this is how
`OPENCLAW_TARGET` stays secret while the rest of the OpenClaw config is set
declaratively.

### 1b. Google service-account key (second secret)

The key is a JSON file. Store it as its own agenix secret and point
`GOOGLE_SERVICE_ACCOUNT_JSON` at its decrypted path:

```nix
"roomieorder/gcp.age".publicKeys = users ++ systems;
```

In the infra module (step 2) decrypt it to a stable path:

```nix
age.secrets."roomieorder-gcp" = {
  file = "${inputs.secrets}/roomieorder/gcp.age";
  path = "/run/agenix/roomieorder-gcp.json";   # match the env file above
  owner = "tunnel";
  mode = "0400";
};
```

Sheets logging is **optional** — leave `ROOMIEORDER_SHEET_ID` empty and the app
degrades to a no-op logger. You can ship without 1b and add it later.

### 1c. Catalog

`catalog.json` holds ASINs + price ceilings — not secret. Either commit a real
one into the infra repo and reference it, or keep it in `nix-secrets`
(`roomieorder/catalog.json`, plaintext). The placeholder catalog in the app
repo has **fake ASINs** — it must be replaced with real ones before going live.

---

## 2. infra module — `modules/link/roomieorder.nix`

Create this file. It mirrors `modules/link/commutecompass.nix`.

```nix
{ inputs, ... }:
{
  flake-file.inputs.roomieorder = {
    url = "github:Multipixelone/roomieorder";
    inputs.nixpkgs.follows = "nixpkgs";
    inputs.flake-utils.follows = "flake-utils";
  };

  configurations.nixos.link.module =
    { config, pkgs, ... }:
    let
      # Same openclaw wrapper commutecompass.nix uses: tunnel's npm-global
      # binary with nodejs on PATH for its `#!/usr/bin/env node` shebang.
      openclawPkg = pkgs.writeShellApplication {
        name = "openclaw";
        runtimeInputs = [ pkgs.nodejs ];
        text = ''exec /home/tunnel/.npm-global/bin/openclaw "$@"'';
      };
    in
    {
      imports = [ inputs.roomieorder.nixosModules.default ];

      age.secrets."roomieorder" = {
        file = "${inputs.secrets}/roomieorder/env.age";
        owner = "tunnel";
        mode = "0400";
      };
      age.secrets."roomieorder-gcp" = {
        file = "${inputs.secrets}/roomieorder/gcp.age";
        path = "/run/agenix/roomieorder-gcp.json";
        owner = "tunnel";
        mode = "0400";
      };

      services.roomieorder = {
        enable = true;
        catalogFile = "${inputs.secrets}/roomieorder/catalog.json"; # or a repo path
        environmentFile = config.age.secrets."roomieorder".path;

        # SAFETY: keep true until you've watched every item reach its review
        # page via `roomieorder dry-run <item>` (PLAN §5, §8).
        dryRun = true;
        wayland = true;

        openclaw = {
          package = openclawPkg;
          # Real chat id comes from OPENCLAW_TARGET in env.age (see 1a).
          target = "";   # empty → sourced entirely from the env file
        };

        extraEnvironment = {
          # Bind to link's LAN address so HA on `iot` can reach it (default is
          # 127.0.0.1, which is unreachable cross-host).
          ROOMIEORDER_HOST = "192.168.6.6";
          ROOMIEORDER_PORT = "8723";
          ROOMIEORDER_DAILY_CAP = "150.00";
        };
      };

      # Open the intake port to the iot host only.
      networking.firewall.extraInputRules = ''
        ip saddr <iot-lan-ip> tcp dport 8723 accept
      '';
      # (or: networking.firewall.allowedTCPPorts = [ 8723 ]; if your LAN is trusted)
    };
}
```

Then add `./roomieorder.nix` to `modules/link/imports.nix` (wherever
`commutecompass.nix` is imported), and run `nix run .#write-flake` (or however
infra regenerates `flake.nix` from `flake-file.inputs`) so the new input lands
in `flake.nix` + `flake.lock`.

### Gotchas

- **Graphical-session env.** The user service must inherit
  `$WAYLAND_DISPLAY`/`$DISPLAY`. Under Hyprland-via-uwsm `graphical-session.target`
  already has them. If link doesn't use uwsm, ensure the compositor runs
  `systemctl --user import-environment WAYLAND_DISPLAY DISPLAY` (or `dbus-update-activation-environment`)
  before the target activates, or the headed browser will fail to launch.
- **agenix + user service.** The secret is decrypted at system activation to
  `/run/agenix/...` owned by `tunnel` (mode 0400). The user service runs as
  `tunnel`, so it can read it — no group juggling like commutecompass needed
  (this isn't a sandboxed system service).
- **Playwright pin.** The module sets `PLAYWRIGHT_BROWSERS_PATH` to
  `pkgs.playwright-driver.browsers`; the python `playwright` version must match
  that browser build. Both move together on a `nixpkgs` bump — if the buy flow
  breaks right after a flake update, suspect this first.
- **Desktop must be awake.** Queued taps drain on wake; the worker can't drive
  a headed browser while the session is locked/asleep (PLAN §6).

---

## 3. Home Assistant buttons — `iot` host

HA is Nix-managed (`services.home-assistant.config`), so add the buttons
declaratively. The reference YAML is `examples/home-assistant.yaml` in the app
repo; the Nix translation:

```nix
# modules/iot/roomieorder.nix (new), or fold into an existing iot module.
{
  configurations.nixos.iot.module = { ... }: {
    services.home-assistant.config = {
      rest_command.roomieorder_reorder = {
        url = "http://192.168.6.6:8723/reorder";   # link's LAN addr
        method = "POST";
        content_type = "application/json";
        payload = ''{"item_key": "{{ item_key }}"}'';
      };

      script = {
        order_paper_towels.sequence = [{
          action = "rest_command.roomieorder_reorder";
          data.item_key = "paper_towels";
        }];
        order_toilet_paper.sequence = [{
          action = "rest_command.roomieorder_reorder";
          data.item_key = "toilet_paper";
        }];
        order_dish_soap.sequence = [{
          action = "rest_command.roomieorder_reorder";
          data.item_key = "dish_soap";
        }];
      };
    };
  };
}
```

Keep one `script.order_<item>` per catalog `item_key`. Add the `<iot-module>` to
the iot imports and rebuild iot.

### The dashboard buttons

The `rest_command` + `script` above are the functional half. The visible
**buttons** are a Lovelace card. Two ways, pick whichever matches how this repo
manages dashboards:

1. **HA UI (simplest):** Edit dashboard → add a **Grid** of **Button** cards,
   one per script, `tap_action: perform-action → script.order_<item>`. The
   button-grid YAML is at the bottom of `examples/home-assistant.yaml`.
2. **Declarative / MCP:** if dashboards are managed in Nix
   (`services.home-assistant.config` `lovelace` / a dashboards file) add the
   grid card there. A Claude with the **ha-mcp** server can also create the
   scripts and dashboard live via `ha_config_set_script` and
   `ha_config_set_dashboard` — useful to prototype before committing the Nix.

No per-roommate attribution: every tap logs as `household` (the default
`requester`). Decided out of scope.

---

## 4. One-time manual bring-up (after deploy)

Do these on `link`, logged into the graphical session, in order:

1. **Amazon login.** Launch the persistent profile once and sign in by hand:
   ```
   roomieorder dry-run paper_towels    # opens the headed browser on the profile
   ```
   If you're not logged in, log in in that window; the session persists in
   `~/.local/state/roomieorder/profile`. Confirm a default shipping address and
   1-tap / default payment are set on the account.
2. **Verify each item reaches the review page** (still `DRY_RUN=true`):
   `roomieorder dry-run <item>` for every catalog key — it stops at the review
   page and screenshots to `~/.local/state/roomieorder/shots/`.
3. **Sheets + notify smoke test:** `roomieorder test-notify` should ping
   Telegram; a placed/dry row should appear in the sheet once `ROOMIEORDER_SHEET_ID`
   is set and the sheet is shared with the service-account email (editor).
4. **Go live for one cheap item:** flip `dryRun = false` in the module (or set
   `DRY_RUN=false` in env.age), rebuild, tap that item's button once, confirm a
   real order + a Sheets row + the Telegram confirmation.
5. If anything trips a challenge/failure the worker **pauses** — clear it, then
   `roomieorder resume`.

CLI reference (all on `link`, env from the same file): `serve`, `init-db`,
`catalog`, `queue`, `status`, `pause`, `resume`, `dry-run <item>`,
`test-notify`. The module also puts `roomieorder` on the system PATH.

---

## 5. Checklist

- [ ] `nix-secrets`: `roomieorder/env.age` (+ `secrets.nix` entry); optional `gcp.age`, `catalog.json`
- [ ] `infra`: `modules/link/roomieorder.nix` + import + regenerate flake input
- [ ] `infra`: firewall opens 8723 from iot to link
- [ ] `infra`: `modules/iot/…` rest_command + scripts; dashboard buttons
- [ ] Deploy link + iot
- [ ] Manual bring-up §4 (Amazon login, dry-run each item, one real buy)
- [ ] Replace placeholder ASINs in the catalog with real ones
