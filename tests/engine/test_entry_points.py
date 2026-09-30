"""Точки входа движка: `python -m ...` действительно доходит до ``main()``.

Отдельный файл, потому что проверки одинаковые по форме и потому, что
это именно контракт с ОС, а не логика: модуль без ``if __name__ ==
"__main__"`` при запуске через ``-m`` отрабатывает пусто и выходит с кодом 0,
и ни тест на ``main()``, ни супервизор этого не замечают.

``--help`` выбран не случайно: его обрабатывает ``argparse`` внутри ``main``,
поэтому нулевой код возврата доказывает, что ``main()`` был вызван, при этом
не трогая БД, окружение и сигналы. Для ``engine.bundle`` та же проверка
покрывает и диспетчер: с ``worker`` в голове argv обязан отработать воркерный
парсер, без него — демонный.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_module(module: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize(
    ("module", "prog"),
    [
        ("engine.control_plane.daemon", "adclicker-daemon"),
        ("engine.worker", "adclicker-worker"),
    ],
)
def test_module_entry_point_reaches_main(module: str, prog: str):
    result = run_module(module, "--help")

    assert result.returncode == 0, (
        f"{module} завершился с кодом {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert prog in result.stdout, f"ожидался вызов argparse из main(), получено: {result.stdout}"


def test_bundle_entry_point_dispatches_to_daemon():
    """``python -m engine.bundle --help`` — это CLI демона (ветка по умолчанию)."""
    result = run_module("engine.bundle", "--help")

    assert result.returncode == 0, (
        f"engine.bundle завершился с кодом {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "adclicker-daemon" in result.stdout, f"ожидался парсер демона: {result.stdout}"


def test_bundle_entry_point_dispatches_to_worker():
    """``python -m engine.bundle worker --help`` — это CLI воркера."""
    result = run_module("engine.bundle", "worker", "--help")

    assert result.returncode == 0, (
        f"engine.bundle worker завершился с кодом {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "adclicker-worker" in result.stdout, f"ожидался парсер воркера: {result.stdout}"
