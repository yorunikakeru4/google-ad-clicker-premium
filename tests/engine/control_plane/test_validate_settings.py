"""Тесты публичной точки входа валидации настроек: validate_settings().

UI (форма Tauri) присылает конфиг одним словарём и должен показать
список проблем ``{"field", "message"}`` прямо в форме: исключение или
SystemExit в пути сохранения превращаются в traceback вместо понятного
сообщения. validate_settings не бросает и не меняет входные данные; правила
типов, диапазонов, min <= max, ЧЧ:ММ и взаимной exclusivity берутся из
``_validate`` того же модуля — новое здесь только проверка существования
файлов из секции paths.
"""

import copy

import pytest

from engine.control_plane.config import default_config, validate_settings

# Значения по умолчанию для этих полей — относительные имена файлов, поэтому в
# базовом каталоге они должны лежать, иначе даже нетронутый конфиг не пройдёт
# проверку файлов.
DEFAULT_PATH_FILES = ("user_agents.txt", "domains.txt")


def _raw(**overrides):
    base = default_config()
    for dotted, value in overrides.items():
        section, _, key = dotted.partition("__")
        base[section][key] = value
    return base


def _fields(problems):
    return [problem["field"] for problem in problems]


def _base_dir(base_dir):
    """Каталог со всеми файлами, на которые ссылаются значения по умолчанию."""

    base_dir.mkdir(parents=True, exist_ok=True)
    for name in DEFAULT_PATH_FILES:
        (base_dir / name).write_text("placeholder\n", encoding="utf-8")
    return base_dir


class TestStructureAndTypes:
    """Типы и диапазоны: те же правила _validate, но в виде списка, а не исключения."""

    def test_valid_config_produces_no_problems(self, tmp_path):
        assert validate_settings(_raw(), base_dir=_base_dir(tmp_path)) == []

    def test_reports_type_mismatch_with_expected_type(self, tmp_path):
        problems = validate_settings(
            _raw(behavior__browser_count="три"), base_dir=_base_dir(tmp_path)
        )

        assert _fields(problems) == ["behavior.browser_count"]
        assert "int" in problems[0]["message"]

    @pytest.mark.parametrize("value", [0, 9])
    def test_rejects_worker_count_outside_range(self, tmp_path, value):
        problems = validate_settings(_raw(behavior__browser_count=value), base_dir=_base_dir(tmp_path))

        assert _fields(problems) == ["behavior.browser_count"]
        assert str(1 if value == 0 else 8) in problems[0]["message"]

    @pytest.mark.parametrize("value", [1, 8])
    def test_accepts_worker_count_on_boundary(self, tmp_path, value):
        assert validate_settings(_raw(behavior__browser_count=value), base_dir=_base_dir(tmp_path)) == []

    def test_reports_min_greater_than_max_on_both_fields(self, tmp_path):
        problems = validate_settings(
            _raw(behavior__ad_page_min_wait=30, behavior__ad_page_max_wait=10),
            base_dir=_base_dir(tmp_path),
        )

        assert _fields(problems) == ["behavior.ad_page_min_wait", "behavior.ad_page_max_wait"]
        by_field = {problem["field"]: problem["message"] for problem in problems}
        assert "behavior.ad_page_max_wait" in by_field["behavior.ad_page_min_wait"]
        assert "behavior.ad_page_min_wait" in by_field["behavior.ad_page_max_wait"]

    def test_reports_malformed_interval(self, tmp_path):
        problems = validate_settings(
            _raw(behavior__running_interval_start="25:00"), base_dir=_base_dir(tmp_path)
        )

        assert _fields(problems) == ["behavior.running_interval_start"]
        assert "ЧЧ:ММ" in problems[0]["message"]

    def test_reports_interval_minutes_over_59(self, tmp_path):
        problems = validate_settings(
            _raw(behavior__running_interval_end="10:75"), base_dir=_base_dir(tmp_path)
        )

        assert _fields(problems) == ["behavior.running_interval_end"]

    def test_accepts_empty_interval(self, tmp_path):
        assert (
            validate_settings(
                _raw(behavior__running_interval_start="", behavior__running_interval_end=""),
                base_dir=_base_dir(tmp_path),
            )
            == []
        )

    @pytest.mark.parametrize("value", ["cdp_auth", "extension", "direct"])
    def test_accepts_every_known_proxy_transport(self, tmp_path, value):
        assert (
            validate_settings(
                _raw(webdriver__proxy_transport=value), base_dir=_base_dir(tmp_path)
            )
            == []
        )

    def test_reports_unknown_proxy_transport_as_one_of(self, tmp_path):
        problems = validate_settings(
            _raw(webdriver__proxy_transport="socks"), base_dir=_base_dir(tmp_path)
        )

        assert _fields(problems) == ["webdriver.proxy_transport"]
        assert "ожидается одно из" in problems[0]["message"]
        assert "cdp_auth" in problems[0]["message"]

    def test_reports_non_string_proxy_transport(self, tmp_path):
        problems = validate_settings(
            _raw(webdriver__proxy_transport=True), base_dir=_base_dir(tmp_path)
        )

        assert _fields(problems) == ["webdriver.proxy_transport"]
        assert "str" in problems[0]["message"]


class TestExclusivity:
    """Взаимная exclusivity: каждая сторона конфликта получает своё сообщение."""

    def test_reports_both_sides_of_query_and_query_file_conflict(self, tmp_path):
        base = _base_dir(tmp_path)
        (base / "q.txt").write_text("usb hub\n", encoding="utf-8")
        data = _raw(paths__query_file="q.txt")
        data["behavior"]["query"] = "shoes"

        problems = validate_settings(data, base_dir=base)

        assert _fields(problems) == ["behavior.query", "paths.query_file"]

    def test_reports_both_sides_of_proxy_and_proxy_file_conflict(self, tmp_path):
        base = _base_dir(tmp_path)
        (base / "p.txt").write_text("127.0.0.1:8080\n", encoding="utf-8")
        data = _raw(paths__proxy_file="p.txt")
        data["webdriver"]["proxy"] = "http://1.2.3.4:8080"

        problems = validate_settings(data, base_dir=base)

        assert _fields(problems) == ["webdriver.proxy", "paths.proxy_file"]


class TestFileChecks:
    """Существование файлов: единственное правило, которого нет в Config.from_dict."""

    @pytest.mark.parametrize("field", ["query_file", "proxy_file", "user_agents", "filtered_domains"])
    def test_reports_missing_file_for_every_path_field(self, tmp_path, field):
        problems = validate_settings(
            _raw(**{f"paths__{field}": "absent.txt"}), base_dir=_base_dir(tmp_path)
        )

        assert problems == [{"field": f"paths.{field}", "message": "файл не найден: absent.txt"}]

    @pytest.mark.parametrize("field", ["query_file", "proxy_file", "user_agents", "filtered_domains"])
    def test_accepts_existing_file_for_every_path_field(self, tmp_path, field):
        base = _base_dir(tmp_path)
        (base / "present.txt").write_text("content\n", encoding="utf-8")

        assert validate_settings(_raw(**{f"paths__{field}": "present.txt"}), base_dir=base) == []

    def test_empty_path_means_not_set_and_is_not_checked(self, tmp_path):
        base = _base_dir(tmp_path)

        assert validate_settings(_raw(paths__query_file="", paths__proxy_file=""), base_dir=base) == []

    def test_reports_missing_absolute_path_as_written(self, tmp_path):
        missing = str(tmp_path / "absent-queries.txt")
        problems = validate_settings(_raw(paths__query_file=missing), base_dir=_base_dir(tmp_path))

        assert problems == [{"field": "paths.query_file", "message": f"файл не найден: {missing}"}]

    def test_relative_path_resolves_against_base_dir_not_cwd(self, tmp_path, monkeypatch):
        base = _base_dir(tmp_path / "config-dir")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (base / "q.txt").write_text("usb hub\n", encoding="utf-8")
        (elsewhere / "other.txt").write_text("usb hub\n", encoding="utf-8")
        monkeypatch.chdir(elsewhere)

        # Файл лежит в базовом каталоге, а не в cwd — проверка должна пройти.
        assert validate_settings(_raw(paths__query_file="q.txt"), base_dir=base) == []
        # Обратный случай: файл рядом с cwd не спасает, когда базовый каталог другой.
        problems = validate_settings(_raw(paths__query_file="other.txt"), base_dir=base)

        assert _fields(problems) == ["paths.query_file"]

    def test_default_base_dir_is_current_directory(self, tmp_path, monkeypatch):
        """Legacy читает config.json из cwd, поэтому без base_dir база — cwd."""

        _base_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        assert validate_settings(_raw()) == []

        (tmp_path / "domains.txt").unlink()

        assert validate_settings(_raw()) == [
            {"field": "paths.filtered_domains", "message": "файл не найден: domains.txt"}
        ]

    def test_directory_is_not_accepted_as_file(self, tmp_path):
        base = _base_dir(tmp_path)
        (base / "a-directory").mkdir()

        problems = validate_settings(_raw(paths__query_file="a-directory"), base_dir=base)

        assert problems == [{"field": "paths.query_file", "message": "файл не найден: a-directory"}]

    def test_non_string_path_value_reports_type_error_only(self, tmp_path):
        problems = validate_settings(_raw(paths__user_agents=42), base_dir=_base_dir(tmp_path))

        assert _fields(problems) == ["paths.user_agents"]
        assert "str" in problems[0]["message"]
        assert "файл не найден" not in problems[0]["message"]

    def test_missing_paths_section_skips_file_checks(self, tmp_path):
        data = _raw()
        del data["paths"]

        assert validate_settings(data, base_dir=tmp_path) == []

    def test_paths_section_of_wrong_type_reports_structure_problem_only(self, tmp_path):
        data = _raw()
        data["paths"] = ["user_agents.txt"]

        problems = validate_settings(data, base_dir=tmp_path)

        assert _fields(problems) == ["paths"]


class TestOrderingAndShape:
    """Формат списка: структура раньше файлов, без дублей, без исключений."""

    def test_structure_problems_come_before_file_problems(self, tmp_path):
        base = _base_dir(tmp_path)
        data = _raw(behavior__browser_count="три")
        data["paths"]["user_agents"] = "absent.txt"

        fields = _fields(validate_settings(data, base_dir=base))

        assert fields == ["behavior.browser_count", "paths.user_agents"]

    def test_every_problem_is_reported_exactly_once(self, tmp_path):
        base = _base_dir(tmp_path)
        data = _raw(behavior__browser_count=0, behavior__click_order=-5)
        data["paths"]["query_file"] = "absent.txt"

        fields = _fields(validate_settings(data, base_dir=base))

        assert sorted(fields) == sorted(set(fields))
        assert set(fields) == {"behavior.browser_count", "behavior.click_order", "paths.query_file"}

    def test_non_dict_input_returns_single_problem_without_crashing(self):
        assert validate_settings([1, 2, 3]) == [
            {"field": "config", "message": "ожидается объект конфигурации"}
        ]

    def test_input_data_is_not_modified(self, tmp_path):
        base = _base_dir(tmp_path)
        data = _raw(paths__query_file="absent.txt", behavior__browser_count=0)
        untouched = copy.deepcopy(data)

        validate_settings(data, base_dir=base)

        assert data == untouched


class TestLogSettingsValidation:
    """Поля хранения логов в публичной валидации: диапазоны и enum."""

    @pytest.mark.parametrize("value", [1, 30, 3650])
    def test_accepts_log_settings_with_valid_values(self, tmp_path, value):
        problems = validate_settings(
            _raw(
                behavior__log_retention_days=value,
                behavior__log_file_level="DEBUG",
                behavior__db_size_limit_mb=value,
            ),
            base_dir=_base_dir(tmp_path),
        )

        assert problems == []

    @pytest.mark.parametrize("value", [0, -1, 3651])
    def test_reports_retention_days_out_of_range(self, tmp_path, value):
        problems = validate_settings(
            _raw(behavior__log_retention_days=value), base_dir=_base_dir(tmp_path)
        )

        assert _fields(problems) == ["behavior.log_retention_days"]
        assert "минимума" in problems[0]["message"] or "максимума" in problems[0]["message"]

    def test_reports_unknown_log_file_level(self, tmp_path):
        problems = validate_settings(
            _raw(behavior__log_file_level="TRACE"), base_dir=_base_dir(tmp_path)
        )

        assert _fields(problems) == ["behavior.log_file_level"]
        assert "ожидается одно из" in problems[0]["message"]

    @pytest.mark.parametrize("value", [-1, 102401])
    def test_reports_db_size_limit_out_of_range(self, tmp_path, value):
        problems = validate_settings(
            _raw(behavior__db_size_limit_mb=value), base_dir=_base_dir(tmp_path)
        )

        assert _fields(problems) == ["behavior.db_size_limit_mb"]

    @pytest.mark.parametrize("field", ["log_retention_days", "log_file_level", "db_size_limit_mb"])
    def test_reports_wrong_type_for_each_field(self, tmp_path, field):
        problems = validate_settings(
            _raw(**{f"behavior__{field}": None}), base_dir=_base_dir(tmp_path)
        )

        assert _fields(problems) == [f"behavior.{field}"]


class TestCleanupSettingsValidation:
    """Поля очистки в публичной валидации: тот же _validate, список проблем."""

    @pytest.mark.parametrize("value", ["25:00", "4:00", ""])
    def test_reports_cleanup_time_problems(self, tmp_path, value):
        problems = validate_settings(
            _raw(behavior__cleanup_time=value), base_dir=_base_dir(tmp_path)
        )

        assert "behavior.cleanup_time" in _fields(problems)

    @pytest.mark.parametrize("value", [0, 31])
    def test_reports_cleanup_interval_days_out_of_range(self, tmp_path, value):
        problems = validate_settings(
            _raw(behavior__cleanup_interval_days=value), base_dir=_base_dir(tmp_path)
        )

        assert _fields(problems) == ["behavior.cleanup_interval_days"]

    def test_valid_cleanup_fields_produce_no_problems(self, tmp_path):
        raw = _raw(behavior__cleanup_time="04:00", behavior__cleanup_interval_days=1)

        assert validate_settings(raw, base_dir=_base_dir(tmp_path)) == []
