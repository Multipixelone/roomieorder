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

> **⚠️ Single source — point both consumers at the *same* file.** The catalog
> is read in two places: the **service** on `link`
> (`services.roomieorder.catalogFile`, §2) and the **HA button generator** on
> `iot` (`lib.haButtons { catalogFile = …; }`, §3). Use one identical path for
> both — `${inputs.secrets}/roomieorder/catalog.json` is the natural choice
> since both hosts already have `inputs.secrets`. If they diverge, the buttons
> and the items the service knows about drift apart (a tapped button 404s, or a
> stocked item has no button). Define the path once (e.g. a `let` binding or a
> small shared module) if you want the compiler to enforce it.

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

## 3. Home Assistant buttons — `iot` host (generated from `catalog.json`)

**Do not hand-write a button list.** The roomieorder flake exposes
`lib.haButtons`, a pure function that turns `catalog.json` into the HA
`rest_command`, the per-item scripts, and a Lovelace button grid. `catalog.json`
is the single source of truth — add a staple there and its button appears; you
never maintain a second list.

`lib.haButtons { catalogFile; endpoint; }` returns:

| field | shape | feed into |
|---|---|---|
| `restCommand` | `{ roomieorder_reorder = {…}; }` | `services.home-assistant.config.rest_command` |
| `scripts` | list of `{ id; alias; sequence; }` | infra's **`iotHass.nixScripts`** |
| `scriptsAttrs` | `{ "order_<key>" = {…}; }` | upstream `config.script` (don't use here — conflicts with `script ui: !include`) |
| `sensors` | a `rest:` list (one fetch, one sensor per item) | `services.home-assistant.config.rest` |
| `dashboardCard` | a `type: grid` of plain buttons | any Lovelace view |
| `dashboardCardDynamic` | same grid, but each item grays out while on cooldown | any Lovelace view (needs `sensors` wired) |

Two optional args control the status side: `pollSeconds` (default `30`, how
often the sensors re-poll `GET /items`) and `statusSensorPrefix` (default
`roomieorder_`, so item `paper_towels` → `sensor.roomieorder_paper_towels`).

**Gray-out behavior.** `sensors` polls the service's `GET /items`, which reports
per-item `last_placed_at`, `cooldown_days`, and an `on_cooldown` flag (true while
the item is inside its catalog `cooldown_days` window of the last *placed* order).
`dashboardCardDynamic` reads `sensor.<prefix><key>`'s `on_cooldown` attribute:
while it's true the button is swapped for a grayed-out card naming when it was
last ordered, so an item that was bought recently can't be re-ordered (the intake
cooldown guard would reject it anyway, §5). It's two stacked `conditional` cards
per item — pure core, no HACS/`custom:button-card`.

> **Only placed orders gray a button.** `on_cooldown` is computed from the last
> `placed` row, exactly like the cooldown guard. In `dryRun` mode orders land as
> `dry_run`, not `placed`, so **nothing grays out until you go live** (§4). Items
> with `cooldown_days: 0` never gray (no cooldown).

This repo's HA host routes Nix scripts through `iotHass.nixScripts` (separate
from UI-managed `scripts.yaml`), so use `.scripts`, not `scriptsAttrs`:

```nix
# modules/iot/roomieorder.nix (new)
{ inputs, ... }:
{
  configurations.nixos.iot.module =
    { ... }:
    let
      buttons = inputs.roomieorder.lib.haButtons {
        catalogFile = "${inputs.secrets}/roomieorder/catalog.json"; # same file the service loads
        endpoint = "http://192.168.6.6:8723";                       # link's LAN addr
        # requester defaults to "household"
      };
    in
    {
      # rest_command + the status sensors are plain config (no include-split).
      services.home-assistant.config = {
        rest_command = buttons.restCommand;
        # Per-item status sensors for the gray-out. `rest` is a top-level list,
        # so this merges with any other `rest:` sensors the host already has.
        rest = buttons.sensors;
      };

      # Scripts go through the repo's Nix-script include, generated per item.
      iotHass.nixScripts = buttons.scripts;
    };
}
```

The firewall rule in §2 already opens 8723 from `iot` to `link` for `/reorder`;
the status sensors hit `GET /items` on that **same** port, so no extra hole.

Add `./roomieorder.nix` to the iot imports and rebuild iot. Because the flake
input is pinned, the buttons only change when you bump the roomieorder input
*and* `catalog.json` differs — deterministic.

### The dashboard buttons

Two grids are on offer. `buttons.dashboardCard` is a plain `type: grid` of one
button per item. `buttons.dashboardCardDynamic` is the same grid but each item
grays out and shows when it was last ordered while inside its cooldown window —
**use this one** for the requested behavior (it needs `buttons.sensors` wired
into `config.rest`, above). Both use the optional `button`/`icon` fields from
`catalog.json`, falling back to the title and `mdi:cart`. Drop one into whichever
Lovelace view you keep:

- **If dashboards are UI-managed** (storage mode): the `conditional`/`markdown`
  cards in `dashboardCardDynamic` are all core types, so paste the rendered YAML
  into the UI's raw-config editor, or have a Claude with the **ha-mcp** server
  push it live via `ha_config_set_dashboard`. (Plain taps still work if you'd
  rather hand-place `dashboardCard`'s buttons → `script.order_<item>`.)
- **If a dashboard is Nix-managed**: splice `buttons.dashboardCardDynamic` into
  that view's `cards`. (The standalone `nixosModules.homeAssistant` can also set
  a whole "Reorder" view via `lovelaceConfig` with `dashboard = true` — it now
  emits the dynamic grid and the sensors automatically — but that forces the
  *default* dashboard to YAML mode, so don't enable it if you edit dashboards in
  the UI.)

> The gray-out is driven entirely by the service's `GET /items`. If the sensors
> read `unavailable`, the `conditional` falls through to the active button (taps
> still work) — a poll failure degrades to the plain grid, it doesn't lock you
> out. Tune freshness with `pollSeconds`; it can't be more current than one poll
> interval, so a 30 s poll means a tapped button may stay live for up to 30 s
> before it grays.

Other HA setups (not this repo) that use the upstream `config.script` path can
skip `lib.haButtons` and just enable the turnkey module:

```nix
imports = [ inputs.roomieorder.nixosModules.homeAssistant ];
services.roomieorder.homeAssistant = {
  enable = true;
  catalogFile = ./catalog.json;
  endpoint = "http://192.168.6.6:8723";
  dashboard = true; # optional: a generated "Reorder" view
};
```

No per-roommate attribution: every tap logs as `household` (the default
`requester`). Decided out of scope.

---

## 4. One-time manual bring-up (after deploy)

Do these on `link`, logged into the graphical session, in order:

1. **Amazon login.** Launch the persistent profile once and sign in by hand:
   ```
   roomieorder login    # opens the headed browser on the profile, waits for you
   ```
   Log in in that window, then press any key in the terminal to save & close;
   the session persists in `~/.local/state/roomieorder/profile`. Confirm a
   default shipping address and 1-tap / default payment are set on the account.
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
- [ ] `infra`: `modules/iot/roomieorder.nix` — `lib.haButtons` → `rest_command` + `rest` (status sensors) + `iotHass.nixScripts` (generated from catalog); splice `dashboardCardDynamic` into a view for the grayed-out buttons
- [ ] Deploy link + iot
- [ ] Manual bring-up §4 (Amazon login, dry-run each item, one real buy)
- [ ] Replace placeholder ASINs in the catalog with real ones
