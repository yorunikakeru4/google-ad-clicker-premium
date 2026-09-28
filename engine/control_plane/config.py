"""Конфигурация control plane: загрузка, валидация, частичное обновление.

Формат взят из config.json в корне репозитория — тем же набором секций
``paths`` / ``webdriver`` / ``behavior``, чтобы демон и UI говорили о
настройках на одном языке, а не заводили второй, параллельный.

Два решения, которые стоит знать:

1. **Секреты маскируются на выходе.** ``behavior.2captcha_apikey`` и
   ``webdriver.proxy`` (там может быть логин:пароль) не попадают в
   ``to_dict()``/``to_json()``: наружу уходит ``SECRET_MASK``. Маска
   round-trip'ится, поэтому ``GET`` + ``POST`` того же конфига не затирает
   ключ пустым значением.
2. **Пустое значение значит "не задано"**, а не "выключено". Legacy-код
   различает эти случаи через ``if config.behavior.query:``, и валидация
   сохраняет это различие: пустая строка проходит проверку, а мусор — нет.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

# Значение, которым секрет заменяется в любом JSON наружу.
SECRET_MASK = "********"

# Схема: секция -> {ключ: (тип, значение по умолчанию)}.
#
# Типы намеренно узкие: bool проверяется до int, потому что isinstance(True, int)
# иначе молча пропустил бы True вместо 1 во всех целочисленных полях.
_SCHEMA: dict[str, dict[str, tuple[type | tuple[type, ...], Any]]] = {
    "paths": {
        "query_file": (str, ""),
        "proxy_file": (str, ""),
        "user_agents": (str, "user_agents.txt"),
        "filtered_domains": (str, "domains.txt"),
    },
    "webdriver": {
        "proxy": (str, ""),
        "auth": (bool, True),
        "incognito": (bool, False),
        "country_domain": (bool, False),
        "language_from_proxy": (bool, True),
        "ss_on_exception": (bool, False),
        "window_size": (str, ""),
        "shift_windows": (bool, False),
        "use_seleniumbase": (bool, False),
    },
    "behavior": {
        "query": (str, ""),
        "ad_page_min_wait": (int, 10),
        "ad_page_max_wait": (int, 15),
        "nonad_page_min_wait": (int, 15),
        "nonad_page_max_wait": (int, 20),
        "max_scroll_limit": (int, 0),
        "check_shopping_ads": (bool, True),
        "excludes": (str, ""),
        "random_mouse": (bool, False),
        "custom_cookies": (bool, False),
        "click_order": (int, 5),
        "browser_count": (int, 2),
        "multiprocess_style": (int, 1),
        "loop_wait_time": (int, 60),
        "wait_factor": (float, 1.0),
        "running_interval_start": (str, ""),
        "running_interval_end": (str, ""),
        "2captcha_apikey": (str, ""),
        "hooks_enabled": (bool, False),
        "telegram_enabled": (bool, False),
        "send_to_android": (bool, False),
        "request_boost": (bool, False),
    },
}

# Секреты: наружу уходят замаскированными.
_SECRET_FIELDS = frozenset({"behavior.2captcha_apikey", "webdriver.proxy"})

# browser_count: legacy трактует 0 как "столько, сколько ядер". Демон держит
# ту же договорённость, иначе конфиг из старой установки молча сменит смысл.
_MIN_WORKERS = 1
_MAX_WORKERS = 8

# Ночное окно 23:00-06:00 — норма, а не ошибка, поэтому сравнение концов
# интервала на "start < end" не делается вовсе.
_INTERVAL_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Диапазоны, которые нельзя вывести из типа: значение осмысленное, но
# запредельное (час ожидания вместо секунд, потолок кликов в миллион).
_MAX_WAIT_SECONDS = 3600
_MAX_LOOP_WAIT_SECONDS = 86_400
_MAX_CLICK_ORDER = 1000
_MAX_WAIT_FACTOR = 100.0


class ConfigError(ValueError):
    """Конфиг не проходит валидацию.

    ``problems`` — список словарей ``{"field", "message"}``, чтобы HTTP-слой
    мог отдать их в JSON как есть, не разбирая текст исключения.
    """

    def __init__(self, problems: list[dict[str, str]]):
        self.problems = problems
        detail = "; ".join(f"{p['field']}: {p['message']}" for p in problems)
        super().__init__(detail or "invalid config")


def default_config() -> dict[str, dict[str, Any]]:
    """Свежая копия значений по умолчанию.

    Копия строится каждый раз: разделяемый словарь-«эталон» позволил бы
    одному вызову patch() испортить значения для всех остальных.
    """
    return {section: {key: default for key, (_, default) in fields.items()} for section, fields in _SCHEMA.items()}


def _type_name(expected: type | tuple[type, ...]) -> str:
    if isinstance(expected, tuple):
        return "|".join(t.__name__ for t in expected)
    return expected.__name__


def _type_matches(value: Any, expected: type | tuple[type, ...]) -> bool:
    # bool — подкласс int, поэтому int-поля не принимают True/False.
    if expected is int or (isinstance(expected, tuple) and int in expected):
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected)


def _coerce(value: Any, expected: type | tuple[type, ...]) -> Any:
    """int -> float там, где поле объявлено как float.

    Конфиг пишут руками, и ``"wait_factor": 2`` не должно отвергаться из-за
    того, что JSON не различает 2 и 2.0.
    """
    if expected is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def _validate_field(field_path: str, value: Any, expected: type) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []

    if not _type_matches(value, expected):
        problems.append(
            {
                "field": field_path,
                "message": f"ожидается {_type_name(expected)}, получено {type(value).__name__}",
            }
        )
        return problems

    if isinstance(value, str) and field_path.endswith(("running_interval_start", "running_interval_end")):
        if value != "" and not _INTERVAL_RE.match(value):
            problems.append({"field": field_path, "message": "ожидается ЧЧ:ММ в пределах 00:00-23:59"})
        return problems

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        limits = _numeric_limits(field_path)
        if limits is not None:
            low, high = limits
            if value < low:
                problems.append(
                    {"field": field_path, "message": f"значение меньше минимума {_fmt_bound(low)}"}
                )
            elif value > high:
                problems.append(
                    {"field": field_path, "message": f"значение больше максимума {_fmt_bound(high)}"}
                )

    return problems


def _fmt_bound(value: float) -> str:
    # 5.0 -> "5", чтобы сообщение об ошибке не пугало лишним ".0".
    return str(int(value)) if float(value).is_integer() else str(value)


def _numeric_limits(field_path: str) -> tuple[float, float] | None:
    limits: dict[str, tuple[float, float]] = {
        "behavior.browser_count": (_MIN_WORKERS, _MAX_WORKERS),
        "behavior.ad_page_min_wait": (0, _MAX_WAIT_SECONDS),
        "behavior.ad_page_max_wait": (0, _MAX_WAIT_SECONDS),
        "behavior.nonad_page_min_wait": (0, _MAX_WAIT_SECONDS),
        "behavior.nonad_page_max_wait": (0, _MAX_WAIT_SECONDS),
        "behavior.click_order": (0, _MAX_CLICK_ORDER),
        "behavior.loop_wait_time": (0, _MAX_LOOP_WAIT_SECONDS),
        "behavior.wait_factor": (0.01, _MAX_WAIT_FACTOR),
        "behavior.max_scroll_limit": (0, _MAX_WAIT_SECONDS),
    }
    return limits.get(field_path)


def _validate_cross_field(data: dict[str, Any]) -> list[dict[str, str]]:
    """Правила, которые не выводятся из одного поля.

    Каждое возвращает обе стороны конфликта: UI подсвечивает оба поля, а не
    то, о котором код подумал первым.
    """
    problems: list[dict[str, str]] = []
    # Секция не-словарь уже отмечена в _validate; перекрёстные правила по ней
    # не проверяются, но и не роняют проверку с AttributeError.
    behavior = data.get("behavior")
    paths = data.get("paths")
    webdriver = data.get("webdriver")
    behavior = behavior if isinstance(behavior, dict) else {}
    paths = paths if isinstance(paths, dict) else {}
    webdriver = webdriver if isinstance(webdriver, dict) else {}

    for low, high in (
        ("ad_page_min_wait", "ad_page_max_wait"),
        ("nonad_page_min_wait", "nonad_page_max_wait"),
    ):
        low_value, high_value = behavior.get(low), behavior.get(high)
        if isinstance(low_value, (int, float)) and isinstance(high_value, (int, float)):
            if low_value > high_value:
                problems.append(
                    {
                        "field": f"behavior.{low}",
                        "message": f"должно быть не больше behavior.{high} ({high_value})",
                    }
                )
                problems.append(
                    {
                        "field": f"behavior.{high}",
                        "message": f"должно быть не меньше behavior.{low} ({low_value})",
                    }
                )

    # Наследие legacy: источник запросов и список файлов взаимоисключающи.
    # Оба сразу включены — значит пользователь не понял, какой из них главный,
    # и молчаливое разрешение этой путаницы хуже явной ошибки.
    if paths.get("query_file") and behavior.get("query"):
        problems.append(
            {"field": "behavior.query", "message": "paths.query_file уже задан — оставьте что-то одно"}
        )
        problems.append(
            {
                "field": "paths.query_file",
                "message": "behavior.query уже задан — оставьте что-то одно",
            }
        )

    if paths.get("proxy_file") and webdriver.get("proxy"):
        problems.append(
            {"field": "webdriver.proxy", "message": "paths.proxy_file уже задан — оставьте что-то одно"}
        )
        problems.append(
            {"field": "paths.proxy_file", "message": "webdriver.proxy уже задан — оставьте что-то одно"}
        )

    return problems


def _validate(data: dict[str, Any]) -> list[dict[str, str]]:
    if not isinstance(data, dict):
        return [{"field": "config", "message": "ожидается объект конфигурации"}]

    problems: list[dict[str, str]] = []

    for section, value in data.items():
        if section not in _SCHEMA:
            problems.append({"field": section, "message": "неизвестная секция конфигурации"})
            continue
        if not isinstance(value, dict):
            problems.append({"field": section, "message": "ожидается объект с параметрами"})
            continue
        for key, key_value in value.items():
            if key not in _SCHEMA[section]:
                problems.append(
                    {"field": f"{section}.{key}", "message": "неизвестный параметр конфигурации"}
                )
                continue
            expected = _SCHEMA[section][key][0]
            problems.extend(_validate_field(f"{section}.{key}", key_value, expected))

    problems.extend(_validate_cross_field(data))
    return problems


def _coerce_all(data: dict[str, Any]) -> dict[str, Any]:
    """Приводит значения к объявленным типам и дополняет недостающие ключи.

    Неизвестные ключи и ошибки сюда не доходят — это делает _validate.
    """
    result = default_config()
    for section, fields in _SCHEMA.items():
        provided = data.get(section)
        if not isinstance(provided, dict):
            continue
        for key, value in provided.items():
            expected = fields[key][0]
            result[section][key] = _coerce(value, expected)
    return result


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Слияние патча в копию base: секции сливаются по ключам, а не заменяются.

    Без этого UI, отправивший ``{"behavior": {"browser_count": 3}}``, стёр бы
    остальные 25 параметров поведения.
    """
    merged = {section: dict(values) for section, values in base.items()}
    for section, values in patch.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return merged


class Config:
    """Валидированный конфиг control plane.

    Экземпляр неизменяем: ``patch()`` возвращает новый, а текущий не трогает.
    Иначе неудачный запрос из UI мог бы оставить демон с наполовину применённым
    конфигом, и следующая валидация прошла бы уже не по тому, что прислали.
    """

    def __init__(self, data: dict[str, dict[str, Any]]):
        self._data = data

    @classmethod
    def from_dict(cls, data: Any) -> Config:
        """Валидирует и нормализует словарь конфига.

        Отсутствующие ключи заполняются значениями по умолчанию: конфиг из
        старой версии кликера не должен ломать демон из-за отсутствующего ключа,
    которого в этой версии ещё не было.
        """
        problems = _validate(data)
        if problems:
            raise ConfigError(problems)
        return cls(_coerce_all(data))

    @classmethod
    def load(cls, path: str | Path) -> Config:
        """Читает конфиг с диска. Отсутствующий файл — не ошибка.

        Свежая установка без config.json должна стартовать на значениях по
        умолчанию, а не требовать создания файла руками.
        """
        path = Path(path)
        if not path.is_file():
            return cls.from_dict(default_config())
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError([{"field": str(path), "message": f"не удалось прочитать конфиг: {exc}"}]) from exc
        return cls.from_dict(raw)

    def get(self, dotted_key: str) -> Any:
        section, _, key = dotted_key.partition(".")
        if section not in self._data or key not in self._data[section]:
            raise KeyError(dotted_key)
        return self._data[section][key]

    def as_dict(self) -> dict[str, dict[str, Any]]:
        """Копия внутренних данных без маскирования — только для кода.

        Наружу (HTTP, файл) уходит исключительно to_dict().
        """
        return {section: dict(values) for section, values in self._data.items()}

    def to_dict(self) -> dict[str, dict[str, Any]]:
        """Представление для JSON наружу: секреты заменены на маску."""
        plain = self.as_dict()
        for dotted in _SECRET_FIELDS:
            section, _, key = dotted.partition(".")
            if section in plain and plain[section].get(key):
                plain[section][key] = SECRET_MASK
        return plain

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def patch(self, patch_data: dict[str, Any]) -> Config:
        """Частичное обновление: возвращает новый Config, текущий не меняет.

        Валидация идёт по результату слияния, а не по патчу: patch может
        нарушить правило между полями (например, поднять min выше max) уже
        существующим значением соседа.
        """
        if not isinstance(patch_data, dict):
            raise ConfigError([{"field": "config", "message": "ожидается объект конфигурации"}])
        return Config.from_dict(_deep_merge(self._data, patch_data))

    def save(self, path: str | Path) -> None:
        """Атомарная запись: временный файл рядом и rename.

        Демон перезаписывает конфиг по запросу UI, и падение посреди записи
        оставило бы пользователя с нечитаемым config.json.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # mode 0o600: в маскированном виде секретов там нет, но сам файл
        # конфигурации пользователь считает чувствительным.
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self.to_json())
                handle.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except BaseException:
            os.unlink(tmp_name)
            raise
