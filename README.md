# SafeRoute

**Real-time evacuation routing around blocked roads.** Smart India Hackathon 2026, problem statement
DM03 *Blocked Evacuation Route Finder* (Disaster Management), team AstroSync.

A resident taps where they are and gets the best route to an open shelter that they can still reach,
using only roads that are open right now. The route avoids rising water and reported flooding, and
spreads people across shelters before any one fills. When a responder blocks a road, a flood sensor
detects water, or a resident reports a problem, every open map updates within seconds. It works in
English, Tamil and Hindi, by SMS for basic phones, and offline.

The working model runs on real OpenStreetMap roads for **Velachery, Chennai** (927 junctions,
1,130 road segments), an area that floods often.

| | |
|---|---|
| Stack | Python 3.11+, Flask, NetworkX (Dijkstra), SQLite, Leaflet.js, OpenStreetMap / OSMnx, Open-Meteo |
| Runtime dependencies | 3 (Flask, NetworkX, python-dotenv), plus gunicorn on Linux hosts |
| Tests | 122 automated tests (`pytest`), lint and format checks (`ruff`) |
| Speed | A route takes ~30 ms; a planner what-if over every junction ~40 ms on a laptop |

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/yashvasin1970/SafeRoute)

---

## What it does

### Residents: *Find a route*
- Tap the map, use your location, or pick your street from a list.
- Choose **On foot** or **By vehicle**, and **Safest** or **Fastest**.
- Get the shelter, ETA, distance and street-by-street steps. Every shelter is listed with how full it
  is, how many people are on the way, and whether it is open, full, closed or cut off.
- **Safest** routes avoid risky roads unless the detour is long, and say what they avoided and what
  it cost ("2 min longer than the fastest, to avoid rising water at Taramani Link Road").
- **Load balancing:** if the nearest shelter is filling up, you are sent where there is space, with
  the reason. *I'm going here* tells the system you are on your way, so the next people spread out.
- **Report** flooding, a blocked road or a rescue need at a spot on the map.
- The route **updates by itself** when conditions change, with a banner explaining why.
- If no open shelter can be reached, it says so plainly and gives safety advice and the 112 number.

### Responders: *Responders*
- Sign in with the responder ID and passcode. Only signed-in responders can change the map.
- Block and reopen roads; mark shelters open, full or closed and set **how many people are inside**.
- See resident reports (rescue requests and notes are visible to responders only), and **verify**
  them (which closes the road) or **dismiss** them.
- Play the **flood story** (see below), or reset the demo.

### Planners: *Planners*
- Build a what-if scenario by tapping roads and shelters. **Nothing is published.**
- Compare live and scenario: junctions that can reach a shelter, cut off, average time, the time
  within which 9 in 10 people reach safety, and the slowest junction. Colour every road by time to safety.

### Live feeds
- **IoT flood sensors:** at 30 cm a sensor closes every road within 80 m; below 15 cm it reopens
  them. Water in the 15 to 30 cm band makes those roads *risky*. A sensor never overrides a responder.
- **Weather:** rain forecast for the next 3 hours from [Open-Meteo](https://open-meteo.com) (free, no
  key), refreshed every 10 minutes. Heavy rain marks the low-lying stretches around sensors as risky.
- **Crowd reports** from residents (app or SMS) make nearby roads risky straight away but never close a
  road by themselves: a responder verifies or dismisses them, as the proposal requires.

### Risk-weighted routing

| Source | Roads affected | Extra cost |
|---|---|---|
| Water rising at a sensor (15 to 30 cm) | Every road the sensor watches | travel time × 2 |
| Unverified resident report of flooding or a blocked road | Roads within 60 m | × 2.5 |
| Heavy or extreme rain forecast | Every sensor's low-lying stretch | × 1.5 |

Risks add up, capped at × 5. ETAs are always real travel times; the risk only steers the choice.

### Shelter load balancing
A shelter's score is its travel time plus a penalty that grows once it is over 70% full, counting
people already on their way (up to 10 extra minutes at 100%). A full shelter is never suggested.
Capacities are in `data/velachery.shelters.json` (sample values).

### SMS for phones without data
Text commands, answered by the same routing engine, with replies in the language you used:

| English | Tamil | Hindi | Reply |
|---|---|---|---|
| `ROUTE <street>` | `வழி <street>` | `रास्ता <street>` | Nearest open shelter, ETA, the first streets to take |
| `SHELTERS` | `முகாம்` | `शिविर` | Each shelter and how full it is |
| `FLOOD <street>` | `வெள்ளம் <street>` | `बाढ़ <street>` | Logs a flooding report |
| `HELP <street>` | `உதவி <street>` | `मदद <street>` | Logs a rescue request for responders |
| `STATUS` | `நிலை` | `स्थिति` | Roads blocked and the rain forecast |

Street names are matched loosely ("sastri rd" finds Sastri Road). Try it in the app under *No mobile
data? Use SMS*. A real SMS gateway posts to `/api/sms/inbound` with the `X-Gateway-Key` header;
Twilio-style form posts get a TwiML reply.

### English, Tamil and Hindi
The whole interface switches language from the top bar, and remembers the choice. The Tamil and Hindi
text was drafted for the demo and should be reviewed by native speakers before real use.

### Works offline
SafeRoute installs like an app (*Install* / *Open as app*). It caches the map and the last live state,
so offline it still shows the last known closures and **works out routes on the device** with its own
Dijkstra, saying that it is offline and how old its information is.

### The flood story (demo)
*Responders → Play flood story* runs a scripted evening in Velachery that drives the real system,
one step every 9 seconds, with narration in all three languages: a heavy-rain warning, rising water, a
resident's report, an automatic closure, shelters filling up, an SMS rescue request, a responder
verifying a report, a shelter filling completely, and the water receding as roads reopen by themselves.

## How it follows the proposal

| Proposal step | Where |
|---|---|
| Load road network | `scripts/build_graph.py` builds `data/velachery.json` from OpenStreetMap with OSMnx; `network.py` loads it into NetworkX |
| Mark blocked roads | Responders (`POST /api/closures`), flood sensors, verified reports; stored in SQLite |
| Remove closed edges | `routing.open_roads()` returns a filtered **view** of the graph: nothing is copied or mutated |
| Shortest path to shelter | `routing.find_route()`: Dijkstra over (risk-weighted) travel time, skipping full, closed and unreachable shelters |
| Show route + ETA | Leaflet map with route, ETA, steps and every shelter's status, refreshed live |

## Run it

Needs Python 3.11 or newer.

```powershell
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m saferoute
```

```bash
# macOS / Linux
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m saferoute
```

Open <http://127.0.0.1:5050>. On first run SafeRoute generates a random responder passcode, sensor
device key, SMS gateway key and session secret, keeps them in `instance/secrets.json`, and prints the
passcode. To set your own, copy `.env.example` to `.env`:

| Variable | Purpose |
|---|---|
| `SAFEROUTE_RESPONDER_ID` | Optional: require this responder ID at sign-in (otherwise any name or unit works) |
| `SAFEROUTE_RESPONDER_PASSCODE` | Passcode responders sign in with |
| `SAFEROUTE_SENSOR_KEY` | Device key flood sensors send |
| `SAFEROUTE_SMS_KEY` | Key an SMS gateway sends to `/api/sms/inbound` |
| `SAFEROUTE_SECRET_KEY` | Signs session cookies |
| `SAFEROUTE_TRUST_PROXY`, `SAFEROUTE_SECURE_COOKIES` | Set to `1` behind a hosting proxy with HTTPS |
| `SAFEROUTE_NO_WEATHER` | Set to `1` to turn off the Open-Meteo feed |
| `SAFEROUTE_STORY_STEP_SECONDS` | Seconds between flood-story steps (default 9) |

## Demo script (about 4 minutes)

1. **Resident.** Pick *Sastri Road* from the street list. A blue route appears with ETA and steps.
2. **Language.** Switch to தமிழ் or हिन्दी in the top bar; everything follows.
3. **Flood story.** In a second window, *Responders → sign in → Play flood story*. Watch the first
   window: rain warning, risky roads in amber, a resident report, Taramani Link Road closing, the route
   moving, shelters filling and people being balanced to other shelters, then roads reopening.
4. **SMS.** Under *No mobile data? Use SMS*, send `ROUTE Sastri Road` or `வழி Sastri Road`.
5. **Responder.** Verify the open rescue request; block a road by hand and watch the reroute banner.
6. **Planner.** Tap two or three roads near a shelter and read the impact table.

Real sensors can be imitated with `python scripts/simulate_sensors.py`.

## How it works

```
 Browser / installed app              Flask API                         Engine (pure Python)
 ───────────────────────              ─────────                         ────────────────────
 resident taps map   ── POST /api/route ──▶ live state ──▶ risk.road_risk ──▶ routing.find_route
 every 3 s           ── GET /api/state?since=v ─▶ 204 if nothing changed
 responder action    ── POST /api/closures, /shelters, /reports/<id>/verify ──▶ SQLite (+ version, log)
 planner scenario    ── POST /api/impact (read-only) ─────────────────────▶ routing.coverage
 resident report     ── POST /api/reports ──▶ reports.make_report ──▶ SQLite
 flood sensor        ── POST /api/sensors/<id>/readings ──▶ sensors.decide ──▶ SQLite
 SMS gateway         ── POST /api/sms/inbound ──▶ sms.handle ──▶ the same routing
 weather             ◀── Open-Meteo every 10 min (feeds.WeatherFeed)
```

## API

| Method | Path | Who | What |
|---|---|---|---|
| GET | `/api/area` | anyone | Roads, junctions, shelters (with capacity), sensors |
| GET | `/api/state?since=<v>` | anyone | Closures, shelter load, sensors, risk, reports, weather, story, activity; `204` if unchanged |
| POST | `/api/route` | anyone | `{lat, lon, mode, prefer}` → route, ETA, steps, risks avoided, load balancing, every shelter |
| POST | `/api/impact` | anyone | `{mode, closures, shelters_closed}` → live vs. scenario coverage (publishes nothing) |
| POST | `/api/arrivals` | anyone | `{shelter, people}`: "I'm going here" (3 per person, expires after 45 min) |
| POST | `/api/reports` | anyone | `{lat, lon, kind, note}` with kind `flooding`, `blocked` or `rescue` (6 per 10 min) |
| POST | `/api/sms/simulate` | anyone | `{text}` → the SMS reply (rate-limited) |
| POST | `/api/responder/login` / `logout` | responders | `{name, passcode}` |
| POST / DELETE | `/api/closures`, `/api/closures/<road>` | responders | Block / reopen a road |
| POST | `/api/shelters/<id>` | responders | `{status}` and/or `{occupancy}` |
| POST | `/api/reports/<id>/verify` or `/dismiss` | responders | Verify (closes the road) or dismiss a report |
| POST | `/api/story/start` or `/stop` | responders | Play or stop the flood story |
| POST | `/api/reset` | responders | Clear closures, reports, readings and shelter counts |
| POST | `/api/sensors/<id>/readings` | devices | `{level_cm}` with `X-Device-Key` |
| POST | `/api/sms/inbound` | SMS gateway | JSON `{text}` or Twilio form `Body`, with `X-Gateway-Key` |

## Security

- **Only authorised responders change the map** (the proposal's answer to false reports). ID and
  passcode compared in constant time; 5 wrong attempts per 5 minutes per address, then a lock-out.
- **Resident reports never close roads** on their own; rescue requests and all notes are shown to
  responders only. Reports, arrivals and SMS are rate-limited per (hashed) address.
- **CSRF protection** on every responder change; `HttpOnly`, `SameSite` and (hosted) `Secure` cookies.
- **Sensors and SMS gateways use their own keys**, separate from the responder passcode.
- **No secrets in the code.** Generated on first run, or set by environment variables or the host.
- **Strict Content Security Policy** (no inline or third-party scripts), `X-Frame-Options: DENY`,
  `nosniff`, `no-referrer`, HSTS on HTTPS. Leaflet is bundled and checked against its official hashes.
- **All input is validated**; the page inserts server text with `textContent`, never as HTML.

## Testing

```bash
pip install -r requirements-dev.txt
pytest          # 122 tests
ruff check . && ruff format --check .
```

Most tests use a six-junction town where every answer can be checked by hand (`tests/conftest.py`):
routing and closures, risk-weighted routing and its explanations, load balancing, every sensor rule,
reports, arrivals, the weather feed (with a fake service), SMS parsing and replies in three languages,
every API endpoint and its security checks, and the whole flood story run against the real Velachery map.

## Project structure

```
saferoute/
  network.py      Road graph, shelters (with capacity) and sensors
  routing.py      Risk-aware, load-balanced routing; coverage; what-if impact (pure functions)
  risk.py         How risky each open road is, from sensors, reports and rain
  sensors.py      Flood sensor thresholds and decisions
  reports.py      Resident reports: snap to a road, verify, dismiss
  feeds.py        Live weather from Open-Meteo, with a drill override
  sms.py          SMS commands in English, Tamil and Hindi
  story.py        The scripted flood evening
  store.py        SQLite: closures, shelters, occupancy, arrivals, reports, feeds, activity, version
  api.py          HTTP API
  security.py     Secrets, sign-in, CSRF, rate limiting, security headers
  compression.py  Gzip for slow connections
  static/         app.js (map client), i18n.js (EN/TA/HI), sw.js (offline), app.css, Leaflet
scripts/          build_graph.py (OpenStreetMap → area), simulate_sensors.py
data/             velachery.json, velachery.sensors.json, velachery.shelters.json
docs/             SafeRoute_Detailed_Explanation.pdf, screenshots
```

## Assumptions and limits

- **Not a certified warning system.** It supports responders and does not replace official alerts.
- **Sample data.** Shelters are mapped schools and halls with sample capacities; sensor positions are
  illustrative; the SMS service runs through an in-app simulator until a gateway is connected.
- **Roads are two-way during an evacuation**; closures apply to whole segments (junction to junction).
- **Weather** is a forecast for the area's centre point, not street-level flooding.
- **Basemap tiles** come from the OpenStreetMap Foundation's public tile server, fine for a demo; a real
  deployment should use its own tile server or a commercial provider.

Road data © OpenStreetMap contributors (ODbL). Weather data by Open-Meteo.com (CC BY 4.0).
