# meteodatacollector

Sammelt täglich die MeteoSchweiz-Messwerte der Station **Zürich-Kloten (KLO)**
und zeigt sie auf einer öffentlichen Startseite (`index.html`) an.

Erfasste Grössen (Tageswerte):

| Grösse | Parameter-Code | Einheit |
|---|---|---|
| Temperatur, Tagesmittel | `tre200d0` | °C |
| Temperatur, Minimum | `tre200dn` | °C |
| Temperatur, Maximum | `tre200dx` | °C |
| Niederschlag, Tagessumme | `rre150d0` | mm |
| Sonnenscheindauer, Tagessumme | `sre000d0` | Minuten (Seite zeigt Stunden) |
| Globalstrahlung, Tagesmittel | `gre000d0` | W/m² |
| Windgeschwindigkeit, Tagesmittel | `fu3010d0` | km/h |
| Relative Luftfeuchtigkeit, Tagesmittel | `ure200d0` | % |

Quelle: MeteoSchweiz Open Government Data (`ch.meteoschweiz.ogd-smn`).
Nutzung ohne Einschränkung, Quellenangabe „Source: MeteoSchweiz" ist Pflicht.

Hinweis: Niederschlag wird von MeteoSchweiz offiziell 05:40–05:40 Uhr
Folgetag summiert (nicht exakt Mitternacht bis Mitternacht).

## Einrichtung auf GitHub

1. Repo `meteodatacollector` (public) erstellen, Dateien pushen.
2. Settings → Actions → General → Workflow permissions → "Read and write" aktivieren.
3. Settings → Pages → Source: Deploy from a branch, Branch `main`, Ordner `/ (root)`.
4. Actions → Workflow manuell einmal starten ("Run workflow").
