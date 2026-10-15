import re

filepath = "/Users/w/Cursor/automation/api_server.py"
with open(filepath, 'r') as f:
    content = f.read()

old_client = """def _ai_client():
    _load_env()
    return anthropic.Anthropic(
        api_key=os.getenv("ANTHROPIC_AUTH_TOKEN"),
        base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
    )"""

new_client = """def _ai_client():
    _load_env()
    return anthropic.Anthropic(
        api_key=os.getenv("ANTHROPIC_AUTH_TOKEN"),
        base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        max_retries=0,
    )"""

content = content.replace(old_client, new_client)

old_memory = """                    try:
                        from scripts.memory_agent import MemoryAgent
                        memory_agent = MemoryAgent()"""

new_memory = """                    try:
                        global _MEM_AGENT
                        if "_MEM_AGENT" not in globals():
                            from scripts.memory_agent import MemoryAgent
                            _MEM_AGENT = MemoryAgent()
                        memory_agent = _MEM_AGENT"""

if old_memory in content:
    content = content.replace(old_memory, new_memory)

with open(filepath, 'w') as f:
    f.write(content)
print("Optimizations applied")
