"""Единая точка входа бинарника sidecar: диспетчер воркера и демона.

PyInstaller собирает движок в один executable, которым Tauri пользуется и как
демоном, и как источником воркеров: супервизор в замороженном режиме спавнит
``[sys.executable, "worker", ...]`` (см.
:func:`engine.control_plane.supervisor.default_command`), а хост поднимает
тот же файл без подкоманды. Значит, бинарнику нужен собственный argv-разбор:
первый токен ``worker`` — ветка воркера, всё остальное — CLI демона.

Почему так, а не subparsers: у обеих сторон уже есть готовые argparse-парсеры
и коды возврата (``engine.worker.build_parser``,
``engine.control_plane.daemon.build_parser``). Диспетчер их только вызывает и
не изобретает собственной диагностики — ``--help``, неизвестные аргументы и
отказ БД выглядят для пользователя одинаково в dev (``python -m
engine.bundle``) и в замороженном бинарнике.

Модуль намеренно импортирует ветки лениво и только из ``dispatch``: сам
диспетчер живёт на stdlib, поэтому ``--help`` не тащит selenium, а тесты
подменяют ``main()`` обеих сторон, не запуская ни демон, ни воркер.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

# Подкоманда воркера. Ровно этот токен первым аргументом (а не «содержит»):
# ``--db worker`` — позиционный аргумент демона, а не спавн браузера.
WORKER_COMMAND = "worker"


def dispatch(argv: Sequence[str] | None = None) -> int:
    """Выбирает ветку по argv и возвращает её код возврата.

    ``argv=None`` — читает ``sys.argv[1:]``, как это делает argparse: именно
    так диспетчер вызывается из ``if __name__ == "__main__"`` замороженного
    бинарника. Код возврата возвращается как есть, без ``sys.exit`` — иначе
    протестовать диспетчер из процесса pytest было бы негде.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == WORKER_COMMAND:
        from engine.worker import main as worker_main

        return worker_main(args[1:])
    from engine.control_plane.daemon import main as daemon_main

    return daemon_main(args)


def main() -> int:
    """Точка входа замороженного бинарника.

    Первым делом — перехват дочернего процесса multiprocessing: ветка
    spawn (дефолт macOS) в замороженном exe порождает дочку как
    ``engine --multiprocessing-fork ...`` (``multiprocessing.spawn`` для
    ``sys.frozen``). Без ``freeze_support()`` дочка уходит в обычный
    ``dispatch()``, падает на argparse и умирает, а родитель виснет в
    ``reader.recv()`` до конца времен — так зависал воркер на macOS через
    ``undetected_chromedriver.dprocess.start_detached``
    (``use_subprocess=False``). В dev вызов — no-op; в frozen его делает
    рабочим патч PyInstaller ``pyi_rth_multiprocessing``, подменяющий
    ``freeze_support`` на posix.
    """
    import multiprocessing

    multiprocessing.freeze_support()
    return dispatch()


if __name__ == "__main__":
    # sys.exit, а не голый return: код возврата должен дойти до
    # вызывающей стороны (Tauri, launchd, терминал).
    sys.exit(main())
