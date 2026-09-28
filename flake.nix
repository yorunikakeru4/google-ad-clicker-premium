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
        # Two legacy deps are absent from nixpkgs and are NOT provided here:
        #   - seleniumbase  -> only needed for webdriver.use_seleniumbase mode
        #   - customtkinter -> only needed by the legacy gui.py, which the Tauri
        #                      UI replaces; not needed by the test suite
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

          # test suite
          pytest
          pytest-asyncio
          pytest-mock
          httpx
        ];
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

          shellHook = ''
            export PYTHONPATH="$PWD:$PYTHONPATH"
            export PYTHONDONTWRITEBYTECODE=1
            echo "python:     $(python --version 2>&1)"
            echo "pytest:     $(pytest --version 2>&1 | head -1)"
            echo "ruff:       $(ruff --version 2>&1)"
            echo "uv:         $(uv --version 2>&1)"
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
