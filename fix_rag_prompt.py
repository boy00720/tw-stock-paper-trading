import re

with open('/Users/w/Cursor/automation/api_server.py', 'r') as f:
    content = f.read()

old_memory_format = 'memory_context = "\\n\\n【從長期記憶庫提取的相關歷史參考】\\n" + "\\n---\\n".join(docs) + "\\n"'
new_memory_format = 'memory_context = "\\n\\n【從長期記憶庫提取的相關歷史參考】\\n" + "\\n---\\n".join(docs) + "\\n\\n(請注意：若上述歷史記憶與 W 剛說的話題無關，請直接忽略這些參考資料，單純回覆 W 的對話即可。)"'

if old_memory_format in content:
    content = content.replace(old_memory_format, new_memory_format)
    with open('/Users/w/Cursor/automation/api_server.py', 'w') as f:
        f.write(content)
    print("Fixed RAG prompt context")
