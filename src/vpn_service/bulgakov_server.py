"""
Bulgakov Stealth Tunnel Server — FastAPI приложение для GCP VM.
Принимает "читательские" запросы и декодирует VPN-трафик.
"""
import json, os, sys, time

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, HTMLResponse

PDF_PATH = os.getenv("BULGAKOV_PDF", "/opt/bulgakov/master.pdf")

# Lazy init
_codec = None
_tunnel = None


def _get_codec():
    global _codec
    if _codec is None:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from src.vpn_service.bulgakov_tunnel import BulgakovCodec
        _codec = BulgakovCodec(PDF_PATH)
    return _codec


def _get_tunnel():
    global _tunnel
    if _tunnel is None:
        from src.vpn_service.bulgakov_tunnel import BulgakovTunnelServer
        _tunnel = BulgakovTunnelServer(_get_codec())
    return _tunnel


app = FastAPI(title="Bulgakov Stealth Tunnel")


@app.get("/health")
async def health():
    codec = _get_codec()
    return {"tunnel": "bulgakov", "words": codec.word_count(), "status": "active"}


@app.get("/read")
async def read_page(
    page: int = Query(...),
    line: int = Query(...),
    word: int = Query(...),
    seq: int = Query(0),
    total: int = Query(1),
):
    """Маскируется под чтение книги онлайн."""
    tunnel = _get_tunnel()
    result = tunnel.handle_request(page, line, word, seq, total)
    response = JSONResponse(content=result)
    response.headers["Content-Type"] = "application/json"
    response.headers["X-Book"] = "Master-i-Margarita"
    return response


@app.get("/")
async def index(request: Request):
    """Корневая страница — маскировка под книжный сайт."""
    return HTMLResponse("""
    <!DOCTYPE html><html lang="ru"><head>
    <meta charset="utf-8"><title>Мастер и Маргарита — читать онлайн</title>
    <meta name="description" content="Михаил Булгаков. Мастер и Маргарита. Читать онлайн бесплатно.">
    </head><body style="font-family:serif;max-width:800px;margin:0 auto;padding:20px">
    <h1>Михаил Булгаков</h1><h2>Мастер и Маргарита</h2>
    <p>Роман. Читать онлайн.</p>
    <p style="color:#888">Сервер работает. Используйте клиент Bulgakov Tunnel для подключения.</p>
    </body></html>
    """)
