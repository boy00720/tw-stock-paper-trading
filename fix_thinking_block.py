import os
import glob

def fix_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
        
    old_return = 'text_blocks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
            if text_blocks:
                return "".join(text_blocks).strip()
            return resp.content[-1].text.strip() if hasattr(resp.content[-1], "text") else str(resp.content)'
    new_return = '''text_blocks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
            if text_blocks:
                return "".join(text_blocks).strip()
            return resp.content[-1].text.strip() if hasattr(resp.content[-1], "text") else str(resp.content)'''
            
    if old_return in content:
        content = content.replace(old_return, new_return)
        with open(filepath, 'w') as f:
            f.write(content)
        print(f"Fixed {filepath}")

# 把所有相關有 fallback 的檔案都修補
for file in glob.glob("/Users/w/Cursor/automation/**/*.py", recursive=True):
    fix_file(file)

