"""Config for the self-evolution loop.

Load with ``Config.fromfile("configs/self_evolution.py")``.
"""

provider = "auto"
workdir = "workdir"

model = {
    "model": "anthropic/claude-opus-4.8",
    "max_tokens": 1024,
}

agent = {
    "name": "tool_calling_agent",
    "max_steps": 6,
}

# Evolution settings.
prompt_name = "agent_system_prompt"
iterations = 5

# A deliberately weak seed so there is room to improve.
seed_prompt = "You are a helpful assistant. Answer the user's question."

# The arithmetic expression the agent must learn to solve with tools.
task_expression = "(12 + 30) * 2"
