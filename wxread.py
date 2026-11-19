"""抓取微信当前打开的聊天：自动向上翻页、去重拼接，输出 Markdown + JSON。

用法：
    python wxread.py                 # 抓最近 1 天
    python wxread.py --days 3        # 抓最近 3 天
    python wxread.py --screens 40    # 最多翻 40 屏

只读：只截图和发送滚轮消息，不点击、不输入、不发送任何消息。抓完会把聊天滚回最底部。
"""
import argparse
import difflib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

import wxcore
import wxparse

PX_PER_NOTCH = 70  # 实测微信每格滚轮约滚 70 像素
PAGE_RATIO = 0.55  # 每次翻过消息区高度的 55%，相邻两屏保留近一半重叠用于拼接


def notches_for(lay) -> int:
    """窗口越高，每次翻得越多：消息区高度决定翻页格数。"""
    return max(2, int((lay.y1 - lay.y0) * PAGE_RATIO / PX_PER_NOTCH))


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _same(a: wxparse.Msg, b: wxparse.Msg) -> bool:
    if a.kind != b.kind or a.side != b.side:
        return False
    x, y = _norm(a.text), _norm(b.text)
    if x == y:
        return True
    return difflib.SequenceMatcher(None, x, y).ratio() >= 0.8


def _overlap(older: list, acc: list) -> int:
    """older 的末尾与 acc 的开头最多重合几条。"""
    for k in range(min(len(older), len(acc)), 0, -1):
        if all(_same(older[len(older) - k + i], acc[i]) for i in range(k)):
            return k
    return 0


def snapshot(hwnd):
    img = wxcore.capture(hwnd)
    lay = wxparse.detect_layout(img)
    lines = wxparse.screen_lines(img, lay)
    return img, lines, lay, wxparse.parse_screen(img, lines, lay)


def _merge_into(dst: wxparse.Msg, src: wxparse.Msg):
    """同一条消息在另一屏里看到了更多信息（昵称、前面的时间分隔、引用），补到已有记录上。"""
    dst.sender = dst.sender or src.sender
    dst.time_before = dst.time_before or src.time_before
    dst.quote = dst.quote or src.quote


def read_chat(days: float = 1, max_screens: int = 30, log=print):
    hwnd = wxcore.find_wechat_window()
    img, lines, lay, items = snapshot(hwnd)
    title = wxparse.chat_title(lines, lay)
    cx, cy = (lay.x0 + lay.x1) // 2, (lay.y0 + lay.y1) // 2
    cutoff = datetime.now() - timedelta(days=days)

    # 只拼接消息本身；时间分隔记在它后面那条消息的 time_before 上，避免分隔线错位
    acc = [m for m in items if m.kind == "msg" and not (m.partial and m.top - lay.y0 < 30)]
    scrolled = stuck = gaps = 0
    try:
        for n in range(1, max_screens):
            oldest = next((t for t in (wxparse.parse_time(m.time_before) for m in acc if m.time_before) if t), None)
            if oldest and oldest < cutoff:
                log(f"已翻到 {oldest:%m-%d %H:%M}，早于要求的时间范围，停止")
                break
            step = notches_for(lay)
            wxcore.scroll(hwnd, cx, cy, step)
            scrolled += step
            time.sleep(0.6)  # 等待界面渲染/加载更早的消息
            img, lines, lay, items = snapshot(hwnd)
            screen = [m for m in items if m.kind == "msg" and not m.partial]
            k = _overlap(screen, acc)
            for i in range(k):
                _merge_into(acc[i], screen[len(screen) - k + i])
            new = screen[: len(screen) - k]
            if not new:
                stuck += 1
                if stuck >= 2:
                    log("画面不再变化，已到聊天记录顶部")
                    break
                continue
            stuck = 0
            if k == 0 and acc:
                gaps += 1
                acc[0].gap_before = True
            acc = new + acc
            log(f"第 {n + 1} 屏：新增 {len(new)} 条，累计 {len(acc)} 条")
    finally:
        wxcore.scroll(hwnd, cx, cy, -(scrolled + 20))  # 滚回最底部，恢复原来的视图

    is_group = any(m.side == "other" and m.sender for m in acc)
    out, cur_time = [], None
    for m in acc:
        if m.time_before:
            cur_time = wxparse.parse_time(m.time_before) or cur_time
        sender = m.sender or ("我" if m.side == "me" else ("?" if is_group else title))
        out.append({
            "time": cur_time.strftime("%Y-%m-%d %H:%M") if cur_time else "",
            "sender": sender,
            "text": m.text,
            **({"quote": m.quote} if m.quote else {}),
            **({"gap_before": True} if getattr(m, "gap_before", False) else {}),
        })
    # 只保留时间范围内的（最上面还没遇到时间分隔的几条无法判断，保留）
    out = [r for r in out if not r["time"] or datetime.strptime(r["time"], "%Y-%m-%d %H:%M") >= cutoff]
    return {"chat": title, "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "days": days, "gaps": gaps, "messages": out}


def to_markdown(data) -> str:
    lines = [f"# {data['chat']}", "",
             f"抓取时间：{data['captured_at']}　范围：最近 {data['days']} 天　共 {len(data['messages'])} 条", ""]
    last_time = None
    for r in data["messages"]:
        if r.get("gap_before"):
            lines.append("> [这里可能漏了几条：两屏之间没有找到重叠]")
        if r["time"] and r["time"] != last_time:
            lines += ["", f"**{r['time']}**", ""]
            last_time = r["time"]
        lines.append(f"- **{r['sender']}**：{r['text']}")
        if r.get("quote"):
            lines.append(f"  - 引用：{r['quote']}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="抓取微信当前打开的聊天记录（只读）")
    ap.add_argument("--days", type=float, default=1, help="抓最近几天，默认 1")
    ap.add_argument("--screens", type=int, default=30, help="最多翻几屏，默认 30")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"))
    a = ap.parse_args()
    data = read_chat(a.days, a.screens, log=lambda s: print(s, file=sys.stderr))
    os.makedirs(a.out, exist_ok=True)
    name = re.sub(r'[\\/:*?"<>|]', "_", data["chat"] or "chat") + datetime.now().strftime("_%Y%m%d_%H%M")
    with open(os.path.join(a.out, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, name + ".md"), "w", encoding="utf-8") as f:
        f.write(to_markdown(data))
    print(os.path.join(a.out, name + ".md"))


if __name__ == "__main__":
    main()
