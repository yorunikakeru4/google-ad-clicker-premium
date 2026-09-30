"""Подготовка chromedriver (``engine/driver_bin``) — контракт под e2e.

Поймано живым прогоном: UC-патч CfT-драйвера ломает ``maximize_window``
(``'Runtime.evaluate' wasn't found``), а UC-качка последнего релиза даёт
драйвер 154 под Chromium 153 (``session not created``). Здесь — спецификация
подготовки: маркер вместо вредного патча, скачивание под версию браузера,
атомарность и идемпотентность.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from engine.driver_bin import (
    UC_MARKER,
    _append_marker,
    browser_version,
    cft_platform,
    prepare_driver,
    resolve_chromedriver_url,
)


class _VersionResult:
    def __init__(self, stdout: str = "", stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr


def test_browser_version_parses_chromium_and_chrome_output() -> None:
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return _VersionResult(stdout="Chromium 153.0.8010.36 built on Debian\n")

    assert browser_version("/usr/bin/chromium", runner=runner) == "153.0.8010.36"
    assert calls[0][0] == "/usr/bin/chromium"

    def runner_google(cmd, **kwargs):
        return _VersionResult(stdout="Google Chrome for Testing 154.0.8037.92\n")

    assert browser_version("chrome", runner=runner_google) == "154.0.8037.92"


def test_browser_version_without_version_is_readable_error() -> None:
    with pytest.raises(RuntimeError, match="нет версии"):
        browser_version("weird", runner=lambda *a, **k: _VersionResult(stdout="no digits here"))


def test_cft_platform_is_linux64_here(monkeypatch) -> None:
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    assert cft_platform() == "linux64"


def test_resolve_prefers_exact_version() -> None:
    seen_text: list[str] = []

    def fetch_bytes(url: str) -> bytes:
        return b"zip"

    def fetch_text(url: str) -> str:
        seen_text.append(url)
        raise AssertionError("каталог не должен читаться при точном совпадении")

    url = resolve_chromedriver_url(
        "153.0.8010.36", fetch_bytes=fetch_bytes, fetch_text=fetch_text
    )
    assert url.endswith("/153.0.8010.36/chromedriver-linux64.zip")
    assert seen_text == []


def test_resolve_falls_back_to_same_major_from_catalog() -> None:
    catalog = {
        "versions": [
            {
                "version": "152.0.0.0",
                "downloads": {
                    "chromedriver": [
                        {"platform": "linux64", "url": "https://example/152.zip"}
                    ]
                },
            },
            {
                "version": "153.0.8010.52",
                "downloads": {
                    "chromedriver": [
                        {"platform": "linux64", "url": "https://example/153-8010.52.zip"}
                    ]
                },
            },
            {
                "version": "154.0.8037.92",
                "downloads": {
                    "chromedriver": [
                        {"platform": "linux64", "url": "https://example/154.zip"}
                    ]
                },
            },
        ]
    }

    def fetch_bytes(url: str) -> bytes:
        raise OSError("404: точной версии нет в каталоге CfT")

    def fetch_text(url: str) -> str:
        return json.dumps(catalog)

    url = resolve_chromedriver_url(
        "153.0.8010.36", fetch_bytes=fetch_bytes, fetch_text=fetch_text
    )
    # major 153, последняя подходящая версия каталога — 153.0.8010.52.
    assert url == "https://example/153-8010.52.zip"


def test_resolve_raises_when_catalog_has_no_matching_major() -> None:
    def fetch_bytes(url: str) -> bytes:
        raise OSError("404")

    def fetch_text(url: str) -> str:
        return json.dumps({"versions": [{"version": "160.0.0.0", "downloads": {}}]})

    with pytest.raises(RuntimeError, match="major 153"):
        resolve_chromedriver_url(
            "153.0.8010.36", fetch_bytes=fetch_bytes, fetch_text=fetch_text
        )


def test_append_marker_is_idempotent_and_keeps_payload(tmp_path) -> None:
    target = tmp_path / "chromedriver"
    target.write_bytes(b"ELF-payload")

    _append_marker(target)
    data = target.read_bytes()
    assert data.startswith(b"ELF-payload")
    assert data.count(UC_MARKER) == 1

    _append_marker(target)
    assert target.read_bytes().count(UC_MARKER) == 1


def _fake_zip(member: str = "chromedriver-linux64/chromedriver", payload: bytes = b"#!/bin/sh\n") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, payload)
    return buffer.getvalue()


def test_prepare_existing_file_only_marks_it(tmp_path) -> None:
    target = tmp_path / "chromedriver"
    target.write_bytes(b"payload")

    def no_fetch(url: str):
        raise AssertionError("сеть не должна открываться: файл уже на месте")

    result = prepare_driver(target, fetch_bytes=no_fetch, fetch_text=no_fetch)
    assert result == target
    assert UC_MARKER in target.read_bytes()


def test_prepare_downloads_unpacks_marks_and_is_executable(tmp_path) -> None:
    target = tmp_path / "chromedriver"
    requests: list[str] = []

    def fetch_bytes(url: str) -> bytes:
        requests.append(url)
        return _fake_zip(payload=b"#!/bin/sh\necho driver\n")

    def fetch_text(url: str) -> str:
        raise AssertionError("каталог не нужен: точная версия доступна")

    prepare_driver(
        target,
        fetch_bytes=fetch_bytes,
        fetch_text=fetch_text,
        browser_ver=lambda: "153.0.8010.36",
    )

    assert target.is_file()
    assert b"echo driver" in target.read_bytes()
    assert UC_MARKER in target.read_bytes()
    assert target.stat().st_mode & 0o111, "драйвер должен быть исполняемым"
    assert requests and requests[0].endswith("/153.0.8010.36/chromedriver-linux64.zip")
    # В tmp_path лежат и файлы песочницы legacy-фикстур — смотрим только
    # артефакты подготовки драйвера: рядом не должно остаться .tmp-копий.
    leftovers = [
        p.name
        for p in tmp_path.iterdir()
        if p.name.startswith("chromedriver") and p.name != "chromedriver"
    ]
    assert leftovers == [], f"временные файлы не убраны: {leftovers}"


def test_prepare_zip_without_driver_is_readble_error(tmp_path) -> None:
    target = tmp_path / "chromedriver"

    def fetch_bytes(url: str) -> bytes:
        return _fake_zip(member="README.txt", payload=b"no driver here")

    with pytest.raises(RuntimeError, match="в zip нет chromedriver"):
        prepare_driver(
            target,
            fetch_bytes=fetch_bytes,
            fetch_text=fetch_bytes,
            browser_ver=lambda: "153.0.8010.36",
        )
