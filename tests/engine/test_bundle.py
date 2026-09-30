"""Диспетчер бинарника sidecar: ``worker`` → воркер, всё прочее → демон.

Один PyInstaller-бинарник обслуживает оба CLI: супервизор спавнит воркеров тем
же файлом, что Tauri поднимает как демон, поэтому выбор ветки — по первому
аргументу. Диспетчер проверяется на моканном argv и подменённых ``main()``
обеих сторон: ни демон, ни воркер в тесте не должны реально стартовать — иначе
тест тронул бы БД, сигналы и порт.

Контракт:

* ``worker ...`` (ровно первым токеном) → ``engine.worker.main`` с хвостом
  аргументов, сам токен не пробрасывается;
* всё остальное, включая пустой argv и не-первый ``worker`` →
  ``engine.control_plane.daemon`` с argv целиком;
* код возврата ветки возвращается вызывающему без изменений.
"""

from __future__ import annotations

import sys

import pytest

from engine import bundle


@pytest.fixture
def routed(monkeypatch) -> dict[str, list[str]]:
    """Подменяет обе ветки и записывает, куда ушёл какой argv."""

    calls: dict[str, list[str]] = {}

    def fake_worker(argv: list[str]) -> int:
        calls["worker"] = list(argv)
        return 11

    def fake_daemon(argv: list[str]) -> int:
        calls["daemon"] = list(argv)
        return 22

    monkeypatch.setattr("engine.worker.main", fake_worker)
    monkeypatch.setattr("engine.control_plane.daemon.main", fake_daemon)
    return calls


def test_worker_token_routes_to_worker_with_tail_argv(routed):
    code = bundle.dispatch(["worker", "--browser-id", "br-1", "--db", "x.db"])

    assert code == 11
    assert routed == {"worker": ["--browser-id", "br-1", "--db", "x.db"]}


def test_worker_token_alone_forwards_empty_argv(routed):
    """Пустой хвост — не повод уходить в демон: воркер сам скажет про
    отсутствующий ``--browser-id`` своим кодом и сообщением."""
    code = bundle.dispatch(["worker"])

    assert code == 11
    assert routed == {"worker": []}


def test_empty_argv_starts_daemon(routed):
    code = bundle.dispatch([])

    assert code == 22
    assert routed == {"daemon": []}


def test_daemon_receives_argv_verbatim(routed):
    code = bundle.dispatch(["--db", "x.db", "--port", "0"])

    assert code == 22
    assert routed == {"daemon": ["--db", "x.db", "--port", "0"]}


def test_unknown_subcommand_goes_to_daemon(routed):
    """Неизвестный токен — не наш случай: его разберёт argparse демона и
    вернёт понятный отказ, а не молчаливый прогон воркера."""
    code = bundle.dispatch(["frobnicate"])

    assert code == 22
    assert routed == {"daemon": ["frobnicate"]}


def test_worker_is_only_a_dispatch_when_it_is_first(routed):
    """``worker`` не первым токеном — это позиционный аргумент демона, а не
    команда: иначе ``--db worker`` неожиданно поднял бы воркер."""
    bundle.dispatch(["--db", "worker"])

    assert "worker" not in routed
    assert routed == {"daemon": ["--db", "worker"]}


def test_argv_none_takes_sys_argv(routed, monkeypatch):
    """``dispatch()`` без аргументов читает ``sys.argv`` — именно так его
    зовёт ``if __name__ == "__main__"`` в замороженном бинарнике."""
    monkeypatch.setattr(sys, "argv", ["engine", "worker", "--browser-id", "br-9"])

    code = bundle.dispatch()

    assert code == 11
    assert routed == {"worker": ["--browser-id", "br-9"]}
