#!/usr/bin/env python3
"""
AudioMotion 區網頻譜串流伺服器 (Windows)

擷取「預設輸出裝置」的 WASAPI loopback(等同錄下喇叭正在播的聲音),
不更動音效卡/預設裝置/音量/增強功能,透過 WebSocket 把原始 PCM 送給手機瀏覽器,
由手機端用 audioMotion-analyzer 即時渲染。

只依賴 PyAudioWPatch(pip install PyAudioWPatch);HTTP/WebSocket 皆為標準函式庫實作。
省電設計:沒有任何手機連線時不開啟擷取;最後一個連線離開即關閉。

用法:
    python server.py                # 擷取預設輸出裝置
    python server.py --list         # 列出可用 loopback 裝置
    python server.py --device Realtek
    python server.py --fake         # 不擷取,用合成音測試網頁
"""
import argparse
import asyncio
import base64
import hashlib
import json
import math
import mimetypes
import socket
import struct
import sys
import threading
import time
from array import array
from pathlib import Path

WEB_DIR = (Path(__file__).parent / "web").resolve()
QUEUE_MAX = 6  # 每個用戶端最多緩衝的音訊區塊,超過就丟舊的(保持低延遲)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PRESETS_FILE = Path(__file__).parent / "presets.json"  # 手機端儲存的自訂預設
MAX_BODY = 256 * 1024


# ----------------------------------------------------------------------------
# 音源:WASAPI loopback / 測試音
# ----------------------------------------------------------------------------
class LoopbackSource:
    """PyAudioWPatch WASAPI loopback,只讀取,不改任何系統設定。"""

    def __init__(self, device_hint=None, frames=1024):
        self.device_hint = device_hint
        self.frames = frames
        self.pa = None
        self.stream = None
        self.rate = 48000
        self.channels = 2
        self.dev_name = ""

    def _pick_device(self, pa):
        if self.device_hint:
            hint = self.device_hint.lower()
            for lb in pa.get_loopback_device_info_generator():
                if hint in lb["name"].lower():
                    return lb
            raise RuntimeError(f"找不到名稱含 '{self.device_hint}' 的 loopback 裝置")
        return pa.get_default_wasapi_loopback()  # 預設輸出裝置的 loopback

    def start(self, on_chunk):
        import pyaudiowpatch as pyaudio

        self.pa = pyaudio.PyAudio()
        dev = self._pick_device(self.pa)
        self.dev_name = dev["name"]
        self.rate = int(dev["defaultSampleRate"])
        src_ch = int(dev["maxInputChannels"])
        self.channels = 1 if src_ch == 1 else 2

        def cb(in_data, frame_count, time_info, status):
            if src_ch > 2:  # 5.1/7.1 等,只取前兩聲道(L/R)
                a = array("h")
                a.frombytes(in_data)
                out = array("h", bytes(2 * (len(a) // src_ch) * 2))
                out[0::2] = a[0::src_ch]
                out[1::2] = a[1::src_ch]
                in_data = out.tobytes()
            on_chunk(in_data)
            return (None, pyaudio.paContinue)

        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=src_ch,
            rate=self.rate,
            input=True,
            input_device_index=dev["index"],
            frames_per_buffer=self.frames,
            stream_callback=cb,
        )

    def stop(self):
        try:
            if self.stream:
                self.stream.stop_stream()
                self.stream.close()
        finally:
            self.stream = None
            if self.pa:
                self.pa.terminate()
                self.pa = None


class FakeSource:
    """測試用:掃頻正弦 + 節奏性低頻,即時速度產生。"""

    def __init__(self, frames=1024):
        self.frames = frames
        self.rate = 48000
        self.channels = 2
        self.dev_name = "FAKE (synthetic)"
        self._run = False
        self._t = None

    def start(self, on_chunk):
        self._run = True

        def loop():
            n = 0
            pl = pr = 0.0
            period = self.frames / self.rate
            nxt = time.perf_counter()
            while self._run:
                out = array("h", bytes(self.frames * 4))
                for k in range(self.frames):
                    t = (n + k) / self.rate
                    fl = 100 * (2 ** (7 * ((t / 8) % 1)))          # 左:100Hz→12.8kHz 往上掃
                    fr = 100 * (2 ** (7 * (1 - ((t / 8) % 1))))    # 右:12.8kHz→100Hz 往下掃
                    pl += 2 * math.pi * fl / self.rate
                    pr += 2 * math.pi * fr / self.rate
                    kick = 0.4 * math.sin(2 * math.pi * 55 * t) * math.exp(-((t * 2) % 1) * 8)
                    hat = 0.25 * math.sin(2 * math.pi * 6000 * t) * math.exp(-(((t * 2) + 0.5) % 1) * 12)
                    out[2 * k] = int((0.35 * math.sin(pl) + kick) * 26000)       # 左:掃頻 + 大鼓
                    out[2 * k + 1] = int((0.35 * math.sin(pr) + hat) * 26000)    # 右:反向掃頻 + 鈸
                on_chunk(out.tobytes())
                n += self.frames
                nxt += period
                d = nxt - time.perf_counter()
                if d > 0:
                    time.sleep(d)

        self._t = threading.Thread(target=loop, daemon=True)
        self._t.start()

    def stop(self):
        self._run = False
        if self._t:
            self._t.join(timeout=1)


# ----------------------------------------------------------------------------
# Hub:管理連線與擷取的開關
# ----------------------------------------------------------------------------
class Hub:
    def __init__(self, make_source):
        self.make_source = make_source
        self.clients = {}  # key -> asyncio.Queue
        self.source = None
        self.loop = None
        self.lock = asyncio.Lock()

    def _on_chunk(self, data: bytes):  # 在擷取執行緒呼叫
        if self.loop:
            self.loop.call_soon_threadsafe(self._broadcast, data)

    def _broadcast(self, data: bytes):
        for q in self.clients.values():
            if q.full():
                try:
                    q.get_nowait()  # 丟掉最舊的
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(data)

    async def add(self, key):
        async with self.lock:
            q = asyncio.Queue(maxsize=QUEUE_MAX)
            if self.source is None:
                src = self.make_source()
                await self.loop.run_in_executor(None, src.start, self._on_chunk)
                self.source = src
                print(f"[capture] 開始擷取: {src.dev_name} ({src.rate} Hz, {src.channels} ch)", flush=True)
            self.clients[key] = q
            return q

    async def remove(self, key):
        async with self.lock:
            self.clients.pop(key, None)
            if not self.clients and self.source is not None:
                src, self.source = self.source, None
                await self.loop.run_in_executor(None, src.stop)
                print("[capture] 無連線,已停止擷取(省電)", flush=True)


# ----------------------------------------------------------------------------
# 極簡 HTTP + WebSocket(伺服器 → 客戶端單向串流)
# ----------------------------------------------------------------------------
def ws_frame(opcode: int, payload: bytes) -> bytes:
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


async def read_ws_frame(reader):
    b1, b2 = await reader.readexactly(2)
    opcode = b1 & 0x0F
    n = b2 & 0x7F
    if n == 126:
        (n,) = struct.unpack("!H", await reader.readexactly(2))
    elif n == 127:
        (n,) = struct.unpack("!Q", await reader.readexactly(8))
    mask = await reader.readexactly(4) if b2 & 0x80 else None
    data = await reader.readexactly(n) if n else b""
    if mask:
        data = bytes(c ^ mask[i & 3] for i, c in enumerate(data))
    return opcode, data


async def http_reply(writer, status, body=b"", ctype="text/plain; charset=utf-8", extra=""):
    reason = {200: "OK", 404: "Not Found", 400: "Bad Request", 405: "Method Not Allowed"}.get(status, "")
    head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            f"Cache-Control: no-cache\r\nConnection: close\r\n{extra}\r\n")
    writer.write(head.encode() + body)
    await writer.drain()


async def serve_static(writer, path):
    rel = path.split("?", 1)[0]
    rel = "index.html" if rel in ("", "/") else rel.lstrip("/")
    f = (WEB_DIR / rel).resolve()
    if WEB_DIR not in f.parents or not f.is_file():
        return await http_reply(writer, 404, b"not found")
    ctype = {".js": "text/javascript", ".mjs": "text/javascript"}.get(f.suffix) or (
        mimetypes.guess_type(f.name)[0] or "application/octet-stream")
    if ctype.startswith("text/"):
        ctype += "; charset=utf-8"
    await http_reply(writer, 200, f.read_bytes(), ctype)


async def handle(hub, reader, writer):
    peer = writer.get_extra_info("peername")
    peer_s = peer[0] if peer else "?"
    try:
        req = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    except Exception:
        writer.close()
        return
    lines = req.decode("latin-1").split("\r\n")
    try:
        method, path, _ = lines[0].split(" ", 2)
    except ValueError:
        writer.close()
        return
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    try:
        route = path.split("?")[0]
        if route == "/api/presets" and method in ("GET", "POST"):
            await presets_api(reader, writer, method, headers)
        elif method != "GET":
            await http_reply(writer, 405, b"GET only")
        elif route == "/ws" and headers.get("upgrade", "").lower() == "websocket":
            await ws_session(hub, reader, writer, headers, peer_s)
            return
        else:
            await serve_static(writer, path)
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def presets_api(reader, writer, method, headers):
    if method == "GET":
        try:
            body = PRESETS_FILE.read_bytes()
        except OSError:
            body = b"{}"
        return await http_reply(writer, 200, body, "application/json; charset=utf-8")
    try:
        n = int(headers.get("content-length", "0"))
        if not 0 < n <= MAX_BODY:
            raise ValueError("size")
        data = json.loads((await reader.readexactly(n)).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("type")
    except (ValueError, UnicodeDecodeError):
        return await http_reply(writer, 400, b"bad json")
    tmp = PRESETS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(PRESETS_FILE)  # 原子寫入
    await http_reply(writer, 200, b"{}", "application/json")


async def ws_session(hub, reader, writer, headers, peer_s):
    key = headers.get("sec-websocket-key")
    if not key:
        return await http_reply(writer, 400, b"bad ws request")
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
    await writer.drain()

    token = object()
    try:
        q = await hub.add(token)
    except Exception as e:  # 例如沒有輸出裝置
        print("[error]", e, flush=True)
        writer.write(ws_frame(0x1, json.dumps({"error": str(e)}).encode()))
        writer.write(ws_frame(0x8, b""))
        await writer.drain()
        return

    src = hub.source
    writer.write(ws_frame(0x1, json.dumps({"rate": src.rate, "channels": src.channels, "format": "s16le"}).encode()))
    await writer.drain()
    print(f"[ws] 連線 {peer_s}  (共 {len(hub.clients)})", flush=True)

    async def pump():
        while True:
            data = await q.get()
            writer.write(ws_frame(0x2, data))
            await writer.drain()

    async def listen():  # 處理 ping / close
        while True:
            op, data = await read_ws_frame(reader)
            if op == 0x8:
                return
            if op == 0x9:
                writer.write(ws_frame(0xA, data))

    tasks = [asyncio.create_task(pump()), asyncio.create_task(listen())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await hub.remove(token)
        print(f"[ws] 離線 {peer_s}  (剩 {len(hub.clients)})", flush=True)


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # 不會真的送封包
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def list_devices():
    import pyaudiowpatch as pyaudio

    pa = pyaudio.PyAudio()
    try:
        d = pa.get_default_wasapi_loopback()
        print(f"預設: {d['name']}")
        print("可用 loopback 裝置:")
        for lb in pa.get_loopback_device_info_generator():
            print(f"  - {lb['name']}  ({int(lb['defaultSampleRate'])} Hz, {lb['maxInputChannels']} ch)")
    finally:
        pa.terminate()


async def amain(args):
    if args.fake:
        make = lambda: FakeSource(args.frames)  # noqa: E731
    else:
        make = lambda: LoopbackSource(args.device, args.frames)  # noqa: E731
    hub = Hub(make)
    hub.loop = asyncio.get_running_loop()
    server = None
    for port in range(args.port, args.port + 20):  # 埠被佔用就自動往後找
        try:
            server = await asyncio.start_server(lambda r, w: handle(hub, r, w), args.host, port)
            break
        except OSError as e:
            if e.errno not in (10048, 98, 48):
                raise
            print(f"[port] {port} 已被佔用,嘗試 {port + 1}", flush=True)
    if server is None:
        raise SystemExit("找不到可用的連接埠,請用 --port 指定")
    if port != args.port:
        args.port = port

    print("AudioMotion 區網頻譜串流  (Ctrl+C 結束)")
    print("在同一 Wi-Fi 的手機瀏覽器開啟:")
    for ip in lan_ips():
        print(f"    http://{ip}:{args.port}/")
    print(flush=True)
    async with server:
        await server.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--device", help="loopback 裝置名稱關鍵字(預設用系統預設輸出裝置)")
    ap.add_argument("--frames", type=int, default=1024, help="每個區塊的取樣數(預設 1024 ≈ 21ms@48k)")
    ap.add_argument("--list", action="store_true", help="列出 loopback 裝置後離開")
    ap.add_argument("--fake", action="store_true", help="使用合成測試音,不擷取真實聲音")
    args = ap.parse_args()
    if args.list:
        return list_devices()
    asyncio.run(amain(args))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
