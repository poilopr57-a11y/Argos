import os, re
base = r"F:\debug\argoss"
path = os.path.join(base, "src", "ai_router.py")
with open(path, "r", encoding="utf-8") as f:
    content = f.read()
old = """    PROVIDERS = [
        \"openai\",
        \"gemini\",
        \"deepseek\",
        \"kimi\",
        \"cloudflare\",
        \"ollama\",
    ]"""
new = """    PROVIDERS = [
        \"ollama\",
        \"openai\",
        \"gemini\",
        \"deepseek\",
        \"kimi\",
        \"cloudflare\",
    ]"""
if old in content:
    content = content.replace(old, new)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("OK")
else:
    print("NOT FOUND")
