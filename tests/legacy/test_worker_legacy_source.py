"""Тесты legacy-моста воркера: ``_LegacySource`` поверх config_reader/utils/proxy.

Юнит-тесты ``engine/worker.py`` идут через фейковый источник и проверяют цикл;
здесь проверяется то, что цикл не может проверить сам — как именно мост
превращает отказы legacy (``SystemExit``, ``OSError``, мусор в конфиге) в
``SourceError``. Без этих тестов обе починки (пустой путь, JSON-``null``,
не-строка) жили бы только в коде, а регрессию сьют бы не заметил: ``TypeError``
и ``IsADirectoryError`` вышли бы из ``run()`` и выглядели бы как падение
процесса, а не как ошибка конфигурации.
"""

import pytest

from engine.worker import SourceError, legacy_source


@pytest.fixture
def source():
    return legacy_source()


class TestQueries:
    """Чтение списка запросов: файл, пусто, мусор, отсутствие."""

    def test_reads_the_configured_file(self, source, set_paths, sandbox_dir):
        set_paths(query_file=str(sandbox_dir / "queries.txt"))

        assert source.queries() == ["wireless keyboard", "bluetooth headphones"]

    def test_unset_query_file_is_a_source_error(self, source, set_paths):
        set_paths(query_file="")

        with pytest.raises(SourceError, match="query_file"):
            source.queries()

    def test_json_null_query_file_is_a_source_error(self, source, set_paths):
        set_paths(query_file=None)

        with pytest.raises(SourceError, match="query_file"):
            source.queries()

    def test_non_string_query_file_is_a_source_error(self, source, set_paths):
        set_paths(query_file=123)

        with pytest.raises(SourceError, match="строкой"):
            source.queries()

    def test_missing_file_is_a_source_error(self, source, set_paths, tmp_path):
        set_paths(query_file=str(tmp_path / "no-such-queries.txt"))

        with pytest.raises(SourceError, match="query_file"):
            source.queries()


class TestProxies:
    """Чтение прокси: файл, одиночный из конфига, пусто, мусор, отсутствие."""

    def test_reads_the_configured_file(self, source, set_paths, sandbox_dir):
        set_paths(proxy_file=str(sandbox_dir / "proxies.txt"))

        assert source.proxies() == ["127.0.0.1:8080", "user:pass@10.0.0.1:3128"]

    def test_single_proxy_from_webdriver_section_when_there_is_no_file(
        self, source, set_paths, config, monkeypatch
    ):
        set_paths(proxy_file="")
        monkeypatch.setattr(config.webdriver, "proxy", "user:pass@10.0.0.1:3128")

        assert source.proxies() == ["user:pass@10.0.0.1:3128"]

    def test_no_proxy_configured_yields_an_empty_list(
        self, source, set_paths, config, monkeypatch
    ):
        set_paths(proxy_file="")
        monkeypatch.setattr(config.webdriver, "proxy", "")

        assert source.proxies() == []

    def test_json_null_proxy_file_falls_back_to_the_single_proxy(
        self, source, set_paths, config, monkeypatch
    ):
        set_paths(proxy_file=None)
        monkeypatch.setattr(config.webdriver, "proxy", "10.0.0.1:8080")

        assert source.proxies() == ["10.0.0.1:8080"]

    def test_non_string_proxy_file_is_a_source_error(self, source, set_paths):
        set_paths(proxy_file=True)

        with pytest.raises(SourceError, match="строкой"):
            source.proxies()

    def test_non_string_single_proxy_is_a_source_error(
        self, source, set_paths, config, monkeypatch
    ):
        set_paths(proxy_file="")
        monkeypatch.setattr(config.webdriver, "proxy", 42)

        with pytest.raises(SourceError, match="строкой"):
            source.proxies()

    def test_missing_file_is_a_source_error(self, source, set_paths, tmp_path):
        set_paths(proxy_file=str(tmp_path / "no-such-proxies.txt"))

        with pytest.raises(SourceError, match="proxy_file"):
            source.proxies()

    def test_env_proxy_wins_over_every_legacy_source(
        self, source, set_paths, sandbox_dir, config, monkeypatch
    ):
        """Супервизор назначает прокси через env — файл и конфиг не главнее."""
        set_paths(proxy_file=str(sandbox_dir / "proxies.txt"))
        monkeypatch.setattr(config.webdriver, "proxy", "10.0.0.1:8080")
        monkeypatch.setenv("ADCLICKER_PROXY", "user:pass@10.0.0.1:9999")

        assert source.proxies() == ["user:pass@10.0.0.1:9999"]

    def test_empty_env_keeps_the_legacy_sources(
        self, source, set_paths, sandbox_dir, monkeypatch
    ):
        set_paths(proxy_file=str(sandbox_dir / "proxies.txt"))
        monkeypatch.setenv("ADCLICKER_PROXY", "")

        assert source.proxies() == ["127.0.0.1:8080", "user:pass@10.0.0.1:3128"]


class TestReloadSettings:
    """Перечитывание конфига: норма и отказ."""

    def test_reads_scheduling_values_from_config_json(self, source, make_config):
        make_config()

        settings = source.reload_settings()

        assert settings.interval_start == "00:00"
        assert settings.interval_end == "00:00"
        assert settings.loop_wait_time == 60
        assert settings.browser_count == 2
        assert settings.multiprocess_style == 1
        assert settings.send_to_android is False

    def test_broken_config_json_is_a_source_error(self, source, tmp_path, monkeypatch):
        # Не make_config: та создаёт экземпляр ConfigReader и сама упала бы
        # на чтении, не дав мосту дойти до своего перехвата.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.json").write_text("{ это не json", encoding="utf-8")

        with pytest.raises(SourceError):
            source.reload_settings()

    def test_missing_config_file_is_a_source_error(self, source, tmp_path, monkeypatch):
        # FileNotFoundError — отдельная ветка от JSONDecodeError: обе обязаны
        # стать SourceError, но сообщения у них разные, и UI их различает.
        monkeypatch.chdir(tmp_path)  # каталог без config.json

        with pytest.raises(SourceError, match="config.json не читается"):
            source.reload_settings()
