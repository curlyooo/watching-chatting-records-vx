"""把一屏截图的识别结果还原成结构化消息（针对 PC 微信 4.x 的默认浅色界面）。"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from PIL import Image

import numpy as np

from wxcore import Line, ocr


@dataclass
class Layout:
    x0: int  # 聊天区左边界
    x1: int  # 聊天区右边界
    y0: int  # 消息区上边界（标题栏分隔线下方）
    y1: int  # 消息区下边界（输入框上方）


@dataclass
class Msg:
    kind: str  # "msg" | "time" | "system"
    side: str = ""  # "me" | "other" | ""
    sender: str = ""
    text: str = ""
    quote: str = ""
    top: int = 0
    bottom: int = 0
    partial: bool = False  # 被屏幕上/下边缘截断，内容可能不完整
    time_before: str = ""  # 紧挨在这条消息上方的时间分隔文字
    gap_before: bool = False  # 拼接时这条之前可能漏了消息
    lines: list = field(default_factory=list, repr=False)


def _lum(px):
    r, g, b = px[:3]
    return (r * 299 + g * 587 + b * 114) // 1000


def detect_layout(img: Image.Image) -> Layout:
    """根据颜色找出右侧聊天区：左边界 = 会话列表变成浅色背景处；上下边界 = 横跨整个聊天区的分隔线。"""
    W, H = img.size
    px = img.load()
    probe_y = 40
    bg = px[W - 40, probe_y]
    x0 = next((x for x in range(W // 5, W // 2) if px[x, probe_y] == bg and px[x - 1, probe_y] != bg), W // 3)
    x1 = W - 1
    for x in range(W - 2, x0, -1):
        if px[x, H // 2] == bg:
            x1 = x
            break
    bg = px[(x0 + x1) // 2, probe_y]

    def sep_row(y):
        c = px[(x0 + x1) // 2, y]
        if c == bg or abs(_lum(c) - _lum(bg)) > 40:
            return False
        xs = range(x0 + 30, x1 - 30, 4)
        return sum(abs(_lum(px[x, y]) - _lum(c)) <= 3 for x in xs) >= 0.9 * len(xs)

    seps = [y for y in range(35, H - 20) if sep_row(y)]
    y0 = next((y for y in seps if y < H // 4), 80) + 1
    # 输入框上沿：中下部第一条分隔线
    y1 = next((y for y in seps if y > H // 3), H - 300) - 1
    return Layout(x0, x1, y0, y1)


def chat_title(lines: list[Line], lay: Layout) -> str:
    head = [l for l in lines if l.y < lay.y0 and lay.x0 < l.x < lay.x0 + (lay.x1 - lay.x0) * 0.7]
    if not head:
        return ""
    t = min(head, key=lambda l: l.x).text
    return re.sub(r"\s*\(\d+\)\s*$", "", t).strip()


def _bubble_masks(img: Image.Image):
    """返回 (我的气泡掩码, 对方气泡掩码)：绿色气泡 / 浅灰气泡。"""
    a = np.asarray(img).astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mine = (g > 200) & (g - r > 40) & (g - b > 40)
    other = (np.abs(r - 238) <= 4) & (np.abs(g - 238) <= 4) & (np.abs(b - 240) <= 4)
    return mine, other


def find_bubbles(img: Image.Image, lay: Layout):
    """按颜色找聊天气泡，返回 [(side, x0, y0, x1, y1)]，从上到下排列。"""
    mine, other = _bubble_masks(img)
    xa, xb = lay.x0 + 50, lay.x1 - 50
    found = []
    for side, mask in (("me", mine), ("other", other)):
        sub = mask[lay.y0:lay.y1 + 1, xa:xb]
        rows = sub.sum(axis=1) >= 20
        y = 0
        while y < len(rows):
            if not rows[y]:
                y += 1
                continue
            start = y
            while y < len(rows) and rows[y]:
                y += 1
            if y - start < 18:
                continue
            block = sub[start:y]
            cols = np.where(block.sum(axis=0) >= (y - start) * 0.5)[0]
            if len(cols) < 15:
                continue
            found.append((side, xa + int(cols[0]), lay.y0 + start, xa + int(cols[-1]), lay.y0 + y - 1))
    found.sort(key=lambda t: t[2])
    return found


def ocr_bubble(img: Image.Image, box) -> str:
    """整屏识别没认出字的气泡（通常是一两个字的短消息）：单独裁出、补边后再认一次。"""
    _, x0, y0, x1, y1 = box
    crop = img.crop((x0 + 8, y0 + 2, x1 - 8, y1 - 1))  # 去掉圆角和小尖角
    fill = crop.getpixel((1, 1))
    canvas = Image.new("RGB", (crop.width + 120, crop.height + 120), fill)
    canvas.paste(crop, (60, 60))
    lines = sorted(ocr(canvas), key=lambda l: (l.y, l.x))
    return "".join(l.text for l in lines)


def parse_screen(img: Image.Image, lines: list[Line], lay: Layout) -> list[Msg]:
    """一屏 → 从上到下的消息列表。气泡位置靠颜色找，气泡里的字是正文，气泡外的灰字是昵称/时间/引用。"""
    gray = img.convert("L")
    bubbles = find_bubbles(img, lay)
    width = lay.x1 - lay.x0
    mid = (lay.x0 + lay.x1) / 2

    def bubble_of(l: Line):
        cx, cy = (l.x + l.right) / 2, (l.y + l.bottom) / 2
        for i, b in enumerate(bubbles):
            if b[1] <= cx <= b[3] and b[2] <= cy <= b[4]:
                return i
        return None

    inside = {i: [] for i in range(len(bubbles))}
    loose = []  # 气泡外的灰字
    for l in lines:
        if not (lay.y0 - 2 <= l.y and l.bottom <= lay.y1 + 2 and lay.x0 + 50 < l.x and l.right < lay.x1 - 10):
            continue
        bi = bubble_of(l)
        if bi is not None:
            inside[bi].append(l)
            continue
        darkest = gray.crop((l.x, l.y, l.right + 1, l.bottom + 1)).getextrema()[0]
        if darkest < 90:
            continue  # 黑字却不在气泡里，多半是卡片/文件标题，最小版本先忽略
        loose.append(l)

    out: list[Msg] = []
    for bi, (side, x0, y0, x1, y1) in enumerate(bubbles):
        text = "".join(l.text for l in sorted(inside[bi], key=lambda l: (l.y, l.x)))
        cut = y0 - lay.y0 < 4 or lay.y1 - y1 < 4  # 被屏幕边缘截断的气泡，翻页后会完整出现
        if not text and not cut:
            text = ocr_bubble(img, bubbles[bi]) or "[短消息未识别]"
        out.append(Msg("msg", side=side, text=text, top=y0, bottom=y1,
                       sender="我" if side == "me" else ""))

    used = set()
    # 先认昵称：紧贴在对方气泡正上方、左边对齐的灰字
    for m, (side, x0, y0, x1, y1) in zip(out, bubbles):
        if side != "other":
            continue
        for i, l in enumerate(loose):
            if i not in used and 0 <= y0 - l.bottom <= 22 and abs(l.x - x0) <= 20:
                m.sender, m.top = l.text, l.y
                used.add(i)
                break
    # 再认引用：气泡正下方、与气泡对齐的灰字，第一行形如"昵称：内容"，可能折成多行
    order = sorted(range(len(loose)), key=lambda i: loose[i].y)
    for m, (side, x0, y0, x1, y1) in zip(out, bubbles):
        last = y1
        for i in order:
            l = loose[i]
            if i in used or not (abs(l.x - x0) <= 25 or abs(l.right - x1) <= 25):
                continue
            first = not m.quote
            if first and 0 <= l.y - y1 <= 30 and re.search(r"[:：]", l.text):
                pass
            elif not first and 0 <= l.y - last <= 22:
                pass
            else:
                continue
            m.quote += l.text
            m.bottom = max(m.bottom, l.bottom)
            last = l.bottom
            used.add(i)
    # 剩下居中的是时间分隔或系统提示（撤回、加群等）
    for i, l in enumerate(loose):
        if i not in used and abs((l.x + l.right) / 2 - mid) < width * 0.08:
            kind = "time" if re.search(r"\d{1,2}[:：]\d{2}", l.text) else "system"
            out.append(Msg(kind, text=l.text, top=l.y, bottom=l.bottom))

    is_group = any(m.side == "other" and m.sender for m in out)
    if is_group:  # 群聊里漏认的昵称（太短的名字整屏识别容易漏）：单独裁出气泡上方那一条再认
        for m, (side, x0, y0, x1, y1) in zip(out, bubbles):
            if side == "other" and not m.sender and y0 - lay.y0 >= 30:
                strip = img.crop((x0, y0 - 24, min(x0 + 260, lay.x1), y0 - 2))
                canvas = Image.new("RGB", (strip.width + 80, strip.height + 60), strip.getpixel((0, 0)))
                canvas.paste(strip, (40, 30))
                name = "".join(l.text for l in sorted(ocr(canvas), key=lambda l: l.x))
                if name:
                    m.sender, m.top = name, y0 - 24
    out.sort(key=lambda m: m.top)
    prev = None
    for m in out:
        if m.top - lay.y0 < 4 or lay.y1 - m.bottom < 4:
            m.partial = True
        # 群聊里靠近顶部、没认到昵称的对方消息：昵称已滚出屏幕，留给下一屏补全
        if is_group and m.kind == "msg" and m.side == "other" and not m.sender and m.top - lay.y0 < 30:
            m.partial = True
        if m.kind == "msg" and prev is not None and prev.kind == "time" and not prev.partial:
            m.time_before = prev.text
        prev = m
    return out


def screen_lines(img: Image.Image, lay: Layout) -> list[Line]:
    """只识别聊天区（含标题栏），比整窗识别快，坐标换算回整窗。"""
    lines = ocr(img.crop((lay.x0, 0, lay.x1, lay.y1)))
    for l in lines:
        l.x += lay.x0
    return lines


_WEEK = "一二三四五六日天"


def parse_time(s: str, now: datetime | None = None) -> datetime | None:
    """把微信的时间分隔文字转成 datetime：16:57 / 昨天 18:20 / 星期三 10:00 / 11月10日 09:12 / 2025年3月1日 8:00。"""
    now = now or datetime.now()
    s = s.replace(" ", "")
    m = re.search(r"(\d{1,2})[:：](\d{2})", s)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    pre = s[: m.start()]
    if (d := re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", pre)):
        day = datetime(int(d.group(1)), int(d.group(2)), int(d.group(3)))
    elif (d := re.search(r"(\d{1,2})月(\d{1,2})日", pre)) or (d := re.search(r"(\d{1,2})/(\d{1,2})", pre)):
        day = datetime(now.year, int(d.group(1)), int(d.group(2)))
        if day > now:
            day = day.replace(year=now.year - 1)
    elif "前天" in pre:
        day = now - timedelta(days=2)
    elif "昨天" in pre:
        day = now - timedelta(days=1)
    elif (d := re.search(r"(?:星期|周)([一二三四五六日天])", pre)):
        wd = min(_WEEK.index(d.group(1)), 6)
        back = (now.weekday() - wd) % 7 or 7
        day = now - timedelta(days=back)
    else:
        day = now
    try:
        return day.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except ValueError:
        return None
