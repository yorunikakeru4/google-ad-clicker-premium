# -*- mode: python ; coding: utf-8 -*-
"""Сборка движка в onefile-бинарник sidecar (план.md, фаза 11).

    pyinstaller bundle.spec          # или python -m PyInstaller bundle.spec
    dist/engine                      # артефакт; в git не попадает

Один файл на два CLI: Tauri поднимает его как демон (без аргументов), а
супервизор в frozen-режиме — как воркер (``engine-<triple> worker ...``).
Диспетчер — :mod:`engine.bundle`, поэтому единственная точка входа здесь —
``engine/bundle.py``.

hiddenimports собраны по факту, а не «на всякий случай»:

* ``engine.*`` целиком — collect_submodules сканирует файлы, а не байткод, и
  покрывает модули, импортируемые лениво/по условию (ветки диспетчера,
  ``engine.db.migrations``, пул прокси);
* ``selenium`` целиком — внутри есть ``importlib.import_module`` по номеру
  версии BiDi-протокола, такое в граф по байткоду не попадает; data-файлы
  (.js-скрипты, selenium-manager) даёт штатный hook-selenium из
  pyinstaller-hooks-contrib, свой collect_data_files не дублируем;
* ``undetected_chromedriver``/``proxy``/``hooks`` и legacy-модули корня —
  импортируются лениво из engine.worker/ad_clicker: modulegraph их видит,
  но имена перечислены явно, чтобы рефакторинг импортов не ронял бинарник
  молча;
* ``charset_normalizer`` — requests резолвит charset-детектор через
  importlib, без явного имени модуль не попадёт в граф.

Чего здесь НЕТ: python-telegram-bot (``telegram_notifier`` импортируется
лениво и только при включённом telegram в конфиге; пакета может не быть —
build обязан проходить и без него) и tkinter (см. ``excludes`` ниже).

datas — то, что код читает через ``Path(__file__)``: в замороженном бинарнике
``__file__`` указывает внутрь _MEIPASS, и без этих файлов демон не поднимет
схему БД, а диагностика не найдёт таблицу локалей. Файлы, которые legacy
относит к cwd (config.json, queries.txt, user_agents.txt), не пакуются — их
кладёт каталог запуска.
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Корень репозитория: spec лежит в нём же, поэтому и точка входа, и pathex
# считаются от SPECPATH, а не от cwd запуска pyinstaller.
REPO_ROOT = SPECPATH

hiddenimports = [
    # ветки диспетчера (сам скрипт bundle.py в граф не нужен: он уже
    # точка входа и выполняется как __main__)
    "engine.worker",
    "engine.control_plane.daemon",
    # legacy-цепочка: импортируется лениво (engine.worker -> ad_clicker ->
    # search_controller/webdriver), но единой точкой входа бинарника будет
    # байткод, а не эти имена — поэтому перечислены явно
    "ad_clicker",
    "search_controller",
    "webdriver",
    "config_reader",
    "logger",
    "utils",
    "proxy",
    "hooks",
    "stats",
    "clicklogs_db",
    "geolocation_db",
    "adb",
    # браузерный стек
    "undetected_chromedriver",
    # requests.compat делает importlib.import_module("charset_normalizer")
    "charset_normalizer",
]

# Пакеты целиком: collect_submodules сканирует файлы пакета и не зависит от
# того, видит ли modulegraph конкретный импорт.
# engine.bundle исключён из выборки: точка входа уже подключена как скрипт,
# второй экземпляр модуля в архиве ничего не даёт.
hiddenimports += [
    name for name in collect_submodules("engine") if name != "engine.bundle"
]
hiddenimports += collect_submodules("selenium")
hiddenimports += collect_submodules("undetected_chromedriver")

datas = [
    # schema.sql и миграции читаются как Path(__file__).parent / ...
    # (engine/db/migrations.py), то же для country_to_locale.json из
    # engine/diagnostics.py: Path(__file__).parents[1] — корень бандла
    *collect_data_files("engine.db", includes=["*.sql", "migrations/*.sql"]),
    (os.path.join(REPO_ROOT, "country_to_locale.json"), "."),
]

# mouseinfo (транзитивная зависимость pyautogui) импортирует tkinter на
# верхнем уровне, а Tk GUI из проекта удалён и нигде не используется.
# Без исключения сборка падает: hook-_tkinter требует tcl/tk data, которых в
# nix dev-shell нет. В рантайме pyautogui ловит ImportError от mouseinfo сам
# (функция mouseInfo становится заглушкой) — это его штатный путь.
excludes = ["mouseinfo", "tkinter", "_tkinter"]

a = Analysis(
    [os.path.join(REPO_ROOT, "engine", "bundle.py")],
    pathex=[REPO_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
