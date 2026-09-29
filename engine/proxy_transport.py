"""Транспорты прокси: единственный словарь значений и его нормализация.

Три транспорта (план.md §2.1): ``cdp_auth`` (дефолт) | ``extension`` |
``direct``. Каждый решает, КАК креды доходят до Chrome: CDP-авторизация,
MV3-расширение или whitelist-IP без кредов вовсе.

Модуль лежит отдельно от ``engine.proxy_auth`` и не импортирует ничего, кроме
стандартной библиотеки — это условие, а не украшение:

* ``config_reader`` (legacy) и ``engine.control_plane.config`` обязаны знать
  дефолт и допустимые значения, но не имеют права тянуть за собой websocket,
  логгер и открытие ``StoreWriter``: конфиг читают и демон, и ``gui.py``, а
  побочный эффект импорта создал бы ``adclicker.db`` в чужом каталоге
  независимо от ``--db``;
* ``engine.proxy_auth`` импортирует эти же значения и использует их в
  ``resolve_proxy_transport``, поэтому второй словарь негде появиться:
  значения, нормализация и авторизация — одна цепочка.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_PROXY_TRANSPORT",
    "PROXY_TRANSPORTS",
    "PROXY_TRANSPORT_CDP_AUTH",
    "PROXY_TRANSPORT_DIRECT",
    "PROXY_TRANSPORT_EXTENSION",
]

PROXY_TRANSPORT_CDP_AUTH = "cdp_auth"
PROXY_TRANSPORT_EXTENSION = "extension"
PROXY_TRANSPORT_DIRECT = "direct"
DEFAULT_PROXY_TRANSPORT = PROXY_TRANSPORT_CDP_AUTH
PROXY_TRANSPORTS = frozenset(
    {PROXY_TRANSPORT_CDP_AUTH, PROXY_TRANSPORT_EXTENSION, PROXY_TRANSPORT_DIRECT}
)
