"""Config for a single tool-calling agent run (MMEngine-style, Autogenesis-flavored).

Load with ``Config.fromfile("configs/tool_calling_agent.py")``. Every module-level
variable below becomes a config key.
"""

# Backend: "auto" uses OpenRouter when OPENROUTER_API_KEY is set, else the mock.
provider = "auto"

# Where versioned resources, memory, and traces are written.
workdir = "workdir"

# Passed to the model backend. The slug is an OpenRouter model id.
model = {
    "model": "anthropic/claude-opus-4.8",
    "max_tokens": 1024,
}

# Agent runtime knobs.
agent = {
    "name": "tool_calling_agent",
    "max_steps": 6,
}

# A reasonable system prompt that encourages tool use.
system_prompt = (
    "You are a precise assistant. Use the available tools — especially the "
    "calculator — to compute exact answers, then reply with the final answer."
)

# The default task for the example runner.
task = "What is (12 + 30) * 2?"
