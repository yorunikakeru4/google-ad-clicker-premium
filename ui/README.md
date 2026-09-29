# Google Ad Clicker Premium — UI

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
  App.vue                  shell: nav drawer, app bar, control panel, routes
  router/index.ts          the 7 routes + NAV_ITEMS for the drawer
  views/                   Dashboard, Logs, Profiles, Proxies, Tasks,
                           Settings, Diagnostics (stubs until their phases)
  components/
    ControlPanel.vue       Start/Pause/Resume/Restart/Kill + workers table
    HeartbeatChip.vue      daemon alive/offline indicator
    ScreenStub.vue         shared stub for unimplemented screens
    ThemeToggle.vue
  composables/
    useDaemonStatus.ts     1s polling of /health + /state, control commands
    useThemeToggle.ts
  lib/
    control.ts             control requests + button disabled rules
    daemonApi.ts           /health, /state, /control/* over the Tauri proxy
    poll.ts                polling reducer (online/offline, tick dedup)
    format.ts              uptime/PID/last-error formatting
    types.ts               daemon payload types
  plugins/vuetify.ts       light/dark theme definitions
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
| `ADCLICKER_CONTROL_TOKEN`| auth token (same name as daemon) | required                    |
| `ADCLICKER_API_URL`      | daemon control API base URL      | `http://127.0.0.1:8787`     |

The token is never logged or echoed back to the frontend.

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
