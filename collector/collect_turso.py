#!/usr/bin/env python3
"""
Parallele Turso-Variante von collect.py.

Sammelt dieselben Tageswerte der MeteoSchweiz-Station Zuerich-Kloten (KLO)
wie collect.py, schreibt sie aber in eine Turso-Datenbank (libSQL/SQLite)
statt in Azure SQL. Voellig unabhaengig vom bestehenden Azure-Pfad - beide
laufen parallel, ohne sich gegenseitig zu beeinflussen.

Quelle: MeteoSchweiz Open Government Data (OGD-SMN)
https://opendatadocs.meteoswiss.ch/a-data-groundbased/a1-automatic-weather-stations
Nutzungsbedingungen: Quellenangabe "Source: MeteoSchweiz" erforderlich.

Benoetigte Umgebungsvariablen (als GitHub Secrets gesetzt):
  TURSO_DATABASE_URL   z.B. libsql://munotstadtmeteodb-<org>.turso.io
  TURSO_AUTH_TOKEN     Auth-Token aus dem Turso-Dashboard
"""

from __future__ import annotations

import csv
import io
import os
import sys
from datetime import datetime

import urllib.request
import libsql

STATION = "klo"  # Zuerich-Kloten
BASE_URL = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/{STATION}"
RECENT_URL = f"{BASE_URL}/ogd-smn_{STATION}_d_recent.csv"
HISTORICAL_URL = f"{BASE_URL}/ogd-smn_{STATION}_d_historical.csv"

# Parameter-Codes fuer Tageswerte (identisch zu collect.py; siehe dort fuer
# Details zu den Eigenheiten von MeteoSchweiz-Rohdaten).
PARAMS = {
    "tre200d0": "temp_mean_c",
    "tre200dn": "temp_min_c",
    "tre200dx": "temp_max_c",
    "rre150d0": "precip_mm",
    "sre000d0": "sunshine_min",
    "gre000d0": "radiation_wm2",
    "fu3010d0": "wind_mean_kmh",
    "fu3010d1": "wind_max_kmh",  # Böenspitze (1s), Tagesmaximum
    "ure200d0": "humidity_pct",
}
COLUMNS = list(PARAMS.values())

TIMESTAMP_COL_CANDIDATES = ("reference_timestamp", "REFERENCE_TS")


def fetch_csv(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "meteodatacollector-turso/1.0"})
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


def get_connection():
    url = os.environ["TURSO_DATABASE_URL"]
    token = os.environ["TURSO_AUTH_TOKEN"]
    return libsql.connect(database=url, auth_token=token)


def ensure_schema(conn) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS klo_daily (
            obs_date TEXT NOT NULL PRIMARY KEY,
            {', '.join(f'{c} REAL' for c in COLUMNS)},
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def upsert_rows(conn, rows: dict[str, dict], batch_size: int = 200) -> int:
    if not rows:
        return 0

    col_list = ", ".join(["obs_date", *COLUMNS])
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in COLUMNS)
    row_placeholder = "(" + ", ".join(["?"] * (1 + len(COLUMNS))) + ")"

    dates = sorted(rows)
    total = len(dates)
    written = 0

    for start in range(0, total, batch_size):
        chunk = dates[start:start + batch_size]
        values_sql = ", ".join([row_placeholder] * len(chunk))

        sql = f"""
            INSERT INTO klo_daily ({col_list})
            VALUES {values_sql}
            ON CONFLICT(obs_date) DO UPDATE SET {set_clause}, updated_at = CURRENT_TIMESTAMP
        """

        params: list = []
        for date in chunk:
            entry = rows[date]
            params.append(date)
            params.extend(entry.get(c) for c in COLUMNS)

        conn.execute(sql, params)
        written += len(chunk)
        print(f"  ... {written}/{total} Tage geschrieben (Turso)", file=sys.stderr)

    conn.commit()
    return written


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
    ensure_schema(conn)
    written = upsert_rows(conn, all_new)

    print(f"Fertig. {written} Tage in Turso (munotstadtmeteodb) geschrieben/aktualisiert.")


if __name__ == "__main__":
    main()
