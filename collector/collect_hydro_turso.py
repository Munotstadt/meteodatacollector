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
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import libsql

LINDAS_QUERY_URL = "https://ld.admin.ch/query"

STATIONS = {
    "2288": {"name": "Rhein – Neuhausen", "kind": "river"},
    "2043": {"name": "Bodensee – Berlingen", "kind": "lake"},
    "2415": {"name": "Glatt – Rheinsfelden", "kind": "river"},
}

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
JSON_PATH = DATA_DIR / "hydro_latest.json"
JSON_WINDOW_DAYS = 90

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


def get_connection(max_attempts: int = 5):
    url = os.environ["TURSO_DATABASE_URL"]
    token = os.environ["TURSO_AUTH_TOKEN"]

    delays = [3, 6, 12, 20, 30][: max_attempts - 1]
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            conn = libsql.connect(database=url, auth_token=token)
            conn.execute("SELECT 1")
            return conn
        except Exception as exc:  # noqa: BLE001 - libsql wirft breite ValueErrors
            last_error = exc
            if attempt == max_attempts:
                break
            wait = delays[attempt - 1]
            print(
                f"WARNUNG: Turso-Verbindung fehlgeschlagen (Versuch {attempt}/{max_attempts}): {exc}\n"
                f"  -> warte {wait}s und versuche erneut.",
                file=sys.stderr,
            )
            time.sleep(wait)
    raise last_error  # noqa: RSE102


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


def export_json(conn) -> int:
    """Liest hydro_readings aus Turso zurueck (letzte JSON_WINDOW_DAYS Tage)
    und schreibt data/hydro_latest.json - dieselbe Form, die index.html
    bisher von Azure bekam. Turso ist damit die Quelle fuers Frontend.
    """
    result = conn.execute(
        """
        SELECT station_id, reading_time, discharge_m3s, water_level_m, water_temp_c
        FROM hydro_readings
        WHERE reading_time >= datetime('now', ?)
        ORDER BY station_id, reading_time
        """,
        (f"-{JSON_WINDOW_DAYS} days",),
    ).fetchall()

    stations_out: dict[str, dict] = {}
    for sid, meta in STATIONS.items():
        stations_out[sid] = {"name": meta["name"], "kind": meta["kind"], "readings": []}

    total = 0
    for row in result:
        sid, reading_time, discharge, water_level, water_temp = row
        if sid not in stations_out:
            continue
        stations_out[sid]["readings"].append({
            "time": reading_time,
            "discharge_m3s": discharge,
            "water_level_m": water_level,
            "water_temp_c": water_temp,
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
    ensure_schema(conn)
    written = upsert_readings(conn, rows)
    exported = export_json(conn)

    print(f"Fertig. {written} Messwerte in Turso (munotstadtmeteodb) geschrieben/aktualisiert.")
    print(f"Export: {exported} Datenpunkte nach {JSON_PATH} geschrieben.")


if __name__ == "__main__":
    main()
