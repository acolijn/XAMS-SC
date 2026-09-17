"""Run the web UI: python -m xams_sc.api

Bound to 127.0.0.1. See the note in app.py about why that is not negotiable.
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from ..config import ConfigError
from ..service import setup_logging

log = logging.getLogger(__name__)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m xams_sc.api")
    # NOT configurable to 0.0.0.0 by accident: the default is loopback and
    # anything else is a decision somebody has to type out in full (§8).
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--broker", default="127.0.0.1")
    p.add_argument("--broker-port", type=int, default=1883)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    setup_logging("webui", args.log_level)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "BINDING TO %s, NOT LOOPBACK. This exposes the interface to the "
            "network. §8 accepts only 127.0.0.1 without a recorded decision "
            "to the contrary.", args.host)

    from .app import create_app
    try:
        app = create_app(broker=args.broker, port=args.broker_port)
    except ConfigError as exc:
        print(f"FATAL: configuration is invalid: {exc}", file=sys.stderr)
        return 2

    log.info("web UI on http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
