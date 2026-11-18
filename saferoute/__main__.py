"""Run SafeRoute locally:  python -m saferoute  [--host 127.0.0.1] [--port 5050]"""

from __future__ import annotations

import argparse

from . import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the SafeRoute server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()

    app = create_app()
    print(f"\n  SafeRoute is running at http://{args.host}:{args.port}")
    print(f"  Responder passcode: {app.config['RESPONDER_PASSCODE']}")
    print("  (set SAFEROUTE_RESPONDER_PASSCODE to choose your own)\n")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
