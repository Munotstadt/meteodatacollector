#!/usr/bin/env python3
"""
MeteoDataCollector - Sammelt Tageswerte der MeteoSchweiz-Station
Zürich-Kloten (KLO) und schreibt sie direkt in eine Azure SQL Database
(Tabelle dbo.klo_daily, ein UPSERT pro Tag).

Quelle: MeteoSchweiz Open Government Data (OGD-SMN)
https://opendatadocs.meteoswiss.ch/a-data-groundbased/a1-automatic-weather-stations
Nutzungsbedingungen: Quellenangabe "Source: MeteoSchweiz" erforderlich.

Benoetigte Umgebungsvariablen (als GitHub Secrets gesetzt):
  AZURE_SQL_SERVER    z.B. sql-munotstadt-meteo.database.windows.net
  AZURE_SQL_DATABASE  z.B. MeteoDB
  AZURE_SQL_USER      SQL-Login (Admin-User aus der Server-Einrichtung)
  AZURE_SQL_PASSWORD  Passwort dazu
"""

from __future__ import annotations

import csv
import io
import os
import sys
from datetime import datetime

import urllib.request
import pymssql

STATION = "klo"  # Zuerich-Kloten
BASE_URL = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/{STATION}"
RECENT_URL = f"{BASE_URL}/ogd-smn_{STATION}_d_recent.csv"
HISTORICAL_URL = f"{BASE_URL}/ogd-smn_{STATION}_d_historical.csv"

# Parameter-Codes fuer Tageswerte (Quelle: ogd-smn_meta_parameters.csv).
PARAMS = {
    "tre200d0": "temp_mean_c",
    "tre200dn": "temp_min_c",
    "tre200dx": "temp_max_c",
    "rre150d0": "precip_mm",
    "sre000d0": "sunshine_min",
    "gre000d0": "radiation_wm2",
    "fu3010d0": "wind_mean_kmh",
    "fu3010dn": "wind_min_kmh",
    "fu3010dx": "wind_max_kmh",
    "ure200d0": "humidity_pct",
}
COLUMNS = list(PARAMS.values())  # DB column order (excluding obs_date)

TIMESTAMP_COL_CANDIDATES = ("reference_timestamp", "REFERENCE_TS")


def fetch_csv(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "meteodatacollector/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw), delimiter=";")
    rows = list(reader)
    if not rows:
        return []

    fieldnames = set(reader.fieldnames or [])
    missing = [p for p in PARAMS if p not in fieldnames]
    if missing:
        print(
            f"WARNUNG: Parameter nicht in {url} gefunden: {missing}\n"
            f"Vorhandene Spalten: {sorted(fieldnames)}",
            file=sys.stderr,
        )
    return rows


def parse_date(row: dict) -> str | None:
    for col in TIMESTAMP_COL_CANDIDATES:
        if col in row and row[col]:
            raw = row[col]
            for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                except ValueError:
                    continue
            print(f"WARNUNG: Konnte Datum nicht parsen: {raw!r}", file=sys.stderr)
    return None


def to_float(value: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def normalize_rows(raw_rows: list[dict]) -> dict[str, dict]:
    """Reduziert Rohzeilen auf {datum: {feld: wert}}."""
    out: dict[str, dict] = {}
    for row in raw_rows:
        date = parse_date(row)
        if not date:
            continue
        entry = {"date": date}
        for code, field in PARAMS.items():
            entry[field] = to_float(row.get(code, ""))
        out[date] = entry
    return out


def get_connection() -> pymssql.Connection:
    server = os.environ["AZURE_SQL_SERVER"]
    database = os.environ["AZURE_SQL_DATABASE"]
    user = os.environ["AZURE_SQL_USER"]
    password = os.environ["AZURE_SQL_PASSWORD"]
    return pymssql.connect(server=server, database=database, user=user, password=password, timeout=60)


def upsert_rows(conn: pymssql.Connection, rows: dict[str, dict]) -> int:
    if not rows:
        return 0

    set_clause = ", ".join(f"target.{c} = source.{c}" for c in COLUMNS)
    insert_cols = ", ".join(["obs_date", *COLUMNS])
    insert_vals = ", ".join(["source.obs_date", *[f"source.{c}" for c in COLUMNS]])
    placeholders = ", ".join(["%s"] * (1 + len(COLUMNS)))

    # One parameterized MERGE per row using a VALUES(...) source. This keeps
    # everything server-side and idempotent (safe to re-run / re-collect).
    merge_sql = f"""
        MERGE dbo.klo_daily AS target
        USING (VALUES ({placeholders})) AS source ({', '.join(['obs_date', *COLUMNS])})
        ON target.obs_date = source.obs_date
        WHEN MATCHED THEN
            UPDATE SET {set_clause}, updated_at = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT ({insert_cols}) VALUES ({insert_vals});
    """

    cursor = conn.cursor()
    count = 0
    for date in sorted(rows):
        entry = rows[date]
        values = [date] + [entry.get(c) for c in COLUMNS]
        cursor.execute(merge_sql, tuple(values))
        count += 1
    conn.commit()
    return count


def main() -> None:
    all_new: dict[str, dict] = {}
    for url in (HISTORICAL_URL, RECENT_URL):
        try:
            raw_rows = fetch_csv(url)
        except Exception as exc:  # noqa: BLE001
            print(f"FEHLER beim Laden von {url}: {exc}", file=sys.stderr)
            continue
        normalized = normalize_rows(raw_rows)
        print(f"{url}: {len(normalized)} Tage gelesen")
        all_new.update(normalized)

    if not all_new:
        print("Keine neuen Daten erhalten, breche ab ohne Aenderungen.", file=sys.stderr)
        sys.exit(1)

    conn = get_connection()
    try:
        written = upsert_rows(conn, all_new)
    finally:
        conn.close()

    print(f"Fertig. {written} Tage in Azure SQL Database geschrieben/aktualisiert.")


if __name__ == "__main__":
    main()

