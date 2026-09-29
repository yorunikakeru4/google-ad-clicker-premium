"""Конкурентная запись из РАЗНЫХ процессов и откат неудачного батча.

``test_store.py`` закрепляет поведение одного writer'а внутри процесса: там все
воркеры — потоки с общей блокировкой и одним соединением, поэтому он ничего не
говорит про боевую схему фазы 3, где каждый воркер — отдельный процесс со своим
``StoreWriter`` и своим соединением. Блокировки SQLite живут между процессами,
и именно этот контракт здесь и проверяется:

1. N процессов пишут одновременно (барьер старта, а не «кто быстрее стартовал»)
   — потеряно ровно 0 записей, порядок внутри каждого процесса сохранён;
2. читатель с ``busy_timeout > 0`` не получает «database is locked», пока
   фоновый батчер с ``batch_size=1`` долбит записями;
3. упавший батч откатывается целиком: «половины» пачки не бывает даже в самом
   опасном случае — когда ``logs`` уже вставлены, а ``clicks`` упали; после
   восстановления схемы запись продолжается.

WAL и ``busy_timeout`` сами по себе уже проверены в
``tests/engine/db/test_migrations.py``, поэтому здесь не дублируются: важен
только наблюдаемый результат под реальной конкуренцией. Любое ожидание сделано
по дедлайну — зависший процесс роняет тест с диагностикой, а не висит вечно.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from engine.db import migrations
from engine.store import StoreWriter

# Дочерний процесс запускается отсюда: в корне репозитория лежит engine/.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Писатель с барьером: отрапортовать о готовности и ждать стартового флага.
# Без барьера процессы начали бы писать в момент, когда первый уже закончил,
# и «конкурентность» проверялась бы лишь постфактум.
BARRIER_WRITER = """
import sys
import time
from pathlib import Path

from engine.store import StoreWriter

db_path, prefix, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
ready, start = Path(sys.argv[4]), Path(sys.argv[5])

writer = StoreWriter(db_path, batch_size=7, flush_interval=0.02)
ready.write_text(prefix, encoding="utf-8")

deadline = time.monotonic() + 30
while not start.exists():
    if time.monotonic() > deadline:
        writer.close()
        sys.exit(2)
    time.sleep(0.001)

for index in range(count):
    writer.log(
        level="INFO",
        category="click",
        message=f"{prefix}-{index:05d}",
        browser_id=prefix,
    )
writer.close()
sys.exit(1 if writer.dropped else 0)
"""

# Фоновый батчер: batch_size=1 превращает каждую запись в отдельную
# транзакцию, flush_interval не даёт буферу ждать. Читателю остаётся
# успеть сделать выборки, пока идёт залп.
BURST_WRITER = """
import sys

from engine.store import StoreWriter

db_path, count = sys.argv[1], int(sys.argv[2])

writer = StoreWriter(db_path, batch_size=1, flush_interval=0.005)
for index in range(count):
    writer.log(
        level="INFO",
        category="click",
        message=f"burst-{index:05d}",
        browser_id="burst",
    )
writer.close()
sys.exit(1 if writer.dropped else 0)
"""


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


def _spawn(script: str, *args: str) -> subprocess.Popen:
    """Запустить дочерний процесс из корня репозитория, где лежит engine/."""

    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _kill(procs: list[subprocess.Popen]) -> None:
    """Погасить незавершённые процессы.

    Осиротевший писатель пережил бы pytest и продолжил писать в tmp_path,
    который каталог cleanup уже удалил.
    """

    for proc in procs:
        if proc.returncode is None:
            proc.kill()
    for proc in procs:
        if proc.returncode is None:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass


def _collect_all(procs: list[subprocess.Popen], labels: list[str], timeout: float) -> None:
    """Дождаться всех процессов по общему дедлайну.

    Ненулевой код возврата — провал: писатель сам считает потери и выходит
    с 1, если ``dropped`` не ноль.
    """

    deadline = time.monotonic() + timeout
    for proc, label in zip(procs, labels):
        remaining = max(deadline - time.monotonic(), 0.1)
        try:
            _, stderr = proc.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=10)
            raise AssertionError(f"{label}: процесс не завершился за {timeout:.0f} с") from None
        assert proc.returncode == 0, (
            f"{label}: ненулевой код возврата {proc.returncode} (writer потерял записи?), "
            f"stderr:\n{stderr}"
        )


def _wait_until_ready(
    markers: list[Path],
    procs: list[subprocess.Popen],
    labels: list[str],
    timeout: float,
) -> None:
    """Дождаться, пока все процессы отметятся, не дожидаясь вечно упавших."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(marker.exists() for marker in markers):
            return
        for proc, label in zip(procs, labels):
            if proc.returncode is None and proc.poll() is not None:
                _, stderr = proc.communicate()
                raise AssertionError(f"{label}: умер до старта, stderr:\n{stderr}")
        time.sleep(0.01)
    missing = [marker.name for marker in markers if not marker.exists()]
    raise AssertionError(f"процессы не отметились за {timeout:.0f} с, ждём: {missing}")


def _rows(db_path, sql: str, params=()):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def _count(db_path, table: str) -> int:
    # Имя таблицы — константа из теста, значения из данных в SQL не попадают.
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _count_as_reader(db_path) -> int:
    """Выборка через ``migrations.connect`` — так же читает БД внешний читатель.

    Именно там выставлен ``busy_timeout``, поэтому упасть такое чтение может
    только ошибкой блокировки. Соединение закрывается явно: ``with conn`` в
    sqlite3 управляет транзакцией, а не жизнью соединения.
    """

    conn = migrations.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0])
    finally:
        conn.close()


def _rename_column(db_path, table: str, old: str, new: str) -> None:
    """Сломать или восстановить схему со стороны другого соединения.

    Writer уже держит своё соединение — именно это и нужно: он должен
    обнаружить несуществующую колонку на своей стороне и упасть в счётчики,
    а не в вызывающий код.
    """

    with sqlite3.connect(db_path) as conn:
        conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
        conn.commit()


class TestParallelProcesses:
    def test_parallel_processes_lose_nothing_and_keep_per_process_order(self, db_path, tmp_path):
        """Четыре процесса пишут одновременно: потеряно 0 записей, порядок цел.

        Доказывает то, что потоковый тест доказать не может: независимые
        соединения под WAL не роняют и не перемешивают записи друг друга —
        каждая пачка предсказуема по ``browser_id``, а внутри процесса
        сообщения идут в порядке записи (иначе в логах был бы бардак из
        «что успело закоммититься первым»).
        """

        prefixes = [f"proc-{index}" for index in range(4)]
        per_process = 300
        ready_dir = tmp_path / "ready"
        ready_dir.mkdir()
        start_flag = tmp_path / "start"

        procs = [
            _spawn(
                BARRIER_WRITER,
                str(db_path),
                prefix,
                str(per_process),
                str(ready_dir / prefix),
                str(start_flag),
            )
            for prefix in prefixes
        ]
        try:
            _wait_until_ready(
                [ready_dir / prefix for prefix in prefixes], procs, prefixes, timeout=30
            )
            start_flag.write_text("go", encoding="utf-8")
            _collect_all(procs, prefixes, timeout=60)
        finally:
            _kill(procs)

        assert _count(db_path, "logs") == len(prefixes) * per_process, (
            f"межпроцессная запись потеряла строки: ожидалось {len(prefixes) * per_process}, "
            f"в базе {_count(db_path, 'logs')}"
        )

        for prefix in prefixes:
            messages = [
                row["message"]
                for row in _rows(
                    db_path,
                    "SELECT message FROM logs WHERE browser_id = ? ORDER BY id",
                    (prefix,),
                )
            ]
            expected = [f"{prefix}-{index:05d}" for index in range(per_process)]
            assert messages == expected, (
                f"у {prefix} нарушен порядок или состав записей: "
                f"получено {len(messages)} строк, первая ошибка на позиции "
                f"{next((i for i, (a, b) in enumerate(zip(messages, expected)) if a != b), '—')}"
            )


class TestReaderAgainstActiveBatcher:
    def test_reader_never_sees_locked_database_while_batcher_writes(self, db_path):
        """Читатель получает выборки, пока фоновый батчер пишет, и ни одной ошибки блокировки.

        Доказывает контракт «UI читает БД одновременно с писателями»: выборки
        идут через соединение с ``busy_timeout`` (как в ``migrations.connect``),
        поэтому единственный возможный провал — «database is locked». Тест
        дополнительно фиксирует, что чтение реально пересеклось с записью
        (иначе он доказывал бы лишь то, что читать пустую базу можно).
        """

        total = 600
        proc = _spawn(BURST_WRITER, str(db_path), str(total))
        counts: list[int] = []
        errors: list[str] = []
        deadline = time.monotonic() + 45

        try:
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    counts.append(_count_as_reader(db_path))
                except sqlite3.Error as exc:
                    errors.append(str(exc))
                time.sleep(0.005)
            assert proc.poll() is not None, "фоновый писатель не завершился за 45 с"
            _collect_all([proc], ["burst-writer"], timeout=15)
        finally:
            _kill([proc])

        assert not errors, "чтение упало во время активной записи:\n" + "\n".join(errors)
        assert len(counts) >= 10, (
            f"окно чтения слишком мало: {len(counts)} выборок — тест не проверил конкуренцию"
        )
        assert any(0 < seen < total for seen in counts), (
            "ни одна выборка не попала в момент записи: "
            "читатель не пересекся с батчером, тест ничего не доказал"
        )
        assert _count(db_path, "logs") == total, "за окно прогона должна быть записана вся пачка"


class TestFailedBatchRollback:
    def test_broken_logs_column_drops_whole_batch_and_recovers_after_restore(self, db_path):
        """Битая схема логов: пачка теряется целиком, запись восстанавливается.

        Доказывает политику ошибок записи из ``store.py``: обрыв не роняет
        вызывающего, ``dropped`` растёт ровно на размер батча, ``last_error``
        называет причину, а в базе не остаётся ни одной строки упавшей пачки.
        Восстановление колонки возвращает writer к работе без пересоздания —
        то есть ошибка была в данных, а не в соединении.
        """

        writer = StoreWriter(db_path, batch_size=10, flush_interval=60.0)
        try:
            _rename_column(db_path, "logs", "message", "message_broken")

            for index in range(10):
                writer.log(
                    level="INFO", category="click", message=f"broken-{index}", browser_id="br-1"
                )

            assert writer.dropped == 10, (
                f"потерян ровно батч из 10 записей, получено {writer.dropped}"
            )
            assert writer.last_error is not None and "message" in writer.last_error, (
                f"last_error должна называть сломанную колонку, получено: {writer.last_error}"
            )
            assert _count(db_path, "logs") == 0, (
                "ни одна строка упавшего батча не должна попасть в БД"
            )

            _rename_column(db_path, "logs", "message_broken", "message")

            for index in range(3):
                writer.log(level="INFO", category="click", message=f"ok-{index}", browser_id="br-1")
            writer.flush()

            assert writer.dropped == 10, (
                "успешные записи после восстановления не должны увеличивать счётчик потерь"
            )
            assert _count(db_path, "logs") == 3, (
                "после восстановления схемы запись должна продолжиться"
            )
        finally:
            writer.close()

    def test_failed_click_batch_rolls_back_logs_written_in_same_batch(self, db_path):
        """Пачка из логов и кликов: упавшие клики откатывают уже вставленные логи.

        Это и есть проверка «не половина»: ``executemany`` по ``logs`` в той же
        транзакции успевает выполниться, и если бы отката не было, в базе
        осталась бы ровно половина пачки. Счётчик потерь обязан вырасти на
        весь батч, а не на упавшую половину.
        """

        writer = StoreWriter(db_path, batch_size=10, flush_interval=60.0)
        try:
            _rename_column(db_path, "clicks", "url", "url_broken")

            # 5 логов + 5 кликов: ровно батч, сброс стартует на последней записи.
            for index in range(5):
                writer.log(
                    level="INFO", category="click", message=f"mix-{index}", browser_id="br-1"
                )
            for index in range(5):
                writer.record_click(url=f"https://example.com/{index}", browser_id="br-1")

            assert writer.dropped == 10, (
                f"вся пачка (5 логов + 5 кликов) должна быть потеряна, получено {writer.dropped}"
            )
            assert writer.last_error is not None and "url" in writer.last_error, (
                f"last_error должна называть сломанную колонку, получено: {writer.last_error}"
            )
            assert _count(db_path, "logs") == 0, (
                "логи из упавшей пачки не должны пережить откат — иначе база получит половину"
            )
            assert _count(db_path, "clicks") == 0, "строки кликов из упавшей пачки недопустимы"

            _rename_column(db_path, "clicks", "url_broken", "url")

            writer.log(level="INFO", category="click", message="after", browser_id="br-1")
            writer.record_click(url="https://example.com/after", browser_id="br-1")
            writer.flush()

            assert writer.dropped == 10, "запись после восстановления схемы идёт без потерь"
            assert _count(db_path, "logs") == 1
            assert _count(db_path, "clicks") == 1
        finally:
            writer.close()
