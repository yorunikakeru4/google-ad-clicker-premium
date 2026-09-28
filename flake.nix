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
          ];

          shellHook = ''
            export PYTHONPATH="$PWD:$PYTHONPATH"
            export PYTHONDONTWRITEBYTECODE=1
            echo "python:     $(python --version 2>&1)"
            echo "pytest:     $(pytest --version 2>&1 | head -1)"
            echo "ruff:       $(ruff --version 2>&1)"
          '';
        };
      }
    );
}
