#!/usr/bin/env python3
"""
Sammelt aktuelle Messwerte von drei BAFU-Hydrostationen ueber den
offiziellen Linked-Data-Dienst LINDAS (SPARQL, ld.admin.ch) und schreibt
sie in eine Azure SQL Database (Tabelle dbo.hydro_readings) sowie als
JSON-Snapshot ins Repo (data/hydro_latest.json), analog zu collect.py.

WICHTIG: Anders als MeteoSchweiz liefert LINDAS nur den jeweils
aktuellsten Messwert pro Station (Aktualisierung alle ~10 Min.), keine
mehrjaehrige Historie. Die Zeitreihe baut sich erst ab dem ersten Lauf
dieses Skripts selbst auf.

Quelle: Bundesamt fuer Umwelt BAFU, LINDAS Linked Data Service
https://www.hydrodaten.admin.ch/ , https://lindas.admin.ch/

Benoetigte Umgebungsvariablen (dieselben wie collect.py):
  AZURE_SQL_SERVER, AZURE_SQL_DATABASE, AZURE_SQL_USER, AZURE_SQL_PASSWORD
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pymssql

LINDAS_QUERY_URL = "https://ld.admin.ch/query"

# Stationen: BAFU-Stationsnummer -> Anzeigename/Art.
# "kind" bestimmt, welches Praedikat als Hauptwert erwartet wird
# (river -> discharge/Abfluss, lake -> waterLevel/Pegel).
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

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
JSON_PATH = DATA_DIR / "hydro_latest.json"

# Wie viele Tage Historie sollen im JSON-Export fuer die Webseite stehen.
JSON_WINDOW_DAYS = 90


def fetch_lindas() -> list[dict]:
    """Fragt LINDAS per SPARQL ab, gibt eine Liste von Zeilen (dicts) zurueck."""
    body = urllib.parse.urlencode({"query": SPARQL_QUERY}).encode("utf-8")
    req = urllib.request.Request(
        LINDAS_QUERY_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Accept": "text/csv",
            "User-Agent": "meteodatacollector-hydro/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8-sig")

    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)
    if not rows:
        print(f"WARNUNG: LINDAS-Antwort enthielt keine Zeilen. Rohantwort:\n{raw[:500]}", file=sys.stderr)
    return rows


def parse_time(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
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


def get_connection(max_attempts: int = 6) -> pymssql.Connection:
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
                f"WARNUNG: DB-Verbindung fehlgeschlagen (Versuch {attempt}/{max_attempts}): {exc}\n"
                f"  -> warte {wait}s und versuche erneut.",
                file=sys.stderr,
            )
            time.sleep(wait)
    raise last_error  # noqa: RSE102


def upsert_readings(conn: pymssql.Connection, rows: list[dict]) -> int:
    cursor = conn.cursor()
    written = 0
    for row in rows:
        station_id = row.get("station_id")
        reading_time = parse_time(row.get("measurementTime"))
        if not station_id or reading_time is None:
            continue

        discharge = to_float(row.get("discharge"))
        water_level = to_float(row.get("waterLevel"))
        water_temp = to_float(row.get("waterTemperature"))

        cursor.execute(
            """
            MERGE dbo.hydro_readings AS target
            USING (VALUES (%s, %s, %s, %s, %s)) AS source
                (station_id, reading_time, discharge_m3s, water_level_m, water_temp_c)
            ON target.station_id = source.station_id AND target.reading_time = source.reading_time
            WHEN MATCHED THEN
                UPDATE SET discharge_m3s = source.discharge_m3s,
                           water_level_m = source.water_level_m,
                           water_temp_c = source.water_temp_c
            WHEN NOT MATCHED THEN
                INSERT (station_id, reading_time, discharge_m3s, water_level_m, water_temp_c)
                VALUES (source.station_id, source.reading_time, source.discharge_m3s,
                        source.water_level_m, source.water_temp_c);
            """,
            (station_id, reading_time, discharge, water_level, water_temp),
        )
        written += 1
    conn.commit()
    return written


def export_json(conn: pymssql.Connection) -> int:
    cursor = conn.cursor(as_dict=True)
    cursor.execute(
        """
        SELECT station_id, reading_time, discharge_m3s, water_level_m, water_temp_c
        FROM dbo.hydro_readings
        WHERE reading_time >= DATEADD(day, -%s, SYSUTCDATETIME())
        ORDER BY station_id, reading_time
        """,
        (JSON_WINDOW_DAYS,),
    )
    rows = cursor.fetchall()

    stations_out: dict[str, dict] = {}
    for sid, meta in STATIONS.items():
        stations_out[sid] = {
            "name": meta["name"],
            "kind": meta["kind"],
            "readings": [],
        }

    total = 0
    for row in rows:
        sid = row["station_id"]
        if sid not in stations_out:
            continue
        stations_out[sid]["readings"].append({
            "time": row["reading_time"].isoformat(),
            "discharge_m3s": float(row["discharge_m3s"]) if row["discharge_m3s"] is not None else None,
            "water_level_m": float(row["water_level_m"]) if row["water_level_m"] is not None else None,
            "water_temp_c": float(row["water_temp_c"]) if row["water_temp_c"] is not None else None,
        })
        total += 1

    payload = {
        "source": "Bundesamt für Umwelt BAFU, LINDAS Linked Data Service - Source: BAFU",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stations": stations_out,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return total


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
    try:
        written = upsert_readings(conn, rows)
        exported = export_json(conn)
    finally:
        conn.close()

    print(f"Fertig. {written} Messwerte in Azure SQL Database geschrieben/aktualisiert.")
    print(f"Export: {exported} Datenpunkte nach {JSON_PATH} geschrieben.")


if __name__ == "__main__":
    main()
