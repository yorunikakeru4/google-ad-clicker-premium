#!/usr/bin/env bash
set -euo pipefail

# Bootstrap venv для dev-сценария без sidecar-сборки (план.md, фаза 11):
# системный python + requirements.txt, без nix. Результат — .venv в корне.
#
#     scripts/bootstrap-venv.sh [--force]
#
# После запуска одна переменная настраивает обе стороны (демон и спавн
# воркеров супервизором):
#
#     export PYTHON="$PWD/.venv/bin/python"
#     "$PYTHON" -m engine.control_plane.daemon --port 8787 --db adclicker.db
#
# Повторный запуск поверх существующего .venv — ошибка: venv могли
# доустановить вручную, и тихая перезапись это уничтожит. Пересоздание —
# только явным --force.

cd "$(dirname "${BASH_SOURCE[0]}")/.."

VENV_DIR=".venv"
# pyproject requires-python: >=3.11
MIN_MAJOR=3
MIN_MINOR=11
FORCE=0
PYTHON_FOR_VENV="${PYTHON_FOR_VENV:-python3}"

usage() {
    cat <<'EOF'
Использование: scripts/bootstrap-venv.sh [--force] [--help]

  --force   пересоздать .venv, если он уже есть (иначе — отказ)
  --help    эта справка

Переменные:
  PYTHON_FOR_VENV   интерпретатор для venv (по умолчанию python3)
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -f | --force)
            FORCE=1
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "ошибка: неизвестный аргумент $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if ! command -v "${PYTHON_FOR_VENV}" >/dev/null 2>&1; then
    echo "ошибка: ${PYTHON_FOR_VENV} не найден в PATH" >&2
    exit 1
fi

# Версия проверяется до удаления .venv: неудачная проверка не должна
# стоить уже готового окружения.
if ! "${PYTHON_FOR_VENV}" -c \
    "import sys; raise SystemExit(0 if sys.version_info >= (${MIN_MAJOR}, ${MIN_MINOR}) else 1)"; then
    found="$("${PYTHON_FOR_VENV}" --version 2>&1)"
    echo "ошибка: нужен python >= ${MIN_MAJOR}.${MIN_MINOR} (requires-python в pyproject.toml), найден: ${found}" >&2
    exit 1
fi

if [[ -e "${VENV_DIR}" ]]; then
    if [[ "${FORCE}" -eq 0 ]]; then
        echo "ошибка: ${VENV_DIR} уже существует; повторный запуск пересоздал бы его." >&2
        echo "Если это сделано намеренно: scripts/bootstrap-venv.sh --force" >&2
        exit 1
    fi
    echo "==> --force: удаляю существующий ${VENV_DIR}"
    rm -rf "${VENV_DIR}"
fi

echo "==> python:  $(${PYTHON_FOR_VENV} --version 2>&1) (${PYTHON_FOR_VENV})"
echo "==> venv:    ${VENV_DIR}"
"${PYTHON_FOR_VENV}" -m venv "${VENV_DIR}"

echo "==> pip:     install -r requirements.txt"
"${VENV_DIR}/bin/python" -m pip install --disable-pip-version-check -r requirements.txt

echo
echo "venv готов: ${VENV_DIR}"
echo
echo "Запуск демона из этого окружения (PYTHON настраивает и воркеров тоже):"
echo "  export PYTHON=\"\$PWD/${VENV_DIR}/bin/python\""
echo "  \"\$PYTHON\" -m engine.control_plane.daemon --port 8787 --db adclicker.db"
echo
echo "Однократный прогон без экспорта:"
echo "  ./${VENV_DIR}/bin/python -m engine.control_plane.daemon --help"
