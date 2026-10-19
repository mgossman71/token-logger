#!/usr/bin/env python3
"""Docker healthcheck: verify the logger is accepting connections.

This is a liveness probe only (does not depend on the upstream Ollama server).
It TCP-connects to the port the server is listening on.
"""
import os
import socket
import sys


def main() -> int:
    host = os.environ.get("HEALTH_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("LISTEN_PORT", "11435"))
    except ValueError:
        return 1
    try:
        with socket.create_connection((host, port), timeout=3):
            return 0
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
