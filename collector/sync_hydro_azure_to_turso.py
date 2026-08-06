#!/usr/bin/env python3
"""
Einmaliges Sync-Skript: kopiert die komplette Historie aus Azure SQL
(dbo.hydro_readings) nach Turso (hydro_readings), damit Turso die gleiche
Datenbasis hat wie Azure. Idempotent (UPSERT) - kann gefahrlos mehrfach
laufen, ueberschreibt nur mit denselben Werten.

Nach diesem einmaligen Abgleich bleiben beide Datenbanken von selbst
synchron, da collect_hydro.py (Azure) und collect_hydro_turso.py (Turso)
dieselbe LINDAS-Quelle im gleichen Rhythmus abfragen.

Benoetigte Umgebungsvariablen (bereits als Secrets vorhanden):
  AZURE_SQL_SERVER, AZURE_SQL_DATABASE, AZURE_SQL_USER, AZURE_SQL_PASSWORD
  TURSO_DATABASE_URL, TURSO_AUTH_TOKEN
"""

from __future__ import annotations

import os
import sys
import time

import pymssql
import libsql


def get_azure_connection(max_attempts: int = 6) -> pymssql.Connection:
    server = os.environ["AZURE_SQL_SERVER"]
    database = os.environ["AZURE_SQL_DATABASE"]
    user = os.environ["AZURE_SQL_USER"]
    password = os.environ["AZURE_SQL_PASSWORD"]

    delays = [5, 10, 20, 30, 45, 60][: max_attempts - 1]
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return pymssql.connect(
                server=server, database=database, user=user, password=password,
                timeout=60, login_timeout=30,
            )
        except pymssql.exceptions.OperationalError as exc:
            last_error = exc
            if attempt == max_attempts:
                break
            wait = delays[attempt - 1]
            print(
                f"WARNUNG: Azure-Verbindung fehlgeschlagen (Versuch {attempt}/{max_attempts}): {exc}\n"
                f"  -> warte {wait}s und versuche erneut.",
                file=sys.stderr,
            )
            time.sleep(wait)
    raise last_error  # noqa: RSE102


def get_turso_connection():
    url = os.environ["TURSO_DATABASE_URL"]
    token = os.environ["TURSO_AUTH_TOKEN"]
    return libsql.connect(database=url, auth_token=token)


def fetch_azure_hydro_readings(conn: pymssql.Connection) -> list[dict]:
    cursor = conn.cursor(as_dict=True)
    cursor.execute("""
        SELECT station_id, reading_time, discharge_m3s, water_level_m, water_temp_c
        FROM dbo.hydro_readings
        ORDER BY station_id, reading_time
    """)
    return cursor.fetchall()


def ensure_turso_schema(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hydro_readings (
            station_id TEXT NOT NULL,
            reading_time TEXT NOT NULL,
            discharge_m3s REAL,
            water_level_m REAL,
            water_temp_c REAL,
            inserted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (station_id, reading_time)
        )
    """)
    conn.commit()


def sync_to_turso(conn, rows: list[dict]) -> int:
    written = 0
    for row in rows:
        station_id = row["station_id"]
        reading_time = row["reading_time"]
        # pymssql liefert DATETIME2 typischerweise als datetime-Objekt;
        # Turso-Spalte ist TEXT (ISO 8601), also konsistent formatieren.
        reading_time_str = reading_time.isoformat() if hasattr(reading_time, "isoformat") else str(reading_time)

        discharge = float(row["discharge_m3s"]) if row["discharge_m3s"] is not None else None
        water_level = float(row["water_level_m"]) if row["water_level_m"] is not None else None
        water_temp = float(row["water_temp_c"]) if row["water_temp_c"] is not None else None

        conn.execute(
            """
            INSERT INTO hydro_readings
                (station_id, reading_time, discharge_m3s, water_level_m, water_temp_c)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(station_id, reading_time) DO UPDATE SET
                discharge_m3s = excluded.discharge_m3s,
                water_level_m = excluded.water_level_m,
                water_temp_c = excluded.water_temp_c
            """,
            (station_id, reading_time_str, discharge, water_level, water_temp),
        )
        written += 1
        if written % 200 == 0:
            print(f"  ... {written}/{len(rows)} Messwerte synchronisiert", file=sys.stderr)

    conn.commit()
    return written


def main() -> None:
    print("Lese Hydro-Historie aus Azure SQL ...")
    azure_conn = get_azure_connection()
    try:
        rows = fetch_azure_hydro_readings(azure_conn)
    finally:
        azure_conn.close()

    print(f"{len(rows)} Messwerte in Azure gefunden.")
    if not rows:
        print("Nichts zu synchronisieren, beende.")
        return

    print("Schreibe nach Turso ...")
    turso_conn = get_turso_connection()
    ensure_turso_schema(turso_conn)
    written = sync_to_turso(turso_conn, rows)

    print(f"Fertig. {written} Messwerte von Azure nach Turso synchronisiert.")


if __name__ == "__main__":
    main()
