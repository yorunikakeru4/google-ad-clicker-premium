#!/usr/bin/env bash
set -euo pipefail

# Сборка движка в onefile-бинарник и укладка его в формат Tauri externalBin:
#
#     ui/src-tauri/binaries/engine-<target-triple>
#
# Tauri сам суффиксует triple (bundle.externalBin в tauri.conf.json), поэтому
# имя обязано совпадать с тем, что выдаёт rustc как host. Триплет берётся из
# `rustc -vV`, переопределяется переменной TARGET_TRIPLE (кросс-сборка или
# отсутствующий rustc). Обычный запуск — из dev-shell:
#
#     nix develop --command bash scripts/build-sidecar.sh
#
# PYTHON_BIN — интерпретатор, которым гоняется PyInstaller: по умолчанию
# `python` из PATH (в dev-shell это nix-python с pyinstaller), можно указать
# .venv/bin/python для сборки с seleniumbase/telegram из requirements.txt.

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PYTHON_BIN="${PYTHON_BIN:-python}"

if [[ -n "${TARGET_TRIPLE:-}" ]]; then
    triple="${TARGET_TRIPLE}"
elif command -v rustc >/dev/null 2>&1; then
    triple="$(rustc -vV | sed -n 's/^host: //p')"
else
    echo "ошибка: rustc не найден в PATH — задайте TARGET_TRIPLE вручную" >&2
    exit 1
fi

if [[ -z "${triple}" ]]; then
    echo "ошибка: не удалось определить target triple (пустой вывод rustc -vV)" >&2
    exit 1
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "ошибка: интерпретатор ${PYTHON_BIN} не найден (нужен pyinstaller, см. flake.nix)" >&2
    exit 1
fi

echo "==> triple:  ${triple}"
echo "==> python:  $(${PYTHON_BIN} --version 2>&1) (${PYTHON_BIN})"
echo "==> сборка:  PyInstaller bundle.spec (onefile, точка входа engine/bundle.py)"
"${PYTHON_BIN}" -m PyInstaller --noconfirm bundle.spec

artifact="dist/engine"
if [[ "${triple}" == *windows* && -f "dist/engine.exe" ]]; then
    artifact="dist/engine.exe"
fi
if [[ ! -f "${artifact}" ]]; then
    echo "ошибка: артефакт ${artifact} не появился" >&2
    exit 1
fi

dest="ui/src-tauri/binaries/engine-${triple}"
if [[ "${triple}" == *windows* ]]; then
    dest="${dest}.exe"
fi
mkdir -p "$(dirname "${dest}")"
cp "${artifact}" "${dest}"
chmod +x "${dest}"

echo "==> готово: ${dest} ($(du -h "${dest}" | cut -f1))"
echo "==> теперь cargo/tauri видят sidecar: bundle.externalBin = binaries/engine"
