#!/usr/bin/env python3
"""Synchronous Phase 0 direct CLI pre-tool gate. No output on quiet paths."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    from cardinal_core import investigation_gate_runtime as gate_runtime
    import _storyboard_discovery as discovery
    payload = json.load(sys.stdin)
    result = gate_runtime.dispatch(payload, runtime="claude", home=Path.home(),
                                   connection=discovery.connection, client=discovery.client_header(),
                                   hook=Path(__file__), environ=os.environ)
    if result:
        print(json.dumps(result))


if __name__ == "__main__":
    # Input/import/process crashes remain the observed runtime fail-open limit.
    # Recoverable enabled-route failures are handled by dispatch as valid denial.
    main()
