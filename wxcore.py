"""微信窗口截图 + 系统文字识别 + 滚动。只读：不点击、不输入、不发送。"""
import asyncio
import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass

from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

try:  # 按真实像素取坐标，避免高分屏缩放造成错位
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]

WM_MOUSEWHEEL = 0x020A
PW_RENDERFULLCONTENT = 2


@dataclass
class Line:
    text: str
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self):
        return self.x + self.w

    @property
    def bottom(self):
        return self.y + self.h


def _process_name(pid: int) -> str:
    h = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    buf = ctypes.create_unicode_buffer(520)
    size = wt.DWORD(520)
    ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    kernel32.CloseHandle(h)
    return buf.value.rsplit("\\", 1)[-1].lower() if ok else ""


def find_wechat_window() -> int:
    """返回微信主窗口句柄（同进程里面积最大的可见窗口）。"""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _process_name(pid.value) in ("weixin.exe", "wechat.exe"):
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            title = ctypes.create_unicode_buffer(64)
            user32.GetWindowTextW(hwnd, title, 64)
            is_main = title.value in ("微信", "Weixin", "WeChat")
            found.append((is_main, (r.right - r.left) * (r.bottom - r.top), hwnd))
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        raise RuntimeError("没找到微信窗口：请先打开 PC 微信并保持窗口不要最小化")
    hwnd = max(found)[2]  # 主窗口优先（设置等子窗口标题不同），其次取面积最大的
    if user32.IsIconic(hwnd):
        raise RuntimeError("微信窗口被最小化了，请先还原")
    return hwnd


def window_rect(hwnd: int):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def capture(hwnd: int) -> Image.Image:
    """截取窗口自身的画面（被其它窗口遮住也能截到）。"""
    left, top, right, bottom = window_rect(hwnd)
    w, h = right - left, bottom - top
    wdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(wdc)
    bmp = gdi32.CreateCompatibleBitmap(wdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.SelectObject(mdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, wdc)
    return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")


def scroll(hwnd: int, x: int, y: int, notches: int):
    """向窗口内 (x, y) 处发送滚轮消息；notches 为正表示向上翻。不移动真实鼠标。"""
    import time
    left, top, _, _ = window_rect(hwnd)
    sx, sy = left + x, top + y
    step = 120 if notches > 0 else -120
    wparam = (step << 16) & 0xFFFFFFFF
    lparam = (sy & 0xFFFF) << 16 | (sx & 0xFFFF)
    for _ in range(abs(notches)):  # 微信每条滚轮消息只滚一格，所以逐格发送
        user32.PostMessageW(hwnd, WM_MOUSEWHEEL, wparam, lparam)
        time.sleep(0.04)


async def _ocr_async(img: Image.Image, scale: float):
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    if scale != 1:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    engine = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
    if engine is None:
        raise RuntimeError("系统没有安装简体中文文字识别组件")
    writer = DataWriter()
    writer.write_bytes(img.convert("RGBA").tobytes("raw", "BGRA"))
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, img.width, img.height)
    result = await engine.recognize_async(bitmap)
    lines = []
    for ln in result.lines:
        words = list(ln.words)
        if not words:
            continue
        x0 = min(w.bounding_rect.x for w in words)
        y0 = min(w.bounding_rect.y for w in words)
        x1 = max(w.bounding_rect.x + w.bounding_rect.width for w in words)
        y1 = max(w.bounding_rect.y + w.bounding_rect.height for w in words)
        lines.append(Line(_join(words), int(x0 / scale), int(y0 / scale),
                          int((x1 - x0) / scale), int((y1 - y0) / scale)))
    return lines


def _join(words) -> str:
    """中文字之间不加空格，英文/数字单词之间保留空格。"""
    out = ""
    for w in words:
        t = w.text
        if out and out[-1].isascii() and t[:1].isascii() and out[-1].isalnum() and t[:1].isalnum():
            out += " "
        out += t
    return out


_rapid = None


def _ocr_rapid(img: Image.Image) -> list[Line]:
    global _rapid
    import numpy as np
    if _rapid is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapid = RapidOCR()
    res, _ = _rapid(np.asarray(img.convert("RGB")))
    out = []
    for box, text, score in res or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x0, y0 = int(min(xs)), int(min(ys))
        out.append(Line(text, x0, y0, int(max(xs)) - x0, int(max(ys)) - y0))
    return out


def ocr(img: Image.Image, scale: float = 2.0) -> list[Line]:
    """识别图片文字，返回带坐标（原图像素）的行。

    优先用 RapidOCR（本地开源中文识别，准确率明显更高）；没装时退回 Windows 自带识别。
    """
    try:
        return _ocr_rapid(img)
    except ImportError:
        return asyncio.run(_ocr_async(img, scale))
