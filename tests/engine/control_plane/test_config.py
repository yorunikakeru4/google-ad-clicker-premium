"""Тесты конфигурации control plane.

Конфиг — единственный способ настроить демон снаружи, поэтому проверяется всё,
что может тихо сломать работу: типы, диапазоны, перекрёстные ограничения
(min <= max), неизвестные ключи и маскирование секретов.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from engine.captcha_policy import CAPTCHA_POLICIES, DEFAULT_CAPTCHA_POLICY
from engine.captcha_threshold import (
    CAPTCHA_THRESHOLD_ACTIONS,
    DEFAULT_CAPTCHA_THRESHOLD_ACTION,
    DEFAULT_CAPTCHA_THRESHOLD_PERCENT,
)
from engine.control_plane import config as config_module
from engine.control_plane.config import Config, ConfigError, validate_settings
from engine.proxy_auth import DEFAULT_PROXY_TRANSPORT, PROXY_TRANSPORTS

SECRET_MASK = config_module.SECRET_MASK


def _raw(**overrides):
    base = config_module.default_config()
    for dotted, value in overrides.items():
        section, _, key = dotted.partition("__")
        base[section][key] = value
    return base


class TestDefaults:
    """Значения по умолчанию повторяют config.json из корня репозитория."""

    def test_default_has_same_shape_as_repository_config_json(self):
        """Совпадает структура: секции, ключи и типы.

        Не значения: в репозиторном config.json пути прописаны руками под
        машину автора (/home/coskun/...), и копировать их в умолчания демона
        нельзя. Демон на чужой машине обязан стартовать без правки конфига.
        """
        repo_config = json.loads(
            (config_module.Path(__file__).parents[3] / "config.json").read_text(encoding="utf-8")
        )
        defaults = config_module.default_config()

        assert set(defaults) == set(repo_config)
        for section, fields in defaults.items():
            assert set(fields) == set(repo_config[section])
            for key, value in fields.items():
                assert isinstance(value, type(repo_config[section][key])) or value == ""

    def test_default_does_not_borrow_machine_specific_paths_from_repo_config(self):
        defaults = config_module.default_config()

        assert defaults["paths"]["query_file"] == ""
        assert defaults["paths"]["proxy_file"] == ""

    def test_default_worker_count_is_two_like_legacy(self):
        assert config_module.default_config()["behavior"]["browser_count"] == 2

    def test_default_config_is_independent_between_calls(self):
        first = config_module.default_config()
        first["behavior"]["browser_count"] = 7

        assert config_module.default_config()["behavior"]["browser_count"] == 2


class TestValidation:
    """Типы и диапазоны: неверное значение должно называть проблему, а не падать."""

    def test_accepts_known_values(self):
        cfg = Config.from_dict(_raw(behavior__browser_count=3))

        assert cfg.get("behavior.browser_count") == 3

    def test_rejects_string_where_int_expected(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__browser_count="три"))

        assert "behavior.browser_count" in str(excinfo.value)
        assert "int" in str(excinfo.value)

    def test_rejects_bool_where_int_expected(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__browser_count=True))

        assert "behavior.browser_count" in str(excinfo.value)

    def test_accepts_bool_where_bool_expected(self):
        cfg = Config.from_dict(_raw(behavior__random_mouse=True))

        assert cfg.get("behavior.random_mouse") is True

    def test_rejects_int_where_bool_expected(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__random_mouse=1))

        assert "behavior.random_mouse" in str(excinfo.value)

    def test_rejects_worker_count_below_minimum(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__browser_count=0))

        assert "behavior.browser_count" in str(excinfo.value)
        assert "1" in str(excinfo.value)

    def test_rejects_worker_count_above_maximum(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__browser_count=9))

        assert "behavior.browser_count" in str(excinfo.value)

    def test_rejects_negative_wait_time(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__ad_page_min_wait=-1))

        assert "behavior.ad_page_min_wait" in str(excinfo.value)

    def test_rejects_zero_wait_factor(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__wait_factor=0.0))

        assert "behavior.wait_factor" in str(excinfo.value)

    def test_accepts_integer_for_float_field(self):
        cfg = Config.from_dict(_raw(behavior__wait_factor=2))

        assert cfg.get("behavior.wait_factor") == 2.0
        assert isinstance(cfg.get("behavior.wait_factor"), float)

    def test_rejects_unknown_top_level_section(self):
        raw = _raw()
        raw["nonsense"] = {"a": 1}

        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(raw)

        assert "nonsense" in str(excinfo.value)

    def test_rejects_unknown_key_inside_section(self):
        raw = _raw()
        raw["behavior"]["browser_cnt"] = 3

        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(raw)

        assert "behavior.browser_cnt" in str(excinfo.value)

    def test_reports_every_problem_at_once(self):
        raw = _raw(behavior__browser_count=0, behavior__click_order=-5, webdriver__auth="yes")

        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(raw)

        problems = excinfo.value.problems
        assert len(problems) == 3
        assert {p["field"] for p in problems} == {
            "behavior.browser_count",
            "behavior.click_order",
            "webdriver.auth",
        }

    def test_rejects_non_object_section(self):
        raw = _raw()
        raw["behavior"] = ["browser_count", 2]

        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(raw)

        assert "behavior" in str(excinfo.value)

    def test_rejects_non_object_root(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict([1, 2, 3])

        assert "config" in str(excinfo.value)

    def test_rejects_malformed_interval(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__running_interval_start="25:00"))

        assert "behavior.running_interval_start" in str(excinfo.value)

    def test_accepts_empty_interval(self):
        cfg = Config.from_dict(_raw(behavior__running_interval_start="", behavior__running_interval_end=""))

        assert cfg.get("behavior.running_interval_start") == ""

    def test_rejects_interval_with_minutes_over_59(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__running_interval_start="10:75"))

        assert "behavior.running_interval_start" in str(excinfo.value)


class TestCrossFieldRules:
    """Ограничения между полями: их нельзя выразить по одному полю."""

    def test_rejects_ad_min_wait_greater_than_max(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__ad_page_min_wait=20, behavior__ad_page_max_wait=10))

        assert "behavior.ad_page_min_wait" in str(excinfo.value)
        assert "behavior.ad_page_max_wait" in str(excinfo.value)

    def test_rejects_equal_ad_min_and_max_is_allowed(self):
        cfg = Config.from_dict(_raw(behavior__ad_page_min_wait=10, behavior__ad_page_max_wait=10))

        assert cfg.get("behavior.ad_page_max_wait") == 10

    def test_rejects_nonad_min_wait_greater_than_max(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__nonad_page_min_wait=30, behavior__nonad_page_max_wait=20))

        assert "behavior.nonad_page_min_wait" in str(excinfo.value)

    def test_rejects_query_and_query_file_together(self):
        raw = _raw(paths__query_file="/tmp/q.txt")
        raw["behavior"]["query"] = "shoes"

        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(raw)

        assert "query" in str(excinfo.value)

    def test_rejects_proxy_and_proxy_file_together(self):
        raw = _raw(paths__proxy_file="/tmp/p.txt")
        raw["webdriver"]["proxy"] = "http://1.2.3.4:8080"

        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(raw)

        assert "proxy" in str(excinfo.value)

    def test_inverted_interval_is_allowed_because_night_range_is_legal(self):
        cfg = Config.from_dict(
            _raw(behavior__running_interval_start="23:00", behavior__running_interval_end="06:00")
        )

        assert cfg.get("behavior.running_interval_start") == "23:00"


class TestPatch:
    """Частичное обновление конфига из UI."""

    def test_patch_changes_only_given_field(self):
        cfg = Config.from_dict(_raw())

        updated = cfg.patch({"behavior": {"browser_count": 3}})

        assert updated.get("behavior.browser_count") == 3
        assert updated.get("behavior.click_order") == cfg.get("behavior.click_order")

    def test_patch_does_not_mutate_original(self):
        cfg = Config.from_dict(_raw())

        cfg.patch({"behavior": {"browser_count": 3}})

        assert cfg.get("behavior.browser_count") == 2

    def test_patch_rejects_unknown_key_without_changing_anything(self):
        cfg = Config.from_dict(_raw())

        with pytest.raises(ConfigError):
            cfg.patch({"behavior": {"nope": 1}})

        assert cfg.get("behavior.browser_count") == 2

    def test_patch_rejects_bad_value_and_keeps_old_config(self):
        cfg = Config.from_dict(_raw())

        with pytest.raises(ConfigError):
            cfg.patch({"behavior": {"browser_count": 99}})

        assert cfg.get("behavior.browser_count") == 2

    def test_patch_can_set_secret(self):
        cfg = Config.from_dict(_raw())

        updated = cfg.patch({"behavior": {"2captcha_apikey": "SECRET-KEY"}})

        assert updated.get("behavior.2captcha_apikey") == "SECRET-KEY"

    @pytest.mark.parametrize("dotted", sorted(config_module._SECRET_FIELDS))
    def test_patch_with_mask_keeps_existing_secret(self, dotted):
        """Маска из GET-ответа означает «не менять», а не новое значение.

        UI отдаёт ``to_dict()`` и может вернуть его же: до фикса
        ``_deep_merge`` клал бы маску как значение, и следующий ``save()``
        закреплял бы потерю ключа.
        """
        section, _, key = dotted.partition(".")
        cfg = Config.from_dict(_raw(**{f"{section}__{key}": "REAL-KEY"}))

        updated = cfg.patch({section: {key: SECRET_MASK}})

        assert updated.get(dotted) == "REAL-KEY"

    @pytest.mark.parametrize("dotted", sorted(config_module._SECRET_FIELDS))
    def test_patch_with_empty_string_clears_secret(self, dotted):
        """Очистка пустой строкой — осознанное действие и работает как раньше."""
        section, _, key = dotted.partition(".")
        cfg = Config.from_dict(_raw(**{f"{section}__{key}": "REAL-KEY"}))

        updated = cfg.patch({section: {key: ""}})

        assert updated.get(dotted) == ""

    @pytest.mark.parametrize("dotted", sorted(config_module._SECRET_FIELDS))
    def test_patch_with_mask_on_unset_secret_stays_unset(self, dotted):
        """Маска по незаданному секрету не создаёт literal-значение «********»."""
        section, _, key = dotted.partition(".")
        cfg = Config.from_dict(_raw())

        updated = cfg.patch({section: {key: SECRET_MASK}})

        assert updated.get(dotted) == ""

    def test_patch_of_unrelated_field_does_not_touch_secrets(self):
        cfg = Config.from_dict(
            _raw(behavior__2captcha_apikey="REAL-KEY", webdriver__proxy="http://user:pw@1.2.3.4:8080")
        )

        updated = cfg.patch({"behavior": {"click_order": 3}})

        assert updated.get("behavior.click_order") == 3
        assert updated.get("behavior.2captcha_apikey") == "REAL-KEY"
        assert updated.get("webdriver.proxy") == "http://user:pw@1.2.3.4:8080"

    def test_mask_patch_alone_leaves_config_unchanged(self):
        """Патч, целиком состоящий из маски, — конфиг без изменений."""
        cfg = Config.from_dict(
            _raw(behavior__2captcha_apikey="REAL-KEY", webdriver__proxy="http://user:pw@1.2.3.4:8080")
        )
        mask_patch: dict = {}
        for dotted in config_module._SECRET_FIELDS:
            section, _, key = dotted.partition(".")
            mask_patch.setdefault(section, {})[key] = SECRET_MASK

        assert cfg.patch(mask_patch).as_dict() == cfg.as_dict()

    def test_mask_is_special_only_for_fields_in_secret_list(self):
        """Правило «маска = не менять» действует ровно по ``_SECRET_FIELDS``.

        Несекретное поле получает строку маски как обычное значение: правило
        не разливается по всей схеме.
        """
        cfg = Config.from_dict(_raw(behavior__query="shoes"))

        updated = cfg.patch({"behavior": {"query": SECRET_MASK}})

        assert updated.get("behavior.query") == SECRET_MASK
        assert config_module._SECRET_FIELDS == frozenset(
            {"behavior.2captcha_apikey", "webdriver.proxy"}
        )


class TestSerialization:
    """Формат наружу: секреты не уезжают, JSON всегда читаем."""

    def test_to_dict_masks_secret(self):
        cfg = Config.from_dict(_raw(behavior__2captcha_apikey="REAL-KEY"))

        assert cfg.to_dict()["behavior"]["2captcha_apikey"] == SECRET_MASK

    def test_to_dict_keeps_everything_else(self):
        cfg = Config.from_dict(_raw(behavior__browser_count=3))

        dumped = cfg.to_dict()

        assert dumped["behavior"]["browser_count"] == 3
        assert set(dumped) == {"paths", "webdriver", "behavior"}

    def test_round_trip_through_dict_preserves_non_secret_values(self):
        original = Config.from_dict(_raw(behavior__browser_count=3, behavior__click_order=7))

        restored = Config.from_dict(original.to_dict())

        assert restored.to_dict() == original.to_dict()

    def test_to_json_is_valid_json(self):
        cfg = Config.from_dict(_raw())

        assert json.loads(cfg.to_json()) == cfg.to_dict()

    def test_get_missing_field_raises_keyerror(self):
        cfg = Config.from_dict(_raw())

        with pytest.raises(KeyError):
            cfg.get("behavior.no_such_field")

    def test_get_without_section_raises_keyerror(self):
        cfg = Config.from_dict(_raw())

        with pytest.raises(KeyError):
            cfg.get("nosuch.field")


class TestFileIO:
    """Чтение config.json с диска и запись изменённого конфига обратно."""

    def test_loads_repository_config(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps(_raw(behavior__browser_count=4)), encoding="utf-8")

        cfg = Config.load(path)

        assert cfg.get("behavior.browser_count") == 4

    def test_load_fills_missing_keys_with_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"behavior": {"browser_count": 5}}), encoding="utf-8")

        cfg = Config.load(path)

        assert cfg.get("behavior.browser_count") == 5
        assert cfg.get("behavior.click_order") == 5
        assert cfg.get("webdriver.auth") is True

    def test_load_rejects_invalid_json(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(ConfigError) as excinfo:
            Config.load(path)

        assert "config.json" in str(excinfo.value)

    def test_load_missing_file_uses_defaults(self, tmp_path):
        cfg = Config.load(tmp_path / "absent.json")

        assert cfg.to_dict() == config_module.default_config()

    @pytest.mark.parametrize("dotted", sorted(config_module._SECRET_FIELDS))
    def test_save_writes_real_secret_and_never_the_mask(self, tmp_path, dotted):
        """Файл — source of truth (план §1): save пишет настоящий ключ.

        Обратное поведение (маска в config.json) убивало ключ навсегда:
        после сохранения из UI и рестарта демон и legacy ``config_reader``
        читали строку ``********``. Маска остаётся средством для HTTP-ответов.
        """
        path = tmp_path / "config.json"
        section, _, key = dotted.partition(".")
        cfg = Config.from_dict(_raw(**{f"{section}__{key}": "REAL-KEY"}))

        cfg.save(path)
        text = path.read_text(encoding="utf-8")
        reloaded = Config.load(path)

        assert "REAL-KEY" in text
        assert SECRET_MASK not in text
        assert reloaded.get(dotted) == "REAL-KEY"

    def test_save_keeps_sorted_json_and_trailing_newline(self, tmp_path):
        path = tmp_path / "config.json"

        Config.from_dict(_raw(behavior__browser_count=3)).save(path)
        text = path.read_text(encoding="utf-8")

        assert text.endswith("}\n")
        parsed = json.loads(text)
        assert list(parsed) == sorted(parsed)
        assert list(parsed["behavior"]) == sorted(parsed["behavior"])

    def test_save_leaves_owner_only_permissions(self, tmp_path):
        path = tmp_path / "config.json"

        Config.from_dict(_raw(behavior__2captcha_apikey="REAL-KEY")).save(path)

        assert path.stat().st_mode & 0o777 == 0o600

    def test_save_is_atomic_and_leaves_no_temp_file(self, tmp_path):
        path = tmp_path / "config.json"
        cfg = Config.from_dict(_raw())

        cfg.save(path)

        assert sorted(p.name for p in tmp_path.iterdir()) == ["config.json"]


class TestProxyTransport:
    """``webdriver.proxy_transport``: enum из ``engine.proxy_auth``, не своя копия.

    Значения и дефолт берутся из того же модуля, что и ``resolve_proxy_transport``:
    два независимых словаря разъехались бы уже на четвёртом транспорте, а ошибка
    вылезла бы только на стенде, когда Chrome молча поехал бы не туда.
    """

    @pytest.mark.parametrize("value", ["cdp_auth", "extension", "direct"])
    def test_accepts_every_known_transport(self, value):
        cfg = Config.from_dict(_raw(webdriver__proxy_transport=value))

        assert cfg.get("webdriver.proxy_transport") == value

    def test_default_is_cdp_auth(self):
        assert config_module.default_config()["webdriver"]["proxy_transport"] == "cdp_auth"
        assert Config.from_dict(_raw()).get("webdriver.proxy_transport") == "cdp_auth"

    def test_default_matches_the_constant_from_proxy_auth(self):
        assert config_module.default_config()["webdriver"]["proxy_transport"] == (
            DEFAULT_PROXY_TRANSPORT
        )

    def test_config_without_the_key_keeps_working(self):
        """Конфиг старой версии: ключа нет, но демон стартует и читает значение."""
        raw = _raw()
        del raw["webdriver"]["proxy_transport"]

        assert Config.from_dict(raw).get("webdriver.proxy_transport") == "cdp_auth"

    def test_unknown_transport_is_a_readable_problem(self):
        raw = _raw(webdriver__proxy_transport="socks")

        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(raw)

        problems = excinfo.value.problems
        assert [problem["field"] for problem in problems] == ["webdriver.proxy_transport"]
        message = problems[0]["message"]
        assert "ожидается одно из" in message
        for value in PROXY_TRANSPORTS:
            assert value in message

    def test_non_string_transport_reports_the_expected_type(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(webdriver__proxy_transport=42))

        problems = excinfo.value.problems
        assert [problem["field"] for problem in problems] == ["webdriver.proxy_transport"]
        assert "str" in problems[0]["message"]

    def test_allowed_values_are_exactly_the_proxy_auth_set(self):
        """Страховка от второго места правды для словаря значений."""
        assert config_module._ENUM_FIELDS["webdriver.proxy_transport"] == PROXY_TRANSPORTS

    def test_patch_can_change_the_transport(self):
        cfg = Config.from_dict(_raw())

        patched = cfg.patch({"webdriver": {"proxy_transport": "direct"}})

        assert patched.get("webdriver.proxy_transport") == "direct"
        assert cfg.get("webdriver.proxy_transport") == "cdp_auth"


class TestCaptchaPolicy:
    """``behavior.captcha_policy``: enum из ``engine.captcha_policy``.

    Дефолт ``stop`` — план §5, фаза 8: при наличии ключа legacy раньше решал
    сам, теперь остановка ждёт оператора, а авто-решение включается явно.
    """

    @pytest.mark.parametrize("value", ["stop", "solve", "both"])
    def test_accepts_every_known_policy(self, value):
        cfg = Config.from_dict(_raw(behavior__captcha_policy=value))

        assert cfg.get("behavior.captcha_policy") == value

    def test_default_is_stop(self):
        assert config_module.default_config()["behavior"]["captcha_policy"] == "stop"
        assert Config.from_dict(_raw()).get("behavior.captcha_policy") == "stop"
        assert DEFAULT_CAPTCHA_POLICY == "stop"

    def test_config_without_the_key_keeps_working(self):
        """Конфиг старой версии: ключа нет, но демон стартует на дефолте."""

        raw = _raw()
        del raw["behavior"]["captcha_policy"]

        assert Config.from_dict(raw).get("behavior.captcha_policy") == "stop"

    def test_unknown_policy_is_a_readable_problem(self):
        raw = _raw(behavior__captcha_policy="wait")

        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(raw)

        problems = excinfo.value.problems
        assert [problem["field"] for problem in problems] == ["behavior.captcha_policy"]
        message = problems[0]["message"]
        assert "ожидается одно из" in message
        for value in CAPTCHA_POLICIES:
            assert value in message

    def test_non_string_policy_reports_the_expected_type(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__captcha_policy=42))

        problems = excinfo.value.problems
        assert problems[0]["field"] == "behavior.captcha_policy"
        assert "str" in problems[0]["message"]

    def test_allowed_values_are_exactly_the_captcha_policy_set(self):
        """Страховка от второго места правды для словаря значений."""

        assert config_module._ENUM_FIELDS["behavior.captcha_policy"] == CAPTCHA_POLICIES

    def test_validate_settings_reports_unknown_policy(self):
        problems = validate_settings(_raw(behavior__captcha_policy="hold"))

        assert [problem["field"] for problem in problems] == ["behavior.captcha_policy"]

    def test_patch_can_change_the_policy(self):
        cfg = Config.from_dict(_raw())

        patched = cfg.patch({"behavior": {"captcha_policy": "solve"}})

        assert patched.get("behavior.captcha_policy") == "solve"
        assert cfg.get("behavior.captcha_policy") == "stop"

class TestCaptchaThreshold:
    """``behavior.captcha_threshold_*``: процент и действие политики порога.

    Процент — доля CAPTCHA в процентах (0..100), действие — enum из
    ``engine.captcha_threshold``: значения и дефолты живут там, где их
    читает политика, иначе два места правды разъехались бы уже на четвёртом
    действии, а ошибка вылезла бы только на стенде.
    """

    @pytest.mark.parametrize("value", [0, 5, 50, 100])
    def test_accepts_percentages_inside_the_range(self, value):
        cfg = Config.from_dict(_raw(behavior__captcha_threshold_percent=value))

        assert cfg.get("behavior.captcha_threshold_percent") == value

    def test_default_percent_is_five(self):
        assert config_module.default_config()["behavior"]["captcha_threshold_percent"] == 5.0
        assert Config.from_dict(_raw()).get("behavior.captcha_threshold_percent") == 5.0

    def test_default_matches_the_constant_from_the_policy_module(self):
        assert config_module.default_config()["behavior"]["captcha_threshold_percent"] == (
            DEFAULT_CAPTCHA_THRESHOLD_PERCENT
        )

    def test_accepts_integer_for_the_percent_field(self):
        """JSON не различает 5 и 5.0 — то же правило, что и у ``wait_factor``."""
        cfg = Config.from_dict(_raw(behavior__captcha_threshold_percent=7))

        assert cfg.get("behavior.captcha_threshold_percent") == 7.0
        assert isinstance(cfg.get("behavior.captcha_threshold_percent"), float)

    @pytest.mark.parametrize("value", [101, -1, 100.5])
    def test_rejects_percentages_outside_the_range(self, value):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__captcha_threshold_percent=value))

        assert "behavior.captcha_threshold_percent" in str(excinfo.value)

    def test_rejects_non_numeric_percent(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__captcha_threshold_percent="пять"))

        problems = excinfo.value.problems
        assert [problem["field"] for problem in problems] == ["behavior.captcha_threshold_percent"]
        assert "float" in problems[0]["message"]

    def test_rejects_bool_where_percent_expected(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__captcha_threshold_percent=True))

        assert "behavior.captcha_threshold_percent" in str(excinfo.value)

    def test_config_without_the_keys_keeps_working(self):
        """Конфиг старой версии: ключей нет, но демон стартует на дефолтах."""
        raw = _raw()
        del raw["behavior"]["captcha_threshold_percent"]
        del raw["behavior"]["captcha_threshold_action"]

        cfg = Config.from_dict(raw)

        assert cfg.get("behavior.captcha_threshold_percent") == 5.0
        assert cfg.get("behavior.captcha_threshold_action") == "warn"

    @pytest.mark.parametrize("value", ["warn", "pause", "rotate"])
    def test_accepts_every_known_action(self, value):
        cfg = Config.from_dict(_raw(behavior__captcha_threshold_action=value))

        assert cfg.get("behavior.captcha_threshold_action") == value

    def test_default_action_is_warn(self):
        assert config_module.default_config()["behavior"]["captcha_threshold_action"] == "warn"
        assert Config.from_dict(_raw()).get("behavior.captcha_threshold_action") == "warn"

    def test_default_action_matches_the_policy_module(self):
        assert config_module.default_config()["behavior"]["captcha_threshold_action"] == (
            DEFAULT_CAPTCHA_THRESHOLD_ACTION
        )

    def test_unknown_action_is_a_readable_problem(self):
        raw = _raw(behavior__captcha_threshold_action="explode")

        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(raw)

        problems = excinfo.value.problems
        assert [problem["field"] for problem in problems] == ["behavior.captcha_threshold_action"]
        message = problems[0]["message"]
        assert "ожидается одно из" in message
        for value in CAPTCHA_THRESHOLD_ACTIONS:
            assert value in message

    def test_non_string_action_reports_the_expected_type(self):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_dict(_raw(behavior__captcha_threshold_action=42))

        problems = excinfo.value.problems
        assert [problem["field"] for problem in problems] == ["behavior.captcha_threshold_action"]
        assert "str" in problems[0]["message"]

    def test_allowed_actions_are_exactly_the_policy_set(self):
        """Страховка от второго места правды для словаря значений."""
        assert config_module._ENUM_FIELDS["behavior.captcha_threshold_action"] == (
            CAPTCHA_THRESHOLD_ACTIONS
        )

    def test_patch_can_change_threshold_and_action(self):
        cfg = Config.from_dict(_raw())

        patched = cfg.patch(
            {"behavior": {"captcha_threshold_percent": 12.5, "captcha_threshold_action": "pause"}}
        )

        assert patched.get("behavior.captcha_threshold_percent") == 12.5
        assert patched.get("behavior.captcha_threshold_action") == "pause"
        assert cfg.get("behavior.captcha_threshold_percent") == 5.0


class TestImportHasNoSideEffects:
    """Импорт конфигурации не должен открывать БД логов.

    Супервизор читает конфиг раньше, чем узнаёт, какую БД обслуживать
    (``--db``): если сам импорт создаёт ``StoreWriter``, демон оставляет
    ``adclicker.db`` в чужом каталоге и независимо от указанного пути. То же
    касается ``config_reader`` — его читает legacy-код.
    """

    def test_importing_config_modules_does_not_create_the_log_db(self, tmp_path):
        repo_root = Path(__file__).resolve().parents[3]
        shutil.copy(repo_root / "config.json", tmp_path / "config.json")
        env = dict(os.environ)
        # Путь к БД указываем явно: conftest прописывает его в окружении
        # pytest, и без переопределения файл создался бы вне tmp_path.
        env["ADCLICKER_DB"] = str(tmp_path / "unexpected-logger-db.db")
        env["PYTHONPATH"] = str(repo_root)

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import config_reader; import engine.control_plane.config",
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "unexpected-logger-db.db").exists()
        assert not (tmp_path / "adclicker.db").exists()
