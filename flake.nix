{
  description = "Google Ad Clicker Premium - Python dev environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      nixpkgs,
      flake-utils,
      ...
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python312;

        # Everything the engine and the test suite need, provided by nix.
        #
        # One legacy dep is absent from nixpkgs and is NOT provided here:
        #   - seleniumbase -> only needed for webdriver.use_seleniumbase mode
        # The Tk GUI dependency was dropped with the Tk GUI itself: the Tauri
        # UI replaces it, and neither the code nor the tests import it.
        pythonDeps = ps: with ps; [
          # engine runtime
          selenium
          undetected-chromedriver
          websocket-client
          openpyxl
          pyautogui
          psutil
          pydantic
          cryptography

          # сборка sidecar-бинарника (план.md, фаза 11): spec лежит в корне,
          # pyinstaller приходит из той же python.withPackages, что и движок,
          # иначе он собрал бы бинарник под чужой интерпретатор
          pyinstaller

          # test suite
          pytest
          pytest-asyncio
          pytest-mock
          httpx
        ]
        # macOS (план.md, фаза 11): pyautogui на darwin работает через
        # PyObjC/Quartz — без него random_mouse и доступ к экрану падают.
        # На Linux эти пакетов в nixpkgs нет, поэтому только под darwin.
        ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isDarwin (
          with ps;
          [
            pyobjc-core
            pyobjc
          ]
        );

        # GTK/WebKit-стек для Rust-части UI (Tauri) + библиотеки chromedriver.
        #
        # cargo test/build для ui/src-tauri без этого падает на первом же
        # -sys крейте: gtk-sys ищет gtk+-3.0.pc и др. через pkg-config,
        # libdbus-sys — dbus-1.pc. Список покрывает всё, что тянет tauri v2
        # на Linux: webkit2gtk-4.1 (включая javascriptcoregtk) + libsoup-3.0
        # + gtk3 с транзитивными glib/cairo/pango/atk/gdk-pixbuf + dbus.
        # nss/nspr/libxcb — динамические зависимости chromedriver (живые
        # e2e-прогоны): без них драйвер падает со status 127 на NixOS.
        #
        # lib.optionals hostPlatform.isLinux: на macOS этих пакетов в nixpkgs
        # нет (webkitgtk, dbus, appindicator — linux-only), и сам факт их
        # наличия в списке роняет eval dev-shell на целевой платформе проекта;
        # там Tauri берёт фреймворки из Xcode SDK.
        tauriDeps = pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux (
          with pkgs;
          [
            gtk3
            glib
            cairo
            pango
            atk
            gdk-pixbuf
            libsoup_3
            webkitgtk_4_1
            dbus
            nss
            nspr
            libxcb
          ]
        );
      in
      {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            (python.withPackages pythonDeps)
            ruff
            basedpyright
            git
            uv
          ];

          nativeBuildInputs = [ pkgs.pkg-config ];
          buildInputs = tauriDeps;          shellHook = ''
            export PYTHONPATH="$PWD:$PYTHONPATH"
            export PYTHONDONTWRITEBYTECODE=1
            # Системный cargo снаружи nix не кладёт rpath на библиотеки из
            # nix store — без этого тест-бинарник tauri не стартует.
            export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath tauriDeps}:$LD_LIBRARY_PATH"
            echo "python:     $(python --version 2>&1)"
            echo "pytest:     $(pytest --version 2>&1 | head -1)"
            echo "ruff:       $(ruff --version 2>&1)"
            echo "uv:         $(uv --version 2>&1)"
            echo "pkg-config: $(pkg-config --version 2>&1)"
          '';
        };

        # Мутационное тестирование.
        #
        # mutmut правит исходники на месте, поэтому прогон идёт на копии
        # рабочего дерева во временном каталоге, который удаляется на выходе.
        # Настоящий checkout не мутируется никогда — ни при успехе, ни при
        # прерывании. Копия снимается tar'ом с рабочего дерева, а не из git
        # archive, чтобы в прогон попадали и незакоммиченные изменения.
        apps.mutation = {
          type = "app";
          program = "${
            pkgs.writeShellApplication {
              name = "mutation";
              runtimeInputs = [ pkgs.uv ];
              text = ''
                set -euo pipefail
                repo="$PWD"
                workdir=$(mktemp -d)
                trap 'rm -rf "$workdir"' EXIT

                tar -C "$repo" \
                  --exclude=.git \
                  --exclude=.worktrees \
                  --exclude=node_modules \
                  --exclude=__pycache__ \
                  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
                  --exclude=.pytest_cache \
                  --exclude=dist \
                  -cf - . | tar -C "$workdir" -xf -

                cd "$workdir"
                echo "мутация идёт на копии в $workdir, checkout не затрагивается"
                # Изолированное окружение mutmut содержит только его самого,
                # поэтому pytest ставится сюда же: системный из nix-среды
                # сюда не попадает.
                uv run --no-project --python 3.12 --with mutmut --with pytest \
                  mutmut run
              '';
            }
          }/bin/mutation";
        };
      }
    );
}
