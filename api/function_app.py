"""
Azure Function (HTTP, Python v2 programming model) that reads the
dbo.klo_daily table from Azure SQL Database and returns it as JSON in
the same shape the frontend (index.html) already expects:

{
  "station": "KLO",
  "station_name": "Zürich-Kloten",
  "source": "...",
  "generated_at": "...",
  "fields": [...],
  "days": [ { "date": "...", "temp_mean_c": ..., ... }, ... ]
}

Required Application Settings (Function App -> Configuration -> App settings):
  AZURE_SQL_SERVER    e.g. sql-munotstadt-meteo.database.windows.net
  AZURE_SQL_DATABASE  e.g. MeteoDB
  AZURE_SQL_USER      SQL login
  AZURE_SQL_PASSWORD  SQL password

CORS (allow the GitHub Pages frontend to call this API) is configured at
the Function App level in the Azure Portal (API -> CORS), not in code.
"""

import json
import os
from datetime import datetime, timezone

import azure.functions as func
import pymssql

app = func.FunctionApp()

COLUMNS = [
    "temp_mean_c",
    "temp_min_c",
    "temp_max_c",
    "precip_mm",
    "sunshine_min",
    "radiation_wm2",
    "wind_mean_kmh",
    "wind_max_kmh",
    "humidity_pct",
]


def get_connection():
    return pymssql.connect(
        server=os.environ["AZURE_SQL_SERVER"],
        database=os.environ["AZURE_SQL_DATABASE"],
        user=os.environ["AZURE_SQL_USER"],
        password=os.environ["AZURE_SQL_PASSWORD"],
        timeout=30,
        login_timeout=30,
    )


@app.route(route="klo-daily", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def klo_daily(req: func.HttpRequest) -> func.HttpResponse:
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor(as_dict=True)
            cursor.execute(
                f"SELECT obs_date, {', '.join(COLUMNS)} FROM dbo.klo_daily ORDER BY obs_date"
            )
            rows = cursor.fetchall()
        finally:
            conn.close()

        days = []
        for row in rows:
            entry = {"date": row["obs_date"].isoformat()}
            for col in COLUMNS:
                val = row.get(col)
                entry[col] = float(val) if val is not None else None
            days.append(entry)

        payload = {
            "station": "KLO",
            "station_name": "Zürich-Kloten",
            "source": "MeteoSchweiz (opendata.swiss) - Source: MeteoSchweiz",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fields": COLUMNS,
            "days": days,
        }

        return func.HttpResponse(
            json.dumps(payload, ensure_ascii=False),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as exc:  # noqa: BLE001
        error_payload = {"error": str(exc)}
        return func.HttpResponse(
            json.dumps(error_payload),
            mimetype="application/json",
            status_code=500,
        )
