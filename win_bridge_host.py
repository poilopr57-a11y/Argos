"""
WinBridgeHost - Хост-бридж для Windows v2.5
Запускать в Windows, а не в Docker! Это "ворота" Аргоса.

Запуск:
    python win_bridge_host.py
"""
from fastapi import FastAPI, Header, HTTPException
import subprocess
import uvicorn
import os

app = FastAPI(title="Argos WinBridge Host", version="2.5")
TOKEN = os.getenv("ARGOS_BRIDGE_TOKEN", "Generation_2026")

@app.post("/exec")
async def run_cmd(data: dict, authorization: str = Header(None)):
    """Выполняет команду PowerShell на хосте"""
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=403, detail="Invalid token")
    
    cmd = data.get("cmd", "")
    if not cmd:
        return {"status": "error", "msg": "No command provided"}
    
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=60
        )
        return {
            "status": "ok",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "msg": "Command timeout"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.get("/health")
async def health():
    """Проверка доступности"""
    return {"status": "ok", "version": "2.5"}

if __name__ == "__main__":
    print("🌉 [WIN_BRIDGE] Хост запущен на http://0.0.0.0:5000")
    print(f"🔐 Токен: {TOKEN[:10]}...")
    uvicorn.run(app, host="0.0.0.0", port=5000)