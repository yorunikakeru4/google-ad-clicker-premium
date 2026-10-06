"""Матчинг доменов чёрного списка клика (план §5, фаза 13).

Модуль лежит в ``engine`` и не тянет legacy-зависимости: его читают и
демон (нормализация строк при добавлении в ``domains.txt``), и
``utils.get_domains`` в воркере. Импорт ``utils`` из демона был бы
недопустим — он тянет selenium/undetected_chromedriver, которых в
control plane нет.

Список ``paths.filtered_domains`` (+ ``behavior.own_domain``) — чёрный:
ссылка на домен из списка не кликается ни в рекламе, ни в shopping, ни в
органике.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse


def normalize_domain(value: Optional[str]) -> str:
    """Хост домена из строки файла или URL: ``https://www.edelind.de/x`` → ``edelind.de``.

    Строки в ``domains.txt`` пишут как угодно — с ``www``, со схемой, с путём,
    в кавычках, в разном регистре. Матчинг сравнивает хосты, поэтому всё это
    приводится к одному виду здесь, а не в каждой точке клика. Пустая строка —
    не домен: пустой элемент списка блокировал бы любую ссылку.

    :param value: строка файла, URL или значение ``behavior.own_domain``
    :rtype: str
    :returns: хост без ``www`` и без точки в конце, либо пустая строка
    """

    if not value:
        return ""

    text = str(value).strip().strip("'\"").strip().lower()
    if not text:
        return ""

    # Схема есть не у строк из файла: без «//» urlparse читал бы домен как путь.
    if "://" not in text:
        text = "//" + text

    try:
        host = urlparse(text).hostname or ""
    except ValueError:
        # Некорректный хост («[», пустой порт и подобное) — не домен.
        return ""

    return host.removeprefix("www.").rstrip(".")


def domain_matches(url: Optional[str], domain: Optional[str]) -> bool:
    """Попадает ли URL под запрет домена: хост равен домену или его поддомен.

    Подстрочный матчинг тут был бы источником ложных срабатываний:
    ``notedelind.de`` содержит ``edelind.de``, но не является им. Домен из
    списка сопоставляется только с границей хоста, ``www`` с обеих сторон
    не различается, а домен в query или fragment хостом не является.

    :type url: str
    :param url: ссылка (href), абсолютная или сырая строка файла
    :type domain: str
    :param domain: домен чёрного списка
    :rtype: bool
    """

    host = normalize_domain(url)
    blocked = normalize_domain(domain)
    if not host or not blocked:
        return False
    return host == blocked or host.endswith("." + blocked)
