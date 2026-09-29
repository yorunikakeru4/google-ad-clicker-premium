from typing import Optional
from contextlib import contextmanager

import sqlite3

from engine.log import get_logger


log = get_logger()


DBCursor = sqlite3.Connection.cursor


class GeolocationDB:
    """SQLite database to keep latitude, longitude for IP

    Raises RuntimeError if database connection is not established.
    """

    def __init__(self) -> None:

        self._create_db_table()

    def save_geolocation(
        self, ip_address: str, latitude: str, longitude: str, country_code: str
    ) -> None:
        """Save IP, latitude, longitude to database

        Raises RuntimeError if an error occurs during the save operation.

        :type ip_address: str
        :param ip_address: IP address
        :type latitude: str
        :param latitude: Latitude for IP
        :type longitude: str
        :param longitude: Longitude for IP
        :type country_code: str
        :param country_code: Country code for IP
        """

        try:
            with self._geolocation_db() as geolocation_db_cursor:
                geolocation_db_cursor.execute(
                    "SELECT ip_address FROM geolocation WHERE ip_address=?", (ip_address,)
                )

                found = geolocation_db_cursor.fetchone()

                if not found:
                    geolocation_db_cursor.execute(
                        "INSERT INTO geolocation VALUES (?, ?, ?, ?)",
                        (ip_address, latitude, longitude, country_code),
                    )
                    log.debug(
                        "proxy",
                        "matching added to database.",
                        fields={
                            "ip_address": ip_address,
                            "latitude": latitude,
                            "longitude": longitude,
                            "country_code": country_code,
                        },
                    )

                else:
                    log.debug(
                        "proxy",
                        "already exists. Skipping...",
                        fields={
                            "ip_address": ip_address,
                            "latitude": latitude,
                            "longitude": longitude,
                            "country_code": country_code,
                        },
                    )

        except sqlite3.Error as exp:
            raise RuntimeError(exp) from exp

    def query_geolocation(self, ip_address: str) -> Optional[tuple[str, str, str]]:
        """Query given IP address in database and return latitude and longitude if exists

        :type ip_address: str
        :param ip_address: IP address
        :rtype: tuple
        :returns: (latitude, longitude, country_code) tuple for the given IP
        """

        try:
            with self._geolocation_db() as geolocation_db_cursor:
                geolocation_db_cursor.execute(
                    "SELECT ip_address, latitude, longitude, country_code FROM geolocation WHERE ip_address=?",
                    (ip_address,),
                )

                found = geolocation_db_cursor.fetchone()

                if not found:
                    log.debug(
                    "proxy",
                    "Couldn't find IP in database!",
                    fields={"ip_address": ip_address},
                )
                    return None
                else:
                    return (found[1], found[2], found[3])

        except sqlite3.Error as exp:
            raise RuntimeError(exp) from exp

    def _create_db_table(self) -> None:
        """Create table to store latitude, longitude for IP"""

        with self._geolocation_db() as geolocation_db_cursor:
            geolocation_db_cursor.execute(
                """CREATE TABLE IF NOT EXISTS geolocation (
                    ip_address TEXT PRIMARY KEY NOT NULL,
                    latitude TEXT NOT NULL,
                    longitude TEXT NOT NULL,
                    country_code TEXT NOT NULL
                );"""
            )

    @contextmanager
    def _geolocation_db(self) -> DBCursor:
        """Context manager that returns geolocation db cursor

        :rtype: sqlite3.Connection.cursor
        :returns: Database connection cursor
        """

        try:
            geolocation_db = sqlite3.connect("geolocation.db")
            yield geolocation_db.cursor()

        except sqlite3.Error as exp:
            log.error(
                "proxy",
                "Failed to connect to geolocation database",
                fields={"error": str(exp)},
            )
            raise RuntimeError("Failed to connect to geolocation database!") from exp

        finally:
            geolocation_db.commit()
            geolocation_db.close()
