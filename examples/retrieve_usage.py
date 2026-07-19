"""Local example: retrieve the usage recorded by a gateway request.

The gateway records one immutable usage event per request, attributed to the
workspace/project/environment/user/team/virtual-key and a run id, with the
provider cost and the Genesis service fee stored separately. This reads them back
from the control-plane API (dashboard session auth — NOT the gns_ inference key).

    export GENESIS_SESSION_TOKEN="<Better Auth JWT>"
    export GENESIS_API_URL="http://localhost:8000"
    python examples/retrieve_usage.py
"""

import os
import urllib.request
import json

API = os.environ.get("GENESIS_API_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("GENESIS_SESSION_TOKEN", "")


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{API}{path}", method="GET")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


events = _get("/v1/usage-events?limit=5")["items"]
for e in events:
    print(f"- {e['created_at']}  {e['provider']}/{e['model']}")
    print(f"    run={e['run_id']}  key={e['virtual_key_id']}  tokens="
          f"{e['input_tokens']}+{e['output_tokens']}")
    print(f"    provider_cost={e['upstream_cost']}  genesis_cost={e['genesis_calculated_cost']}"
          f"  ({e['cost_source']}, {e['reconciliation_state']})")

# The per-run receipt (aggregated across a run's calls) lands with the receipts
# PR; today each usage event already carries its run_id for correlation.
