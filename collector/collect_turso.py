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
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
JSON_PATH = DATA_DIR / "klo_daily.json"

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


class ResilientConnection:
    """Wrapper um eine Turso-Verbindung, der bei einem Verbindungs- oder
    Stream-Fehler (z.B. "stream not found" nach laengerer offener
    Verbindung, oder ein kurzer Netzwerk-Aussetzer) automatisch neu
    verbindet und die Abfrage erneut versucht - statt den ganzen Lauf
    abbrechen zu lassen. Reconnects passieren transparent bei jedem
    execute()-Aufruf, nicht nur beim ersten Verbindungsaufbau.
    """

    def __init__(self, max_query_attempts: int = 4):
        self.max_query_attempts = max_query_attempts
        self._conn = self._connect()

    def _connect(self, max_attempts: int = 5):
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

    def execute(self, sql, params=None):
        delays = [3, 6, 12]
        last_error: Exception | None = None
        for attempt in range(1, self.max_query_attempts + 1):
            try:
                return self._conn.execute(sql, params) if params is not None else self._conn.execute(sql)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == self.max_query_attempts:
                    break
                wait = delays[min(attempt - 1, len(delays) - 1)]
                print(
                    f"WARNUNG: Turso-Abfrage fehlgeschlagen (Versuch {attempt}/{self.max_query_attempts}): {exc}\n"
                    f"  -> baue neue Verbindung auf, warte {wait}s.",
                    file=sys.stderr,
                )
                time.sleep(wait)
                self._conn = self._connect()
        raise last_error  # noqa: RSE102

    def commit(self):
        self._conn.commit()


def get_connection() -> ResilientConnection:
    return ResilientConnection()


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


JSON_WINDOW_YEARS = 20


def export_json(conn) -> int:
    """Liest die letzten JSON_WINDOW_YEARS Jahre aus klo_daily und schreibt
    data/klo_daily.json.

    Turso ist damit ab jetzt die Quelle fuers Frontend (GitHub Pages liest
    diese Datei statisch) - entkoppelt von Azure, das weiterhin unabhaengig
    sein eigenes Archiv pflegt. Die vollstaendige Historie bleibt in Turso
    selbst erhalten; nur der Website-Export ist begrenzt, damit die Datei
    nicht bei jedem Seitenaufruf unnoetig gross ist.
    """
    col_list = ", ".join(["obs_date", *COLUMNS])
    result = conn.execute(
        f"""
        SELECT {col_list} FROM klo_daily
        WHERE obs_date >= date('now', '-{JSON_WINDOW_YEARS} years')
        ORDER BY obs_date
        """
    ).fetchall()

    days = []
    for row in result:
        entry = {"date": row[0]}
        for i, col in enumerate(COLUMNS, start=1):
            entry[col] = row[i]
        days.append(entry)

    payload = {
        "station": "KLO",
        "station_name": "Zürich-Kloten",
        "source": "MeteoSchweiz (opendata.swiss) - Source: MeteoSchweiz",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fields": COLUMNS,
        "days": days,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(days)


def main() -> None:
    conn = get_connection()
    ensure_schema(conn)

    # Die volle Historie wurde einmalig komplett geladen (33'000+ Tage).
    # Ab jetzt reicht MeteoSchweiz' "recent"-CSV (~200-220 Tage) - das ist
    # praktisch der "current load": deckt den laufenden Tag plus
    # nachtraegliche Korrekturen der letzten Wochen/Monate ab, ohne bei
    # jedem der vielen taeglichen Laeufe die komplette Historie erneut zu
    # lesen und zu schreiben.
    all_new: dict[str, dict] = {}
    try:
        raw_rows = fetch_csv(RECENT_URL)
    except Exception as exc:  # noqa: BLE001
        print(f"FEHLER beim Laden von {RECENT_URL}: {exc}", file=sys.stderr)
        sys.exit(1)
    normalized = normalize_rows(raw_rows)
    print(f"{RECENT_URL}: {len(normalized)} Tage gelesen")
    all_new.update(normalized)

    if not all_new:
        print("Keine neuen Daten erhalten, breche ab ohne Aenderungen.", file=sys.stderr)
        sys.exit(1)

    written = upsert_rows(conn, all_new)
    exported = export_json(conn)

    print(f"Fertig. {written} Tage in Turso (munotstadtmeteodb) geschrieben/aktualisiert.")
    print(f"Export: {exported} Tage nach {JSON_PATH} geschrieben.")


if __name__ == "__main__":
    main()
