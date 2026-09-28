"""Тесты конфигурации control plane.

Конфиг — единственный способ настроить демон снаружи, поэтому проверяется всё,
что может тихо сломать работу: типы, диапазоны, перекрёстные ограничения
(min <= max), неизвестные ключи и маскирование секретов.
"""

import json

import pytest

from engine.control_plane import config as config_module
from engine.control_plane.config import Config, ConfigError

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

    def test_save_writes_masked_secret_placeholder(self, tmp_path):
        path = tmp_path / "config.json"
        cfg = Config.from_dict(_raw(behavior__2captcha_apikey="REAL-KEY"))

        cfg.save(path)
        reloaded = Config.load(path)

        assert SECRET_MASK in path.read_text(encoding="utf-8")
        assert "REAL-KEY" not in path.read_text(encoding="utf-8")
        assert reloaded.get("behavior.2captcha_apikey") == SECRET_MASK

    def test_save_is_atomic_and_leaves_no_temp_file(self, tmp_path):
        path = tmp_path / "config.json"
        cfg = Config.from_dict(_raw())

        cfg.save(path)

        assert sorted(p.name for p in tmp_path.iterdir()) == ["config.json"]
