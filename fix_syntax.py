import re

with open('/Users/w/Cursor/automation/api_server.py', 'r') as f:
    content = f.read()

# Fix any broken strings or trailing newlines causing SyntaxError inside the memory_context block.
# I will directly edit the file and restart PM2.
