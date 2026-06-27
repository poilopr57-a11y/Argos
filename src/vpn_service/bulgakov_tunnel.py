"""
Bulgakov Stealth Tunnel — стеганографический VPN через Мастер и Маргарита.
Каждый байт данных кодируется как координаты (страница, строка, слово) в тексте книги.
Трафик неотличим от чтения литературы онлайн.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import struct
import time
from collections import OrderedDict
from typing import Optional

import requests


class BulgakovCodec:
    """Парсит PDF 'Мастер и Маргарита' в координатную сетку."""

    def __init__(self, pdf_path: str):
        self.words: list[tuple[int, int, int, str]] = []  # (page, line, word_idx, text)
        self._parse_pdf(pdf_path)
        # Строим обратный индекс: первая буква слова → координаты
        self._by_first_letter: dict[str, list[int]] = {}
        for i, (_, _, _, word) in enumerate(self.words):
            ch = word[0].lower() if word else " "
            self._by_first_letter.setdefault(ch, []).append(i)

    def _parse_pdf(self, path: str) -> None:
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(path)
            for page_num in range(min(len(doc), 200)):
                page = doc[page_num]
                text = page.get_text("text")
                lines = text.split("\n")
                for line_num, line in enumerate(lines):
                    tokens = line.split()
                    for word_idx, word in enumerate(tokens):
                        clean = "".join(c for c in word if c.isalpha())
                        if len(clean) >= 2:
                            self.words.append((page_num + 1, line_num + 1, word_idx + 1, clean.lower()))
            doc.close()
        except ImportError:
            self._fallback_text()
        if len(self.words) < 256:
            self._fallback_text()

    def _fallback_text(self) -> None:
        master_text = (
            "однажды весною в час небывало жаркого заката в москве на патриарших прудах "
            "появились два гражданина первый из них одетый в летнюю серенькую пару был "
            "маленького роста упитан лыс свою приличную шляпу пирожком нес в руке а на "
            "хорошо выбритом лице его помещались сверхъестественных размеров очки в "
            "черной роговой оправе второй плечистый рыжеватый вихрастый молодой человек "
            "в заломленной на затылок клетчатой кепке был в ковбойке жеваных белых "
            "брюках и в черных тапочках первый был не кто иной как михаил александрович "
            "берлиоз председатель правления одной из крупнейших московских литературных "
            "ассоциаций сокращенно именуемой массолит и редактор толстого художественного "
            "журнала а молодой спутник его поэт иван николаевич понырев пишущий под "
            "псевдонимом бездомный попав в тень чуть позеленевших лип писатели бросились "
            "первым долгом к пестро раскрашенной будочке с надписью пиво и воды "
            "да следует отметить первый удар этого ужасного майского вечера не только "
            "в будочке но и во всей аллее параллельной малой бронной улице не оказалось "
            "ни одного человека в тот же час когда уже казалось и дышать нечем когда "
            "солнце раскалив москву в сухом тумане валилось кудато за садовое кольцо "
            "никто не пришел под липы никто не сел на скамейку пуста была аллея "
            "дайте нарзану попросил берлиоз нарзану нету ответила женщина в будочке "
            "пиво есть только абрикосовая женщина ответила берлиоз нету есть абрикосовая "
            "только теплая давайте давайте давайте берлиоз достал из кармана портсигар "
            "закурил и поэт здесь же сочинял стихи посвященные какомуто очередному "
            "событию и вдруг изпод земли вырос человек вроде бы и похожий на человека "
            "только очень странный гражданин ростом в сажень но в плечах узок худ "
            "неимоверно и физиономия прошу заметить издевательская волосы русые брови "
            "черные но одно выше другого глаз правый черный левый почемуто зеленый "
            "брови черные но одна выше другой словом иностранец пройдя мимо скамейки "
            "на которой помещались редактор и поэт иностранец покосился на них "
            "остановился и вдруг уселся на соседней скамейке в двух шагах от приятелей "
            "немец подумал берлиоз англичанин подумал бездомный нет больше француз "
            "определил берлиоз поляк нет скорее всего иностранец был именно немец "
            "или поляк ибо одет был с иголочки в серый прекрасный костюм в заграничные "
            "туфли иностранец откинувшись на спинку скамейки с любопытством оглядывал "
            "поэта и редактора вы атеисты неожиданно спросил иностранец да мы "
            "атеисты улыбнувшись ответил берлиоз а бездомный подумал вот привязался "
        )
        words = master_text.split()
        for i, word in enumerate(words):
            line_num = i // 10 + 1
            word_idx = i % 10 + 1
            page_num = i // 100 + 1
            self.words.append((page_num, line_num, word_idx, word.lower()))

    def word_count(self) -> int:
        return len(self.words)

    def encode_byte(self, byte: int) -> tuple[int, int, int, str]:
        """Кодирует один байт (0-255) в координаты слова."""
        pool_size = len(self.words) // 256
        if pool_size < 1:
            raise ValueError("Недостаточно слов в тексте для кодирования")
        idx = byte * pool_size + random.randint(0, pool_size - 1)
        idx = min(idx, len(self.words) - 1)
        return self.words[idx]

    def decode_coordinate(self, page: int, line: int, word_idx: int) -> int:
        """Декодирует координаты обратно в байт."""
        base_idx = None
        for i, (p, l, w, _) in enumerate(self.words):
            if p == page and l == line and w == word_idx:
                base_idx = i
                break
        if base_idx is None:
            # Приблизительный поиск
            for i, (p, l, w, _) in enumerate(self.words):
                if p == page and l == line:
                    base_idx = i + word_idx - w
                    break
            if base_idx is None:
                return 0
        pool_size = max(1, len(self.words) // 256)
        byte_val = min(255, base_idx // pool_size)
        return byte_val

    def encode_data(self, data: bytes) -> list[dict]:
        """Кодирует блок данных в список координат."""
        result = []
        for i, byte in enumerate(data):
            page, line, widx, word = self.encode_byte(byte)
            result.append({
                "i": i,
                "p": page,
                "l": line,
                "w": widx,
                "word": word,
            })
        return result

    def decode_data(self, coords: list[dict]) -> bytes:
        """Декодирует координаты обратно в данные."""
        coords.sort(key=lambda x: x.get("i", 0))
        result = bytearray()
        for c in coords:
            result.append(self.decode_coordinate(c["p"], c["l"], c["w"]))
        return bytes(result)


# ============================================================
# HTTP Tunnel — клиент и сервер
# ============================================================

class BulgakovTunnelServer:
    """Серверная часть туннеля (запускается на GCP VM)."""

    def __init__(self, codec: BulgakovCodec, listen_port: int = 8088):
        self.codec = codec
        self.port = listen_port
        self._buffer: list[dict] = []

    def handle_request(self, page: int, line: int, word_idx: int, seq: int, total: int) -> dict:
        """Обрабатывает один 'читательский' запрос."""
        self._buffer.append({"p": page, "l": line, "w": word_idx, "i": seq})
        if seq >= total - 1:
            data = self.codec.decode_data(self._buffer)
            self._buffer.clear()
            response = self._process_packet(data)
            resp_coords = self.codec.encode_data(response)[:total]
            return {"status": "ok", "data": resp_coords}
        return {"status": "ack"}

    def _process_packet(self, data: bytes) -> bytes:
        """Обрабатывает реальный сетевой пакет."""
        try:
            # Простой echo для теста; в продакшене — socks/tun прокси
            return b"ARGOS:" + data[:64]
        except Exception:
            return b"ERROR"


class BulgakovTunnelClient:
    """Клиент туннеля."""

    def __init__(self, codec: BulgakovCodec, server_url: str):
        self.codec = codec
        self.server = server_url.rstrip("/")

    def send_data(self, data: bytes, packet_size: int = 16) -> bytes:
        """Отправляет данные через стеганографический туннель."""
        encoded = self.codec.encode_data(data)
        total = len(encoded)
        response_coords = []

        for seq, coord in enumerate(encoded):
            url = (
                f"{self.server}/read"
                f"?page={coord['p']}&line={coord['l']}&word={coord['w']}"
                f"&seq={seq}&total={total}"
            )
            try:
                r = requests.get(url, timeout=10, headers={
                    "User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/120.0 Mobile",
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": "https://www.litres.ru/mihail-bulgakov/master-i-margarita/",
                })
                resp = r.json()
                if resp.get("status") == "ok" and resp.get("data"):
                    response_coords.extend(resp["data"])
            except Exception:
                pass
            time.sleep(0.05)  # Эмуляция чтения

        if response_coords:
            return self.codec.decode_data(response_coords)
        return b""


def create_tunnel_server_app(codec: BulgakovCodec):
    """Создаёт FastAPI приложение для туннеля."""
    from fastapi import FastAPI, Query
    from fastapi.responses import JSONResponse

    app = FastAPI()
    tunnel = BulgakovTunnelServer(codec)

    @app.get("/read")
    async def read_page(
        page: int = Query(...),
        line: int = Query(...),
        word: int = Query(...),
        seq: int = Query(0),
        total: int = Query(1),
    ):
        result = tunnel.handle_request(page, line, word, seq, total)
        return JSONResponse(content=result)

    @app.get("/health")
    async def health():
        return {"tunnel": "bulgakov", "words": codec.word_count()}

    return app
