"""Local example: call the Genesis gateway with a stock OpenAI client.

No Genesis SDK needed for inference — point the OpenAI client's ``base_url`` at
Genesis and use a Genesis virtual key as the ``api_key``.

    pip install openai
    # create a key first (dashboard or the control-plane API):
    #   POST /v1/virtual-keys  -> {"key": "gns_test_…", ...}   (shown once)
    export GENESIS_API_KEY="gns_test_…"
    export GENESIS_BASE_URL="http://localhost:8000/v1"
    python examples/gateway_chat_completion.py
"""

import os

from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("GENESIS_API_KEY", "gns_test_example"),
    base_url=os.environ.get("GENESIS_BASE_URL", "http://localhost:8000/v1"),
)

# Use the raw-response wrapper so we can read the Genesis request id header.
raw = client.chat.completions.with_raw_response.create(
    model="anthropic/claude-opus-4.8",
    messages=[{"role": "user", "content": "Explain compound interest simply."}],
)

completion = raw.parse()
print(completion.choices[0].message.content)
print("\nGenesis-Request-Id:", raw.headers.get("x-genesis-request-id"))
print("Attach it (or the run id) to correlate the usage event + run receipt.")
