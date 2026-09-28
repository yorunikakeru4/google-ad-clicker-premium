"""Тесты geolocation_db.GeolocationDB: кэш координат прокси-IP в SQLite."""

import pytest


def test_saved_geolocation_is_returned(make_geolocation_db):
    db = make_geolocation_db()

    db.save_geolocation("1.2.3.4", "52.52", "13.40", "DE")

    assert db.query_geolocation("1.2.3.4") == ("52.52", "13.40", "DE")


def test_unknown_ip_returns_none(make_geolocation_db):
    db = make_geolocation_db()
    db.save_geolocation("1.2.3.4", "52.52", "13.40", "DE")

    assert db.query_geolocation("5.6.7.8") is None


def test_query_on_empty_database_returns_none(make_geolocation_db):
    assert make_geolocation_db().query_geolocation("1.2.3.4") is None


def test_second_save_for_same_ip_does_not_overwrite(make_geolocation_db):
    db = make_geolocation_db()

    db.save_geolocation("1.2.3.4", "52.52", "13.40", "DE")
    db.save_geolocation("1.2.3.4", "0.0", "0.0", "US")

    assert db.query_geolocation("1.2.3.4") == ("52.52", "13.40", "DE")


def test_second_save_logs_skipping(make_geolocation_db, caplog):
    db = make_geolocation_db()
    db.save_geolocation("1.2.3.4", "52.52", "13.40", "DE")
    caplog.clear()

    db.save_geolocation("1.2.3.4", "0.0", "0.0", "US")

    assert "already exists. Skipping..." in caplog.text


def test_coordinates_are_stored_as_given_strings(make_geolocation_db):
    db = make_geolocation_db()

    db.save_geolocation("1.2.3.4", "52.5200", "13.4050", "DE")

    latitude, longitude, country_code = db.query_geolocation("1.2.3.4")
    assert latitude == "52.5200"
    assert longitude == "13.4050"
    assert country_code == "DE"


def test_several_ips_are_stored_independently(make_geolocation_db):
    db = make_geolocation_db()
    db.save_geolocation("1.2.3.4", "52.52", "13.40", "DE")
    db.save_geolocation("5.6.7.8", "48.85", "2.35", "FR")

    assert db.query_geolocation("1.2.3.4") == ("52.52", "13.40", "DE")
    assert db.query_geolocation("5.6.7.8") == ("48.85", "2.35", "FR")


def test_data_survives_reopening_of_the_database(make_geolocation_db):
    db = make_geolocation_db()
    db.save_geolocation("1.2.3.4", "52.52", "13.40", "DE")

    assert make_geolocation_db().query_geolocation("1.2.3.4") == ("52.52", "13.40", "DE")


@pytest.mark.xfail(
    reason=(
        "Баг legacy-кода: контекст-менеджер _geolocation_db в блоке finally "
        "обращается к переменной, которой ещё нет, если connect() упал, "
        "поэтому вместо задокументированного RuntimeError вылетает "
        "UnboundLocalError: cannot access local variable"
    ),
    strict=True,
)
def test_database_connection_failure_should_raise_runtime_error(isolated_cwd, make_geolocation_db):
    # Каталог вместо файла БД заставляет sqlite3.connect() упасть.
    (isolated_cwd / "geolocation.db").mkdir()

    with pytest.raises(RuntimeError) as excinfo:
        make_geolocation_db().save_geolocation("1.2.3.4", "52.52", "13.40", "DE")

    assert "Failed to connect to geolocation database!" in str(excinfo.value)
