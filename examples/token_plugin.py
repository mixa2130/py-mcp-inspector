#!/usr/bin/env python3
"""An example auth plugin: a script the inspector runs to get the credential.

The contract is the whole of this file's interface:

* arguments come from the **Script arguments** field, split the way a shell
  would split them;
* the environment is the inspector's own, plus ``PYMCPINSPECTOR_URL``,
  ``PYMCPINSPECTOR_TRANSPORT``, ``PYMCPINSPECTOR_AUTH_HEADER`` and
  ``PYMCPINSPECTOR_AUTH_SCHEME``, which say where the connection points;
* **stdout** carries the answer -- either the bare token, or a JSON object with
  ``token`` and optionally ``scheme`` and ``header`` when the script also
  decides how the credential is sent;
* **stderr** is for progress and diagnosis; every line of it reaches the
  inspector's log, whether the run succeeded or not;
* a non-zero exit is a failure, and its last stderr lines become the message
  the viewer sees.

A real one would call an identity provider here. This one mints something
recognisable so the loop can be tried end to end against ``demo_server.py``:

    python examples/demo_server.py --transport streamable-http --port 8931
    # in the UI: Streamable HTTP -> http://127.0.0.1:8931/mcp
    #            Token script -> examples/token_plugin.py
    #            Connect -> Tools -> show_headers -> Run tool
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="demo", help="which imaginary account to mint for")
    parser.add_argument("--json", action="store_true", help="answer with the object form of the contract")
    parser.add_argument("--fail", action="store_true", help="fail, to see what a failure looks like")
    args = parser.parse_args()

    target = os.environ.get("PYMCPINSPECTOR_URL", "(nowhere)")
    print(f"minting a {args.profile} token for {target}", file=sys.stderr)

    if args.fail:
        print("the vault refused: no active session", file=sys.stderr)
        return 1

    token = f"demo-{args.profile}-{int(time.time())}"
    if args.json:
        # The object form also says how the credential travels, which is what
        # an API-key style server needs: header X-Api-Key, no scheme at all.
        json.dump({"token": token, "scheme": "", "header": "X-Api-Key"}, sys.stdout)
    else:
        print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
