#!/usr/bin/env python3
"""
Parallele Turso-Variante von collect_hydro.py.

Sammelt dieselben BAFU-Hydrostationen (Rhein Neuhausen, Bodensee Berlingen,
Glatt Rheinsfelden) ueber LINDAS wie collect_hydro.py, schreibt sie aber in
Turso statt Azure SQL. Voellig unabhaengig vom bestehenden Azure-Pfad.

Quelle: Bundesamt fuer Umwelt BAFU, LINDAS Linked Data Service

Benoetigte Umgebungsvariablen:
  TURSO_DATABASE_URL   z.B. libsql://munotstadtmeteodb-<org>.turso.io
  TURSO_AUTH_TOKEN     Auth-Token aus dem Turso-Dashboard
"""

from __future__ import annotations

import csv
import io
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime

import libsql

LINDAS_QUERY_URL = "https://ld.admin.ch/query"

STATIONS = {
    "2288": {"name": "Rhein – Neuhausen", "kind": "river"},
    "2043": {"name": "Bodensee – Berlingen", "kind": "lake"},
    "2415": {"name": "Glatt – Rheinsfelden", "kind": "river"},
}

SPARQL_QUERY = """
PREFIX schema: <http://schema.org/>
PREFIX hd: <https://environment.ld.admin.ch/foen/hydro/dimension/>
PREFIX hgs: <https://environment.ld.admin.ch/foen/hydro/station/>
PREFIX river: <https://environment.ld.admin.ch/foen/hydro/river/observation/>
PREFIX lake: <https://environment.ld.admin.ch/foen/hydro/lake/observation/>

SELECT ?station_id ?station_name ?measurementTime ?discharge ?waterLevel ?waterTemperature
WHERE {
  VALUES ?station_id { "2288" "2043" "2415" }
  {
    BIND(IRI(CONCAT(STR(river:), ?station_id)) AS ?obs)
  } UNION {
    BIND(IRI(CONCAT(STR(lake:), ?station_id)) AS ?obs)
  }
  ?obs hd:measurementTime ?measurementTime .
  OPTIONAL { ?obs hd:discharge ?discharge }
  OPTIONAL { ?obs hd:waterLevel ?waterLevel }
  OPTIONAL { ?obs hd:waterTemperature ?waterTemperature }
  BIND(IRI(CONCAT(STR(hgs:), ?station_id)) AS ?station_iri)
  ?station_iri schema:name ?station_name .
}
"""


def fetch_lindas() -> list[dict]:
    body = urllib.parse.urlencode({"query": SPARQL_QUERY}).encode("utf-8")
    req = urllib.request.Request(
        LINDAS_QUERY_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Accept": "text/csv",
            "User-Agent": "meteodatacollector-hydro-turso/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8-sig")

    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)
    if not rows:
        print(f"WARNUNG: LINDAS-Antwort enthielt keine Zeilen. Rohantwort:\n{raw[:500]}", file=sys.stderr)
    return rows


def parse_time(raw: str) -> str | None:
    if not raw:
        return None
    try:
        # Normalisieren auf ISO 8601, Zeitzonen-Suffix bleibt erhalten.
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.isoformat()
    except ValueError:
        print(f"WARNUNG: Konnte measurementTime nicht parsen: {raw!r}", file=sys.stderr)
        return None


def to_float(raw: str) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def get_connection():
    url = os.environ["TURSO_DATABASE_URL"]
    token = os.environ["TURSO_AUTH_TOKEN"]
    return libsql.connect(database=url, auth_token=token)


def ensure_schema(conn) -> None:
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


def upsert_readings(conn, rows: list[dict]) -> int:
    written = 0
    for row in rows:
        station_id = row.get("station_id")
        reading_time = parse_time(row.get("measurementTime"))
        if not station_id or reading_time is None:
            continue

        discharge = to_float(row.get("discharge"))
        water_level = to_float(row.get("waterLevel"))
        water_temp = to_float(row.get("waterTemperature"))

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
            (station_id, reading_time, discharge, water_level, water_temp),
        )
        written += 1
    conn.commit()
    return written


def main() -> None:
    try:
        rows = fetch_lindas()
    except Exception as exc:  # noqa: BLE001
        print(f"FEHLER beim Abfragen von LINDAS: {exc}", file=sys.stderr)
        sys.exit(1)

    if not rows:
        print("Keine Daten von LINDAS erhalten, breche ab.", file=sys.stderr)
        sys.exit(1)

    conn = get_connection()
    ensure_schema(conn)
    written = upsert_readings(conn, rows)

    print(f"Fertig. {written} Messwerte in Turso (munotstadtmeteodb) geschrieben/aktualisiert.")


if __name__ == "__main__":
    main()
