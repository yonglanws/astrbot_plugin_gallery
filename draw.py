"""自包含的 Pillow 声明式布局工具包。

忠实移植自 lunabot 的 draw/plot.py + draw/painter.py，提供相同的声明式 API：
    with Canvas(bg=FillBg(...)).set_padding(...) as c:
        with Grid(row_count=...).set_sep(...).set_item_align('t'):
            VSplit()... / TextBox(...) / ImageBox(...) / Spacer(...)

仅实现画廊用到的子集：FillBg / Widget / Frame / Canvas / HSplit / VSplit /
Grid / TextBox / ImageBox / Spacer / TextStyle。用 Pillow 的 ImageDraw 直接绘制，
不依赖 lunabot 的全局配置、字体缓存或 painter 线程池。

字体采用自动探测：优先使用配置/系统中的思源黑体，回退到系统 CJK 字体，
最后回退到 PIL 默认字体（中文可能显示为方块，仅作兜底）。
"""

from __future__ import annotations

import asyncio
import os
import threading
import contextvars
from dataclasses import dataclass
from typing import Union, Tuple, List, Optional, Callable

from PIL import Image, ImageDraw, ImageFont

# ==================== 常量与颜色 ====================

DEFAULT_PADDING = 0
DEFAULT_MARGIN = 0
DEFAULT_SEP = 8

BLACK = (0, 0, 0, 255)
WHITE = (255, 255, 255, 255)
TRANSPARENT = (0, 0, 0, 0)
SHADOW = (0, 0, 0, 150)
THUMBNAIL_BG_COLOR = (230, 240, 255, 255)

Color = Tuple[int, int, int, int]
Size = Tuple[int, int]
Position = Tuple[int, int]

ALIGN_MAP = {
    'c': ('c', 'c'), 'l': ('l', 'c'), 'r': ('r', 'c'),
    't': ('c', 't'), 'b': ('c', 'b'),
    'tl': ('l', 't'), 'tr': ('r', 't'), 'bl': ('l', 'b'), 'br': ('r', 'b'),
    'lt': ('l', 't'), 'lb': ('l', 'b'), 'rt': ('r', 't'), 'rb': ('r', 'b'),
}

DEFAULT_FONT = "SourceHanSansCN-Regular"
DEFAULT_BOLD_FONT = "SourceHanSansCN-Bold"

# ==================== 字体管理 ====================

_font_cache: dict[str, ImageFont.FreeTypeFont] = {}
_font_searched = False
_font_candidates: list[str] = []


def _collect_font_candidates() -> list[str]:
    """收集可能的字体路径（插件自带字体优先，回退系统 CJK 字体）。"""
    global _font_searched
    if _font_searched:
        return _font_candidates
    _font_searched = True
    cands: list[str] = []
    # 1. 插件自带字体（与 draw.py 同目录，优先级最高）
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("MiSans-Medium.ttf", "MiSans-Bold.ttf"):
        cands.append(os.path.join(here, name))
    # 2. data 目录下的字体
    for base in ("data/utils/fonts", "data/plugin_data/astrbot_plugin_gallery/fonts"):
        for name in ("MiSans-Medium.ttf", "MiSans-Bold.ttf"):
            cands.append(os.path.join(base, name))
    # 3. 系统 CJK 字体（回退）
    win_fonts = os.environ.get("WINDIR", r"C:\Windows") + r"\Fonts"
    cands += [
        os.path.join(win_fonts, "msyh.ttc"),       # 微软雅黑
        os.path.join(win_fonts, "msyhbd.ttc"),     # 微软雅黑粗体
        os.path.join(win_fonts, "simhei.ttf"),     # 黑体
        os.path.join(win_fonts, "simsun.ttc"),     # 宋体
        os.path.join(win_fonts, "Deng.ttf"),       # 等线
    ]
    cands += [
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    _font_candidates.extend(cands)
    return _font_candidates


def _get_pil_font(font_name: str, size: int) -> ImageFont.FreeTypeFont:
    """加载字体，带缓存与回退。粗体与常规均优先使用插件自带的 MiSans 字体。"""
    key = f"{font_name}_{size}"
    if key in _font_cache:
        return _font_cache[key]
    _collect_font_candidates()
    bold = "Bold" in font_name or "bold" in font_name
    font = None
    # 优先 MiSans-Bold（粗体时）或 MiSans-Medium（常规时）
    for path in _font_candidates:
        base = os.path.basename(path).lower()
        is_bold_file = "bold" in base
        if bold and not is_bold_file:
            continue
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if font is None:
        # 不区分粗细再找一次
        for path in _font_candidates:
            if os.path.exists(path):
                try:
                    font = ImageFont.truetype(path, size)
                    break
                except Exception:
                    continue
    if font is None:
        # 最后回退到默认字体（中文可能无法显示）
        font = ImageFont.load_default(size if size <= 32 else 32)
    _font_cache[key] = font
    return font


def get_text_size(font: ImageFont.FreeTypeFont, text: str) -> Size:
    if not text:
        return (0, 0)
    bbox = font.getbbox(text)
    return (bbox[2] - bbox[0], bbox[3] - bbox[1])


def get_text_width(font: ImageFont.FreeTypeFont, text: str) -> int:
    return get_text_size(font, text)[0]


# ==================== Painter（直接绘制） ====================

class Painter:
    """直接在一张 RGBA 图上绘制，维护 offset/size 区域栈。"""

    def __init__(self, img: Image.Image = None, size: Size = None):
        if img is None:
            img = Image.new("RGBA", size, (0, 0, 0, 0))
        self.img = img
        self.size = img.size
        self.offset = (0, 0)
        self.w = self.size[0]
        self.h = self.size[1]
        self.region_stack: list[tuple[Position, Size]] = []
        self._draw = ImageDraw.Draw(self.img)

    async def get(self, cache_key: str = None) -> Image.Image:
        return self.img

    def move_region(self, dlt: Position, size: Size = None):
        offset = (self.offset[0] + dlt[0], self.offset[1] + dlt[1])
        size = size or self.size
        self.region_stack.append((self.offset, self.size))
        self.offset = offset
        self.size = size
        self.w = size[0]
        self.h = size[1]

    def shrink_region(self, dlt: Position):
        pos = (self.offset[0] + dlt[0], self.offset[1] + dlt[1])
        size = (self.size[0] - dlt[0] * 2, self.size[1] - dlt[1] * 2)
        self.region_stack.append((self.offset, self.size))
        self.offset = pos
        self.size = size
        self.w = size[0]
        self.h = size[1]

    def restore_region(self, depth: int = 1):
        for _ in range(depth):
            if not self.region_stack:
                self.offset = (0, 0)
                self.size = self.img.size
                self.w = self.size[0]
                self.h = self.size[1]
                return
            self.offset, self.size = self.region_stack.pop()
            self.w = self.size[0]
            self.h = self.size[1]

    def rect(self, pos: Position, size: Size, fill: Color = None,
             stroke: Color = None, stroke_width: int = 1):
        x0 = self.offset[0] + pos[0]
        y0 = self.offset[1] + pos[1]
        x1 = x0 + size[0]
        y1 = y0 + size[1]
        if fill is not None:
            self._draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=fill)
        if stroke is not None and stroke_width > 0:
            self._draw.rectangle([x0, y0, x1 - 1, y1 - 1], outline=stroke, width=stroke_width)

    def roundrect(self, pos: Position, size: Size, fill: Color = None, radius: int = 0,
                  stroke: Color = None, stroke_width: int = 1, corners=(True, True, True, True)):
        x0 = self.offset[0] + pos[0]
        y0 = self.offset[1] + pos[1]
        x1 = x0 + size[0]
        y1 = y0 + size[1]
        if fill is not None:
            self._draw.rounded_rectangle([x0, y0, x1 - 1, y1 - 1], radius=radius, fill=fill)
        if stroke is not None and stroke_width > 0:
            self._draw.rounded_rectangle(
                [x0, y0, x1 - 1, y1 - 1], radius=radius, outline=stroke, width=stroke_width
            )

    def text(self, text: str, pos: Position, font: ImageFont.FreeTypeFont, fill: Color = BLACK):
        x = self.offset[0] + pos[0]
        y = self.offset[1] + pos[1]
        # getbbox 的左上角可能非 0，需校正使其左对齐到 pos
        bbox = font.getbbox(text)
        self._draw.text((x - bbox[0], y - bbox[1]), text, font=font, fill=fill)

    def paste(self, image: Image.Image, pos: Position, size: Size = None,
              use_shadow: bool = False, shadow_width: int = 6, shadow_alpha: float = 0.6):
        x = self.offset[0] + pos[0]
        y = self.offset[1] + pos[1]
        if size and tuple(image.size) != tuple(size):
            # 与原版一致：精确缩放到目标尺寸（fit 模式下宽高比已由 ImageBox 算好）
            image = image.resize((int(size[0]), int(size[1])), Image.Resampling.LANCZOS)
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        self.img.alpha_composite(image, (x, y))

    def paste_with_alphablend(self, image: Image.Image, pos: Position, size: Size = None,
                              alpha_adjust: float = 1.0,
                              use_shadow: bool = False, shadow_width: int = 6,
                              shadow_alpha: float = 0.6):
        x = self.offset[0] + pos[0]
        y = self.offset[1] + pos[1]
        if size and tuple(image.size) != tuple(size):
            image = image.resize((int(size[0]), int(size[1])), Image.Resampling.LANCZOS)
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        if alpha_adjust != 1.0:
            r, g, b, a = image.split()
            a = a.point(lambda v: int(v * alpha_adjust))
            image = Image.merge("RGBA", (r, g, b, a))
        self.img.alpha_composite(image, (x, y))


# ==================== 背景 ====================

class WidgetBg:
    def draw(self, p: Painter):
        raise NotImplementedError()


class FillBg(WidgetBg):
    def __init__(self, fill: Color, stroke: Color = None, stroke_width: int = 1):
        self.fill = fill
        self.stroke = stroke
        self.stroke_width = stroke_width

    def draw(self, p: Painter):
        p.rect((0, 0), p.size, self.fill, self.stroke, self.stroke_width)


# ==================== 布局组件 ====================

class Widget:
    _thread_local = contextvars.ContextVar('widget_stack', default=None)

    def __init__(self):
        self.parent: Optional[Widget] = None
        self.content_halign = 'l'
        self.content_valign = 't'
        self.vmargin = DEFAULT_MARGIN
        self.hmargin = DEFAULT_MARGIN
        self.vpadding = DEFAULT_PADDING
        self.hpadding = DEFAULT_PADDING
        self.w: Optional[int] = None
        self.h: Optional[int] = None
        self.bg: Optional[WidgetBg] = None
        self.omit_parent_bg = False
        self.offset = (0, 0)
        self.offset_xanchor = 'l'
        self.offset_yanchor = 't'
        self.allow_draw_outside = False
        self.w_size_policy = 'fixed'
        self.h_size_policy = 'fixed'
        self._calc_w: Optional[int] = None
        self._calc_h: Optional[int] = None
        self.drawn = False
        # 自动加入当前父组件
        cur = Widget.get_current_widget()
        if cur is not None:
            cur.add_item(self)

    @staticmethod
    def get_current_widget_stack():
        local = Widget._thread_local.get()
        return local.wstack if local is not None else None

    @classmethod
    def get_current_widget(cls) -> Optional['Widget']:
        stk = cls.get_current_widget_stack()
        return stk[-1] if stk else None

    def __enter__(self):
        local = self._thread_local.get()
        if local is None:
            local = threading.local()
            local.wstack = []
        local.wstack.append(self)
        self._thread_local.set(local)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        local = self._thread_local.get()
        assert local is not None and local.wstack[-1] is self
        local.wstack.pop()
        if not local.wstack:
            self._thread_local.set(None)

    def add_item(self, item: 'Widget', index: int = None):
        item.set_parent(self)
        if index is None:
            self.items.append(item)
        else:
            self.items.insert(index, item)
        return self

    def set_parent(self, parent: 'Widget'):
        self.parent = parent
        return self

    def set_content_align(self, align: str):
        if align not in ALIGN_MAP:
            raise ValueError('Invalid align')
        self.content_halign, self.content_valign = ALIGN_MAP[align]
        return self

    def set_margin(self, margin: Union[int, Tuple[int, int]]):
        if isinstance(margin, int):
            self.vmargin = margin
            self.hmargin = margin
        else:
            self.hmargin = margin[0]
            self.vmargin = margin[1]
        return self

    def set_padding(self, padding: Union[int, Tuple[int, int]]):
        if isinstance(padding, int):
            self.vpadding = padding
            self.hpadding = padding
        else:
            self.hpadding = padding[0]
            self.vpadding = padding[1]
        return self

    def set_size(self, size: Size):
        size = size or (None, None)
        self.w, self.h = size
        return self

    def set_w(self, w: int):
        self.w = w
        return self

    def set_h(self, h: int):
        self.h = h
        return self

    def set_bg(self, bg: WidgetBg):
        self.bg = bg
        return self

    def set_size_policy(self, w_policy: str = None, h_policy: str = None):
        if w_policy:
            assert w_policy in ('fixed', 'fit')
            self.w_size_policy = w_policy
        if h_policy:
            assert h_policy in ('fixed', 'fit')
            self.h_size_policy = h_policy
        return self

    def _get_content_size(self) -> Size:
        return (0, 0)

    def _get_self_size(self) -> Size:
        if not (self._calc_w and self._calc_h):
            content_w, content_h = self._get_content_size()
            content_w_limit = self.w - self.hpadding * 2 if self.w is not None else content_w
            content_h_limit = self.h - self.vpadding * 2 if self.h is not None else content_h
            if (content_w > content_w_limit or content_h > content_h_limit) and not self.allow_draw_outside:
                content_w = min(content_w, content_w_limit)
                content_h = min(content_h, content_h_limit)
            self._calc_w = (content_w_limit if self.w is not None else content_w) + self.hmargin * 2 + self.hpadding * 2
            self._calc_h = (content_h_limit if self.h is not None else content_h) + self.vmargin * 2 + self.vpadding * 2
            if self.w_size_policy == 'fit' and self.w is None:
                self._calc_w = content_w + self.hmargin * 2 + self.hpadding * 2
            if self.h_size_policy == 'fit' and self.h is None:
                self._calc_h = content_h + self.vmargin * 2 + self.vpadding * 2
        return (int(self._calc_w), int(self._calc_h))

    def _get_content_pos(self) -> Position:
        w, h = self._get_self_size()
        w -= self.hpadding * 2 + self.hmargin * 2
        h -= self.vpadding * 2 + self.vmargin * 2
        cw, ch = self._get_content_size()
        cx = {'l': 0, 'r': w - cw, 'c': (w - cw) // 2}[self.content_halign]
        cy = {'t': 0, 'b': h - ch, 'c': (h - ch) // 2}[self.content_valign]
        return (cx, cy)

    def _draw_self(self, p: Painter):
        if self.bg:
            self.bg.draw(p)

    def _draw_content(self, p: Painter):
        pass

    def draw(self, p: Painter):
        assert not self.drawn, 'Only support draw once for each widget'
        self.drawn = True
        assert p.size == self._get_self_size()
        # offset 锚点
        offset_x = {'l': self.offset[0], 'r': self.offset[0] - p.w,
                    'c': self.offset[0] - p.w // 2}[self.offset_xanchor]
        offset_y = {'t': self.offset[1], 'b': self.offset[1] - p.h,
                    'c': self.offset[1] - p.h // 2}[self.offset_yanchor]
        p.move_region((offset_x, offset_y))
        p.shrink_region((self.hmargin, self.vmargin))
        self._draw_self(p)
        p.shrink_region((self.hpadding, self.vpadding))
        cx, cy = self._get_content_pos()
        p.move_region((cx, cy))
        self._draw_content(p)
        p.restore_region(4)


class Frame(Widget):
    def __init__(self, items: List[Widget] = None):
        super().__init__()
        self.items = items or []
        for item in self.items:
            item.set_parent(self)

    def _get_content_size(self) -> Size:
        size = (0, 0)
        for item in self.items:
            w, h = item._get_self_size()
            size = (max(size[0], w), max(size[1], h))
        return size

    def _draw_content(self, p: Painter):
        cw, ch = self._get_content_size()
        for item in self.items:
            w, h = item._get_self_size()
            x = {'l': 0, 'r': cw - w, 'c': (cw - w) // 2}[self.content_halign]
            y = {'t': 0, 'b': ch - h, 'c': (ch - h) // 2}[self.content_valign]
            p.move_region((x, y), (w, h))
            item.draw(p)
            p.restore_region()


class HSplit(Widget):
    def __init__(self, items: List[Widget] = None, ratios: List[float] = None,
                 sep=DEFAULT_SEP, item_size_mode='fixed', item_align='c'):
        super().__init__()
        self.items = items or []
        for item in self.items:
            item.set_parent(self)
        self.ratios = ratios
        self.sep = sep
        assert item_size_mode in ('expand', 'fixed')
        self.item_size_mode = item_size_mode
        self.item_halign, self.item_valign = ALIGN_MAP[item_align]

    def set_item_align(self, align: str):
        self.item_halign, self.item_valign = ALIGN_MAP[align]
        return self

    def set_sep(self, sep: int):
        self.sep = sep
        return self

    def _get_item_sizes(self):
        ratios = self.ratios if self.ratios else [item._get_self_size()[0] for item in self.items]
        if self.item_size_mode == 'expand':
            ratio_sum = sum(ratios)
            unit_w = (self.w - self.sep * (len(ratios) - 1) - self.hpadding * 2) / ratio_sum
        else:
            unit_w = 0
            for r, item in zip(ratios, self.items):
                iw, _ = item._get_self_size()
                if r > 0:
                    unit_w = max(unit_w, iw / r)
        h = max([item._get_self_size()[1] for item in self.items]) if self.items else 0
        return [(int(unit_w * r), h) for r in ratios]

    def _get_content_size(self) -> Size:
        if not self.items:
            return (0, 0)
        sizes = self._get_item_sizes()
        return (sum(s[0] for s in sizes) + self.sep * (len(sizes) - 1), max(s[1] for s in sizes))

    def _draw_content(self, p: Painter):
        if not self.items:
            return
        sizes = self._get_item_sizes()
        cur_x = 0
        for i, (item, (w, h)) in enumerate(zip(self.items, sizes)):
            iw, ih = item._get_self_size()
            p.move_region((cur_x, 0), (w, h))
            x = {'l': 0, 'r': w - iw, 'c': (w - iw) // 2}[self.item_halign]
            y = {'t': 0, 'b': h - ih, 'c': (h - ih) // 2}[self.item_valign]
            p.move_region((x, y), (iw, ih))
            item.draw(p)
            p.restore_region(2)
            cur_x += w + self.sep


class VSplit(Widget):
    def __init__(self, items: List[Widget] = None, ratios: List[float] = None,
                 sep=DEFAULT_SEP, item_size_mode='fixed', item_align='c'):
        super().__init__()
        self.items = items or []
        for item in self.items:
            item.set_parent(self)
        self.ratios = ratios
        self.sep = sep
        assert item_size_mode in ('expand', 'fixed')
        self.item_size_mode = item_size_mode
        self.item_halign, self.item_valign = ALIGN_MAP[item_align]

    def set_item_align(self, align: str):
        self.item_halign, self.item_valign = ALIGN_MAP[align]
        return self

    def set_sep(self, sep: int):
        self.sep = sep
        return self

    def _get_item_sizes(self):
        ratios = self.ratios if self.ratios else [item._get_self_size()[1] for item in self.items]
        if self.item_size_mode == 'expand':
            ratio_sum = sum(ratios)
            unit_h = (self.h - self.sep * (len(ratios) - 1) - self.vpadding * 2) / ratio_sum
        else:
            unit_h = 0
            for r, item in zip(ratios, self.items):
                _, ih = item._get_self_size()
                if r > 0:
                    unit_h = max(unit_h, ih / r)
        w = max([item._get_self_size()[0] for item in self.items]) if self.items else 0
        return [(w, int(unit_h * r)) for r in ratios]

    def _get_content_size(self) -> Size:
        if not self.items:
            return (0, 0)
        sizes = self._get_item_sizes()
        return (max(s[0] for s in sizes), sum(s[1] for s in sizes) + self.sep * (len(sizes) - 1))

    def _draw_content(self, p: Painter):
        if not self.items:
            return
        sizes = self._get_item_sizes()
        cur_y = 0
        for i, (item, (w, h)) in enumerate(zip(self.items, sizes)):
            iw, ih = item._get_self_size()
            p.move_region((0, cur_y), (w, h))
            x = {'l': 0, 'r': w - iw, 'c': (w - iw) // 2}[self.item_halign]
            y = {'t': 0, 'b': h - ih, 'c': (h - ih) // 2}[self.item_valign]
            p.move_region((x, y), (iw, ih))
            item.draw(p)
            p.restore_region(2)
            cur_y += h + self.sep


class Grid(Widget):
    def __init__(self, items: List[Widget] = None, row_count=None, col_count=None,
                 item_size_mode='fixed', item_align='c', hsep=DEFAULT_SEP, vsep=DEFAULT_SEP,
                 vertical=False):
        super().__init__()
        self.items = items or []
        for item in self.items:
            item.set_parent(self)
        self.row_count = row_count
        self.col_count = col_count
        assert not (self.row_count and self.col_count)
        self.item_size_mode = item_size_mode
        self.hsep = hsep
        self.vsep = vsep
        self.item_halign, self.item_valign = ALIGN_MAP[item_align]
        self.vertical = vertical
        self._grid_rc = None
        self._col_ws = None
        self._row_hs = None

    def set_item_align(self, align: str):
        self.item_halign, self.item_valign = ALIGN_MAP[align]
        return self

    def set_sep(self, hsep=None, vsep=None):
        if hsep is not None:
            self.hsep = hsep
        if vsep is not None:
            self.vsep = vsep
        return self

    def _calc_grid_rc_and_sizes(self):
        if self._grid_rc is None:
            r, c = self.row_count, self.col_count
            if not r:
                r = (len(self.items) + c - 1) // c
            if not c:
                c = (len(self.items) + r - 1) // r
            self._grid_rc = (r, c)
            if self.item_size_mode == 'expand':
                gw = (self.w - self.hsep * (c - 1) - self.hpadding * 2) / c
                gh = (self.h - self.vsep * (r - 1) - self.vpadding * 2) / r
                self._col_ws = [int(gw)] * c
                self._row_hs = [int(gh)] * r
            else:  # fixed
                gw, gh = 0, 0
                for item in self.items:
                    iw, ih = item._get_self_size()
                    gw = max(gw, iw)
                    gh = max(gh, ih)
                self._col_ws = [int(gw)] * c
                self._row_hs = [int(gh)] * r
        return self._grid_rc, self._col_ws, self._row_hs

    def _get_content_size(self) -> Size:
        (r, c), ws, hs = self._calc_grid_rc_and_sizes()
        return (int(sum(ws) + self.hsep * (c - 1)), int(sum(hs) + self.vsep * (r - 1)))

    def _draw_content(self, p: Painter):
        (r, c), ws, hs = self._calc_grid_rc_and_sizes()
        cur_x, cur_y = 0, 0
        for idx, item in enumerate(self.items):
            if not self.vertical:
                i, j = idx // c, idx % c
            else:
                i, j = idx % r, idx // r
            gw, gh = ws[j], hs[i]
            p.move_region((cur_x, cur_y), (gw, gh))
            iw, ih = item._get_self_size()
            dx = {'l': 0, 'r': gw - iw, 'c': (gw - iw) // 2}[self.item_halign]
            dy = {'t': 0, 'b': gh - ih, 'c': (gh - ih) // 2}[self.item_valign]
            p.move_region((dx, dy), (iw, ih))
            item.draw(p)
            p.restore_region(2)
            if not self.vertical:
                cur_x += gw + self.hsep
                if idx % c == c - 1:
                    cur_x = 0
                    cur_y += gh + self.vsep
            else:
                cur_y += gh + self.vsep
                if idx % r == r - 1:
                    cur_y = 0
                    cur_x += gw + self.hsep


# ==================== 文本与图片组件 ====================

@dataclass
class TextStyle:
    font: str = DEFAULT_FONT
    size: int = 16
    color: Color = BLACK
    use_shadow: bool = False
    shadow_offset: Union[Tuple[int, int], int] = 1
    shadow_color: Color = SHADOW


class TextBox(Widget):
    def __init__(self, text: str = '', style: TextStyle = None, line_count=None,
                 line_sep=2, wrap=True, overflow='shrink', use_real_line_count=False):
        super().__init__()
        self.text = str(text)
        self.style = style or TextStyle()
        self.line_count = line_count
        self.line_sep = line_sep
        self.wrap = wrap
        assert overflow in ('shrink', 'clip')
        self.overflow = overflow
        self.use_real_line_count = use_real_line_count
        if line_count is None:
            self.line_count = 99999 if use_real_line_count else 1
        self.set_padding(2)
        self.set_margin(0)

    def set_text(self, text: str):
        self.text = str(text)
        return self

    def _get_pil_font(self):
        return _get_pil_font(self.style.font, self.style.size)

    def _get_clip_idx(self, font, text: str, width: int, suffix=''):
        suffix_width = get_text_width(font, suffix) if suffix else 0
        target = width - suffix_width
        if target < 0:
            return 0
        full = get_text_width(font, text)
        if full <= target:
            return None
        # 步进裁剪
        idx = len(text)
        while idx > 0 and get_text_width(font, text[:idx]) > target:
            idx -= 1
        return idx if idx < len(text) else None

    def _get_lines(self):
        font = self._get_pil_font()
        lines = self.text.split('\n')
        result = []
        for line in lines:
            if self.w:
                w = self.w - self.hpadding * 2
                suffix = '...' if self.overflow == 'shrink' else ''
                if self.wrap:
                    while True:
                        clip_idx = self._get_clip_idx(font, line, w, '')
                        if clip_idx is None:
                            result.append(line)
                            break
                        line_suffix = suffix if len(result) == self.line_count - 1 else ''
                        if line_suffix:
                            clip_idx = self._get_clip_idx(font, line, w, line_suffix)
                            if clip_idx is None:
                                result.append(line)
                                break
                        result.append(line[:clip_idx] + line_suffix)
                        line = line[clip_idx:]
                        if len(result) == self.line_count:
                            break
                else:
                    clip_idx = self._get_clip_idx(font, line, w, suffix)
                    if clip_idx is not None:
                        line = line[:clip_idx] + suffix
                    result.append(line)
            else:
                result.append(line)
        return result[:self.line_count]

    def _get_content_size(self) -> Size:
        lines = self._get_lines()
        font = self._get_pil_font()
        w = max((get_text_width(font, line) for line in lines), default=0)
        line_count = len(lines) if self.use_real_line_count else self.line_count
        h = line_count * (self.style.size + self.line_sep) - self.line_sep
        if self.w:
            w = self.w - self.hpadding * 2
        if self.h:
            h = self.h - self.vpadding * 2
        return (w, h)

    def _draw_content(self, p: Painter):
        font = self._get_pil_font()
        lines = self._get_lines()
        text_h = (self.style.size + self.line_sep) * len(lines) - self.line_sep
        start_y = {'t': 0, 'b': p.h - text_h, 'c': (p.h - text_h) // 2}[self.content_valign]
        for i, line in enumerate(lines):
            lw = get_text_width(font, line)
            x = {'l': 0, 'r': p.w - lw, 'c': (p.w - lw) // 2}[self.content_halign]
            y = start_y + i * (self.style.size + self.line_sep)
            p.move_region((x, y), (lw, self.style.size))
            if self.style.use_shadow:
                so = self.style.shadow_offset
                so = (so, so) if isinstance(so, int) else so
                p.text(line, so, font=font, fill=self.style.shadow_color)
            p.text(line, (0, 0), font=font, fill=self.style.color)
            p.restore_region()


class ImageBox(Widget):
    def __init__(self, image: Union[str, Image.Image], image_size_mode=None, size=None,
                 use_alphablend=False, alpha_adjust=1.0):
        super().__init__()
        if isinstance(image, str):
            self.image = Image.open(image)
        else:
            self.image = image
        if size:
            self.set_size(size)
        if image_size_mode is None:
            self.image_size_mode = 'fit' if size and (size[0] or size[1]) else 'original'
        else:
            self.image_size_mode = image_size_mode
        self.set_margin(0)
        self.set_padding(0)
        self.use_alphablend = use_alphablend
        self.alpha_adjust = alpha_adjust

    def set_image_size_mode(self, mode: str):
        assert mode in ('fit', 'fill', 'original')
        self.image_size_mode = mode
        return self

    def _get_content_size(self) -> Size:
        w, h = self.image.size
        if self.image_size_mode == 'original':
            return (w, h)
        tw = (self.w - self.hpadding * 2) if self.w else 1000000
        th = (self.h - self.vpadding * 2) if self.h else 1000000
        if self.image_size_mode == 'fit':
            scale = min(tw / w, th / h)
            return (int(w * scale), int(h * scale))
        # fill
        if self.w and self.h:
            return (int(self.w - self.hpadding * 2), int(self.h - self.vpadding * 2))
        scale = max(tw / w, th / h)
        return (int(w * scale), int(h * scale))

    def _draw_content(self, p: Painter):
        w, h = self._get_content_size()
        if self.use_alphablend:
            p.paste_with_alphablend(self.image, (0, 0), (w, h), self.alpha_adjust)
        else:
            p.paste(self.image, (0, 0), (w, h))


class Spacer(Widget):
    def __init__(self, w: int = 1, h: int = 1):
        super().__init__()
        self.set_size((w, h))

    def _get_content_size(self) -> Size:
        return (self.w - 2 * self.hpadding, self.h - 2 * self.vpadding)

    def _draw_content(self, p: Painter):
        pass


class Canvas(Frame):
    def __init__(self, w=None, h=None, bg: WidgetBg = None):
        super().__init__()
        self.set_size((w, h))
        self.set_bg(bg)
        self.set_margin(0)

    async def get_img(self, scale: float = None) -> Image.Image:
        size = self._get_self_size()
        p = Painter(size=size)
        self.draw(p)
        img = await p.get()
        if scale and scale != 1.0:
            img = img.resize(
                (int(size[0] * scale), int(size[1] * scale)), Image.Resampling.BILINEAR
            )
        return img
