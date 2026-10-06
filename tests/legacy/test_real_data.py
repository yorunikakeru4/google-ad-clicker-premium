"""Characterization-тесты на реальных данных пользователя.

Данные (запросы, домены для отбора non-ad ссылок, прокси) лежат вне
репозитория — в ``~/.cache/clicker-testdata`` или в каталоге из
``CLICKER_TEST_DATA_DIR``. Файлы никогда не коммитятся и не копируются:
тесты читают их напрямую, сеть не открывается, пароли прокси не попадают в
assert'ы, сообщения об ошибках и журналы — печатаются только host, порт и
счётчики.

Зачем: синтетические тесты в ``test_utils.py``/``test_proxy.py`` проверяют
парсеры на примерах, придуманных автором теста. Здесь те же функции прогоняются
через настоящие файлы — именно там всплывают мелочи, которых нет в примерах:
кириллица, отсутствующий перевод строки в конце файла, реальная структура
учётных записей. Тесты фиксируют текущее поведение (characterization): они
отвечают на вопрос «что реально делает парсер с данными пользователя», а не
на вопрос «каким парсер должен быть».

На машине без файлов каждый тест пропускается (``skipif``), а не падает.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import FakeDriver

import proxy
import utils
from engine.proxy_auth import parse_proxy_credentials
from test_search_controller import make_link

_ENV_DATA_DIR = os.environ.get("CLICKER_TEST_DATA_DIR")
TEST_DATA_DIR = (
    Path(_ENV_DATA_DIR).expanduser()
    if _ENV_DATA_DIR
    else Path.home() / ".cache" / "clicker-testdata"
)


def _data_ready(filename: str) -> bool:
    path = TEST_DATA_DIR / filename
    return path.is_file() and path.stat().st_size > 0


def _missing_reason(filename: str) -> str:
    return f"нет реальных данных: {TEST_DATA_DIR / filename} (проверьте CLICKER_TEST_DATA_DIR)"


def _non_blank_lines(path: Path) -> list[str]:
    """Строки файла без пустых: с ними сравнивается результат парсера."""

    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def test_data_dir() -> Path:
    """Каталог с реальными данными; наличие файлов проверяет skipif теста."""

    return TEST_DATA_DIR


@pytest.mark.skipif(not _data_ready("queries.txt"), reason=_missing_reason("queries.txt"))
def test_real_queries_file_is_parsed_line_by_line(set_paths, test_data_dir):
    """Реальный файл запросов читается целиком: ни одна строка не потеряна, мусора нет.

    Доказывает комплектность ``utils.get_queries()`` (та функция, что
    обслуживает ``paths.query_file``): парсер не выбрасывает строки с
    кириллицей и цифрами, не склеивает и не порождает пустых «запросов» —
    пустая строка в списке означала бы бессмысленный поиск впустую.
    """

    path = test_data_dir / "queries.txt"
    file_lines = _non_blank_lines(path)
    set_paths(query_file=path)

    queries = utils.get_queries()

    assert len(queries) >= 100, (
        f"ожидался большой реальный файл запросов, разобрано {len(queries)} строк — "
        "возможно, тест смотрит не в тот каталог"
    )
    assert len(queries) == len(file_lines), (
        f"парсер запросов потерял строки: в файле {len(file_lines)} непустых, "
        f"разобрано {len(queries)}"
    )
    assert all(query.strip() for query in queries), (
        "в списке запросов не должно быть пустых или пробельных строк"
    )
    # Порядок концов: первая строка файла — первый запрос, последняя — последний.
    assert queries[0] == file_lines[0].strip().strip("'\""), (
        "первая строка файла не стала первым запросом"
    )
    assert queries[-1] == file_lines[-1].strip().strip("'\""), (
        "последняя строка файла не стала последним запросом"
    )


@pytest.mark.skipif(not _data_ready("domains.txt"), reason=_missing_reason("domains.txt"))
def test_real_domain_list_blocks_its_own_links(
    set_paths, make_search_controller, test_data_dir
):
    """Реальный список доменов запрещает non-ad ссылки через ``_get_non_ad_links``.

    Доказывает, что файл из ``paths.filtered_domains`` реально управляет отбором
    (``ad_clicker.py`` передаёт ``get_domains()`` в ``search_for_ads``): ссылка
    с доменом из файла не кликается вовсе, чужая — проходит, а сырые строки
    файла, не оформленные как URL, не проходят проверку ``href`` — она обязана
    начинаться с http. Синтетические случаи отбора покрыты в
    ``test_search_controller.py``; здесь проверяется только реальный файл.

    Семантика списка — чёрный список (план §5, фаза 13): совпадение с файлом
    отбраковывает ссылку, отсутствие совпадения пропускает. Раньше было
    ровно наоборот, и кликер кликал только ссылки на наш домен.
    """

    path = test_data_dir / "domains.txt"
    set_paths(filtered_domains=path)
    domains = utils.get_domains()

    assert domains, f"реальный файл {path.name} должен содержать хотя бы один домен"
    assert any("edelind.de" in domain for domain in domains), (
        f"тест ожидает в реальном файле edelind.de, сейчас в нём: {len(domains)} записей без него"
    )

    candidates = [
        make_link("https://www.edelind.de/goldkette-75-cm"),
        make_link("https://www.edelind.de"),
        make_link("https://www.booking.com/hotels"),  # дефолт репозитория, в реальном файле его нет
        make_link("edelind.de"),  # сырая строка файла без схемы
        make_link("www.edelind.de"),  # то же, с www
    ]
    controller = make_search_controller(driver=FakeDriver(links=candidates))

    selected = [link.get_attribute("href") for link in controller._get_non_ad_links([], domains)]

    assert selected == ["https://www.booking.com/hotels"], (
        f"наш домен обязан быть заблокирован, а чужой — пройти; выбрано: {selected}"
    )


@pytest.mark.skipif(not _data_ready("proxies.txt"), reason=_missing_reason("proxies.txt"))
def test_real_proxy_file_parses_into_structured_credentials(set_paths, test_data_dir):
    """Реальный файл прокси разбирается построчно в корректную структуру.

    Доказывает, что ``proxy.get_proxies()`` (чтение ``paths.proxy_file``) отдаёт
    ровно строки файла, а существующий разбор ``user:password@host:port``
    принимает каждую из них: логин и пароль непустые, host непустой, порт —
    число в диапазоне 1..65535. Единственный допустимый провал — сообщение о
    номере строки и host: пароли в диагностику не попадают.
    """

    path = test_data_dir / "proxies.txt"
    file_lines = _non_blank_lines(path)
    set_paths(proxy_file=path)

    proxies = proxy.get_proxies()

    assert len(proxies) >= 10, (
        f"ожидался большой реальный файл прокси, прочитано {len(proxies)} строк — "
        "возможно, тест смотрит не в тот каталог"
    )
    assert len(proxies) == len(file_lines), (
        f"чтение файла прокси потеряло строки: в файле {len(file_lines)}, прочитано {len(proxies)}"
    )

    problems: list[str] = []
    for number, line in enumerate(proxies, start=1):
        try:
            username, password = parse_proxy_credentials(line)
        except ValueError:
            problems.append(f"строка {number}: формат не user:password@host:port")
            continue

        host, _, port = line.rpartition("@")[2].partition(":")
        if not username or not password:
            problems.append(f"строка {number}: пустой логин или пароль (host={host})")
        if not host:
            problems.append(f"строка {number}: пустой host")
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            problems.append(f"строка {number}: порт вне диапазона 1..65535 (host={host})")

    assert not problems, (
        f"реальные прокси разобраны не полностью: {len(problems)} из {len(proxies)} строк\n"
        + "\n".join(problems)
    )
