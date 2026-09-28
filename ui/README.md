# Google Ad Clicker Premium — UI

Desktop UI scaffold: **Tauri 2 + Vue 3 + TypeScript + Vuetify 3 + vue-router**.

This is the Phase 0 foundation. The real screens are not implemented yet — the
placeholder route exists only so the stack is wired up and buildable.

## Requirements

- Node.js 20+ and `pnpm`
- Rust toolchain (stable)
- Linux desktop system libraries — see [Linux system dependencies](#linux-system-dependencies)

## Commands

| Command             | What it does                                              |
| ------------------- | --------------------------------------------------------- |
| `pnpm install`      | install JS dependencies                                    |
| `pnpm tauri dev`    | run the app in dev mode (Vite on :1420 + Rust)             |
| `pnpm tauri build`  | type-check, build the frontend, then bundle a release      |
| `pnpm build`        | frontend only: `vue-tsc --noEmit` then `vite build`        |
| `pnpm preview`      | serve the built frontend standalone                        |

## Structure

```
src/
  main.ts                  app bootstrap: vue + router + vuetify
  App.vue                  v-app shell, app bar, <router-view />
  router/index.ts          routes (placeholder only)
  views/HomeView.vue       placeholder screen + scaffold self-check
  components/ThemeToggle.vue
  composables/useThemeToggle.ts
  plugins/vuetify.ts       light/dark theme definitions
src-tauri/                 Rust shell
  src/lib.rs               tauri builder, `greet` command
  tauri.conf.json          app id, window, build + bundle config
```

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
