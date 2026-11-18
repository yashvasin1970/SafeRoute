"""Pretend to be the flood sensors in the field, for demos.

Each sensor posts a water depth to SafeRoute, the same way a real device would (an HTTP POST with
its device key). Watch the map while this runs: the roads around a sensor close by themselves when
water reaches 30 cm and reopen once it drops below 15 cm.

    python scripts/simulate_sensors.py                    # a storm: water rises, then recedes
    python scripts/simulate_sensors.py --set FS-02=42     # send one reading and stop
    python scripts/simulate_sensors.py --interval 1       # faster

The device key is read from SAFEROUTE_SENSOR_KEY, or from instance/secrets.json, which the server
creates on its first run. Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# How each sensor's water rises during the storm, in cm per step, and its peak.
STORM = {
    "FS-02": (6, 55),  # Taramani Link Road floods first and worst
    "FS-01": (4, 40),  # Velachery Main Road floods a little later
    "FS-04": (5, 35),  # Ram Nagar Main Road starts rising after a delay
    "FS-03": (3, 22),  # Velachery Bypass Road only reaches the caution level
}
DELAY = {"FS-04": 3}
RISE_STEPS = 12
RECEDE_STEPS = 10
RECEDE_CM = 6


def device_key() -> str:
    key = os.environ.get("SAFEROUTE_SENSOR_KEY")
    if key:
        return key
    secrets_file = ROOT / "instance" / "secrets.json"
    if secrets_file.exists():
        return json.loads(secrets_file.read_text(encoding="utf-8"))["sensor_key"]
    sys.exit("No device key. Start the server once (it creates instance/secrets.json) or set SAFEROUTE_SENSOR_KEY.")


def send(base_url: str, key: str, sensor: str, level: float) -> dict:
    request = urllib.request.Request(
        f"{base_url}/api/sensors/{sensor}/readings",
        data=json.dumps({"level_cm": level}).encode(),
        headers={"Content-Type": "application/json", "X-Device-Key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"error": json.loads(error.read() or b"{}").get("error", str(error))}


def report(sensor: str, level: float, result: dict) -> None:
    if "error" in result:
        print(f"  {sensor}: {level:5.1f} cm  ->  error: {result['error']}")
        return
    note = ""
    if result.get("closed"):
        note = f"  -> closed {result['closed']} road segment(s)"
    elif result.get("reopened"):
        note = f"  -> reopened {result['reopened']} road segment(s)"
    print(f"  {sensor}: {level:5.1f} cm  ({result['band']}){note}")


def storm_levels():
    """Yield {sensor: level} for each step: water rises, peaks, then recedes."""
    levels = dict.fromkeys(STORM, 0.0)
    for step in range(1, RISE_STEPS + 1):
        for sensor, (rate, peak) in STORM.items():
            start = DELAY.get(sensor, 0)
            levels[sensor] = float(min(peak, max(0, step - start) * rate))
        yield dict(levels)
    for _ in range(RECEDE_STEPS):
        for sensor in levels:
            levels[sensor] = max(0.0, levels[sensor] - RECEDE_CM)
        yield dict(levels)


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate SafeRoute flood sensors.")
    parser.add_argument("--url", default="http://127.0.0.1:5050", help="SafeRoute server address")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between readings")
    parser.add_argument("--set", metavar="SENSOR=CM", help="send a single reading, e.g. FS-02=42")
    args = parser.parse_args()
    base_url = args.url.rstrip("/")
    key = device_key()

    if args.set:
        sensor, _, level = args.set.partition("=")
        report(sensor, float(level), send(base_url, key, sensor, float(level)))
        return

    print("Simulating a storm. Watch the map. Ctrl+C to stop.")
    try:
        for step, levels in enumerate(storm_levels(), start=1):
            print(f"Reading {step}:")
            for sensor, level in levels.items():
                report(sensor, level, send(base_url, key, sensor, level))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped.")
    except urllib.error.URLError as error:
        sys.exit(f"Can't reach SafeRoute at {base_url}: {error.reason}. Is the server running?")


if __name__ == "__main__":
    main()
