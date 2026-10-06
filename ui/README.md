# Premium Bot — UI

Desktop UI: **Tauri 2 + Vue 3 + TypeScript + Vuetify 3 + vue-router**.

App shell with the seven screens from the plan (Dashboard, Logs, Profiles,
Proxies, Tasks, Settings, Diagnostics), daemon heartbeat indicator and control
buttons (Start/Pause/Resume/Restart/Kill) over the daemon's control API. The
screens themselves are stubs until their phases land.

## Requirements

- Node.js 20+ and `pnpm`
- Rust toolchain (stable)
- Linux desktop system libraries — see [Linux system dependencies](#linux-system-dependencies)

## Commands

| Command             | What it does                                              |
| ------------------- | --------------------------------------------------------- |
| `pnpm install`      | install JS dependencies                                    |
| `pnpm test`         | run unit tests (vitest)                                    |
| `pnpm tauri dev`    | run the app in dev mode (Vite on :1420 + Rust)             |
| `pnpm tauri build`  | type-check, build the frontend, then bundle a release      |
| `pnpm build`        | frontend only: `vue-tsc --noEmit` then `vite build`        |
| `pnpm preview`      | serve the built frontend standalone                        |

## Structure

```
src/
  main.ts                  app bootstrap: vue + router + vuetify
  App.vue                  renders the app shell
  router/index.ts          the 7 routes + NAV_ITEMS for the drawer
  layouts/
    AppShell.vue           app bar (status, heartbeat, controls, theme),
                           nav drawer, offline banner, snackbar, router-view
  views/                   Dashboard, Logs, Profiles, Proxies, Tasks,
                           Settings, Diagnostics
  components/
    DbUnavailableAlert.vue "database missing / failed to open" + retry
    charts/                BarChartCard (chart.js bars for the dashboard)
    data/                  DataTablePage, FilterBar, LogViewer, MetricCard
    forms/                 ConfirmDialog, SettingField
    layout/                PageLayout, DaemonControls, ThemeToggle
    status/                StatusChip, DaemonStatusChip, HeartbeatIndicator
  composables/
    useDaemonStatus.ts     1s polling of /health + /state, control commands
    useDashboard.ts        metrics polling for the dashboard
    useLogs.ts             live log polling, cursor pages, filters
    useDb.ts, useThemeToggle.ts
  constants/               statusMap (shared status colours/icons), schemas
  lib/
    control.ts             control requests + button disabled rules
    daemonApi.ts           /health, /state, /control/* over the Tauri proxy
    dbApi.ts               db_open / metrics / paged log read commands
    logFilters.ts          log filter form, validation, localStorage
    logMerge.ts            cursor page merge for the live list
    poll.ts                polling reducer (online/offline, tick dedup)
    series.ts, thresholds.ts, csv.ts, format.ts, types.ts
  plugins/vuetify.ts       gruvbox themes and component defaults
  styles/global.css        font, .text-muted, .empty-state
src-tauri/                 Rust shell
  src/lib.rs               tauri builder, command registration
  src/control.rs           HTTP proxy to the daemon control API
  src/db.rs                read-only SQLite log reader
  tauri.conf.json          app id, window, build + bundle config
```

## Daemon connection

The frontend never talks HTTP directly (CSP/scopes); it calls the Tauri
command `control_request`, implemented in `src-tauri/src/control.rs`. It reads
the same environment contract as the daemon:

| Variable                 | Meaning                          | Default                     |
| ------------------------ | -------------------------------- | --------------------------- |
| `ADCLICKER_CONTROL_TOKEN`| auth token (same name as daemon) | generated at startup        |
| `ADCLICKER_API_URL`      | daemon control API base URL      | `http://127.0.0.1:8787`     |

The token is never logged or echoed back to the frontend.

On startup the app resolves the token once (`ensure_control_token` in
`src/control.rs`): an explicitly set variable always wins, otherwise the app
generates 128 random bits and exports them to its own environment, so the UI
and the daemon it spawns share the same value without any manual setup — this
is what makes a Finder launch work. Set the variable yourself only when the
daemon is started externally (launchd plist, manual `daemon.py`): then the app
must receive that same token, or control API calls come back as 401.

## Building a desktop app

`pnpm tauri dev` is only the dev loop (Vite on :1420 + Rust host, daemon from
Python sources). For a real desktop app run `pnpm tauri build` — it
type-checks the frontend, builds the Rust release and packages a bundle
(`.app`/`.dmg` on macOS, `.deb`/`.rpm`/AppImage on Linux) into
`src-tauri/target/release/bundle/`. Double-clicking that `.app` needs no
environment set up beforehand.

The sidecar binary has to exist before the bundle step:

```sh
nix develop --command bash scripts/build-sidecar.sh          # with nix
scripts/bootstrap-venv.sh && PYTHON_BIN=.venv/bin/python \
  scripts/build-sidecar.sh                                   # without nix
```

What the packaged launch resolves on its own (see `src-tauri/src/daemon.rs`
and `src-tauri/src/resources.rs`):

- control token — generated at startup if the variable is unset;
- daemon program — the `engine` sidecar next to the app binary inside
  `.app/Contents/MacOS/`; `ADCLICKER_DAEMON_PYTHON` still wins, and
  `pnpm tauri dev` stays on `python3` from sources even though tauri copies a
  frozen sidecar next to the dev binary;
- working directory — project tree above the launch directory, otherwise the
  app data dir (`~/Library/Application Support/Premium Bot` on macOS,
  the same path as `WorkingDirectory` in the launchd plist);
- data files — `config.json`, `queries.txt`, `proxies.txt`,
  `user_agents.txt`, `domains.txt`, `domain_mapping.json`, `cookies.txt` are
  bundled via `bundle.resources` and copied into that directory on first
  launch, missing files only (user edits win over the bundle). `queries.txt`
  and `proxies.txt` are local, untracked files: the bundle step needs them on
  the build machine;
- the port — before spawning, the app probes `:8787` with its token: a
  daemon already answering is adopted instead of duplicated, and a daemon
  with a foreign token is reported («порт занят демоном с другим токеном»)
  instead of spawning a child that cannot bind;
- database path — `adclicker.db` is read from the same working directory
  where the daemon writes it (`ADCLICKER_DB` still overrides).

Startup failures (no token, no `config.json`, foreign daemon) are stored in
`daemon_status.last_error` and shown in the top banner together with the
polling error, because stderr of a Finder-launched app goes nowhere.

## Theming

Two themes live in `src/plugins/vuetify.ts` (`adclickerLight`, `adclickerDark`).
The current choice is persisted to `localStorage` under `adclicker:theme` and
restored on startup, so the app keeps your preference between runs.

## Routing note

The router uses `createWebHashHistory`, not `createWebHistory`. Tauri serves the
frontend from a custom protocol with no SPA fallback, so history mode breaks on
reload and deep links inside a packaged build.

## Linux system dependencies

Building on Linux needs the WebKitGTK stack. On Debian/Ubuntu:

```sh
sudo apt install libwebkit2gtk-4.1-dev build-essential curl wget file \
  libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev
```

On NixOS the equivalent `nix-shell` inputs are:

```sh
nix-shell -p webkitgtk_4_1 gtk3 libayatana-appindicator librsvg openssl \
  pkg-config glib cairo pango gdk-pixbuf at-spi2-atk dbus \
  --run "pnpm tauri build"
```

Without them, `cargo` fails early with
`The system library 'libsoup-3.0' required by crate 'soup3-sys' was not found`.

### Known NixOS caveat: AppImage bundling

On NixOS, `pnpm tauri build` compiles the app and produces working `.deb` and
`.rpm` bundles, but the **AppImage** step fails:

```
subprocess ~/.cache/tauri/linuxdeploy-plugin-gstreamer.sh --plugin-api-version failed with exit code 6
```

`linuxdeploy-plugin-gstreamer.sh` has the shebang `#! /bin/bash`, and NixOS only
provides `/bin/sh`. This is a host limitation, not a project defect; on
Debian/Ubuntu AppImage bundling works normally.

## Recommended IDE Setup

- [VS Code](https://code.visualstudio.com/) + [Vue - Official](https://marketplace.visualstudio.com/items?itemName=Vue.volar) + [Tauri](https://marketplace.visualstudio.com/items?itemName=tauri-apps.tauri-vscode) + [rust-analyzer](https://marketplace.visualstudio.com/items?itemName=rust-lang.rust-analyzer)
