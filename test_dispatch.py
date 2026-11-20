"""画廊插件指令路由层测试（不依赖 AstrBot 运行时）。

验证核心需求：
- 监听所有消息，非画廊指令直接放行（不拦截、不回复）
- 兼容 / 前缀，也允许不带 / 的裸指令
- enable_slash_prefix=False 时仅接受裸指令
- gall 子指令的管理员权限门禁
- 指令处理后停止事件传播
"""

import sys
import os
import builtins
import types
import asyncio
import tempfile


# 可被测试覆盖的下载映射：url -> bytes；供 mock 的 download_image_by_url 使用
_url_to_bytes: dict = {}


def _install_astrbot_mocks():
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    astrbot_api = types.ModuleType("astrbot.api")

    def _mock_log(*args, **kwargs):
        # 真实 astrbot logger 支持 exc_info 等关键字；main.py 的兜底异常分支
        # 会调用 logger.error(..., exc_info=True)，mock 必须一并接受，
        # 否则测试会在日志调用处先抛 TypeError，掩盖被测行为
        print(*args)

    astrbot_api.logger = types.SimpleNamespace(
        info=_mock_log,
        warning=_mock_log,
        error=_mock_log,
        debug=lambda *a, **k: None,
    )
    astrbot_api.AstrBotConfig = dict

    api_event = types.ModuleType("astrbot.api.event")

    class _MockFilter:
        class EventMessageType:
            ALL = "all"
            PRIVATE_MESSAGE = "private"
            GROUP_MESSAGE = "group"

        @staticmethod
        def event_message_type(t):
            def deco(fn):
                return fn
            return deco

        @staticmethod
        def command(*a, **k):
            def deco(fn):
                return fn
            return deco

    api_event.filter = _MockFilter
    api_event.AstrMessageEvent = object
    api_event.MessageChain = object

    api_star = types.ModuleType("astrbot.api.star")

    class _MockStar:
        def __init__(self, context):
            self.context = context

        async def html_render(self, tmpl, data, return_url=True, options=None):
            return os.path.join(tempfile.gettempdir(), "mock_render.jpg")

        async def text_to_image(self, text):
            return os.path.join(tempfile.gettempdir(), "mock_t2i.jpg")

    api_star.Context = object
    api_star.Star = _MockStar
    api_star.register = lambda *a, **k: (lambda c: c)

    api_comp = types.ModuleType("astrbot.api.message_components")

    class _Comp:
        def __init__(self, *args, **kw):
            self.__dict__.update(kw)

    for name in ["Plain", "At", "Image", "Record", "Video", "File", "Face",
                 "Reply", "Node", "Nodes", "Poke", "Forward"]:
        setattr(api_comp, name, type(name, (_Comp,), {}))

    # Plain 兼容位置参数（真实签名是 Plain(text)）
    def _plain_init(self, text: str = "", **kw):
        self.text = text
        self.__dict__.update(kw)
    setattr(api_comp, "Plain", type("Plain", (_Comp,), {"__init__": _plain_init}))
    api_comp.Image.fromURL = classmethod(lambda cls, url: cls(url=url))
    api_comp.Image.fromFileSystem = classmethod(lambda cls, path: cls(file=path))

    core = types.ModuleType("astrbot.core")
    star_pkg = types.ModuleType("astrbot.core.star")
    star_tools = types.ModuleType("astrbot.core.star.star_tools")
    star_tools.StarTools = types.SimpleNamespace(
        get_data_dir=lambda: os.path.join(tempfile.mkdtemp(prefix="gallery_rt_"))
    )
    io_mod = types.ModuleType("astrbot.core.utils.io")

    async def _download_image_by_url(url, *a, **k):
        # 复刻真实 save_temp_img：一律存 .jpg，即使原图是 gif/png
        import os, tempfile, uuid
        data = _url_to_bytes.get(url, b"")
        d = tempfile.mkdtemp(prefix="dl_")
        p = os.path.join(d, f"dl_{uuid.uuid4().hex[:8]}.jpg")
        with open(p, "wb") as f:
            f.write(data)
        return p

    io_mod.download_image_by_url = _download_image_by_url

    # aiocqhttp 平台事件类（合并转发拉取时 isinstance 校验用）
    class AiocqhttpMessageEvent:
        pass

    aiocqhttp_msg_event = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )
    aiocqhttp_msg_event.AiocqhttpMessageEvent = AiocqhttpMessageEvent
    platform_pkg = types.ModuleType("astrbot.core.platform")
    sources_pkg = types.ModuleType("astrbot.core.platform.sources")
    aiocqhttp_pkg = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")

    astrbot_api.event = api_event
    astrbot_api.star = api_star
    astrbot_api.message_components = api_comp
    astrbot.core = core
    core.star = star_pkg
    star_pkg.star_tools = star_tools
    core.utils = types.ModuleType("astrbot.core.utils")
    core.utils.io = io_mod

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = astrbot_api
    sys.modules["astrbot.api.event"] = api_event
    sys.modules["astrbot.api.star"] = api_star
    sys.modules["astrbot.api.message_components"] = api_comp
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.star"] = star_pkg
    sys.modules["astrbot.core.star.star_tools"] = star_tools
    sys.modules["astrbot.core.utils"] = core.utils
    sys.modules["astrbot.core.utils.io"] = io_mod
    sys.modules["astrbot.core.platform"] = platform_pkg
    sys.modules["astrbot.core.platform.sources"] = sources_pkg
    sys.modules["astrbot.core.platform.sources.aiocqhttp"] = aiocqhttp_pkg
    sys.modules["astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"] = (
        aiocqhttp_msg_event
    )


_install_astrbot_mocks()

# 以包上下文导入 main
import importlib.machinery
import importlib.util

_pkg_dir = os.path.dirname(os.path.abspath(__file__))
spec = importlib.machinery.ModuleSpec("astrbot_plugin_gallery_rt", None, is_package=True)
pkg = importlib.util.module_from_spec(spec)
pkg.__path__ = [_pkg_dir]
sys.modules["astrbot_plugin_gallery_rt"] = pkg

import astrbot.api.message_components as Comp
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot_plugin_gallery_rt.main import GalleryPlugin


class MockBotAPI:
    """模拟 aiocqhttp 客户端 API，记录调用并返回预置结果。"""

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[tuple] = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        key = (action, str(kwargs.get("message_id") or kwargs.get("id")))
        if key in self.responses:
            return self.responses[key]
        return self.responses.get(action)


class MockEvent(AiocqhttpMessageEvent):
    """最小事件 mock。"""

    def __init__(self, message_str, admin=False, bot_api: MockBotAPI | None = None,
                 wake=True):
        self.message_str = message_str
        self._admin = admin
        # AstrBot 唤醒标记：@机器人/唤醒前缀/私聊为 True
        self.is_at_or_wake_command = wake
        self.message_obj = types.SimpleNamespace(message=[])
        self.stopped = False
        self.sent = []
        if bot_api is not None:
            self.bot = types.SimpleNamespace(api=bot_api)

    def is_admin(self):
        return self._admin

    def get_platform_name(self):
        return "aiocqhttp"

    @property
    def unified_msg_origin(self):
        return "aiocqhttp:GroupMessage:mock_group"

    def stop_event(self):
        self.stopped = True

    def get_sender_id(self):
        return "user1"

    def get_group_id(self):
        return ""

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    def image_result(self, path):
        return ("image", path)

    async def send(self, r):
        self.sent.append(r)


class TestResults:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def check(self, name, cond, detail=""):
        if cond:
            self.passed += 1
            print(f"  [PASS] {name}")
        else:
            self.failed += 1
            self.failures.append(f"{name}: {detail}")
            print(f"  [FAIL] {name} - {detail}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n=== 结果: {self.passed}/{total} 通过 ===")
        for f in self.failures:
            print(f"  - {f}")
        return self.failed == 0


class HandlerResults(list):
    """一次 handler 执行的产出：yield 结果列表 + event.send 直发快照。

    看图等路径直接调用 event.send() 而不 yield，只看 yield 结果会把
    「其实已经回复了」误判成「0 回复」，因此两种产出必须一起记录。
    """

    def __init__(self, results=(), sent=()):
        super().__init__(results)
        self.sent = list(sent)


async def run_handler(plugin, event):
    """收集 on_message 异步生成器的所有结果，并记录 event.sent 直发内容。"""
    results = []
    async for r in plugin.on_message(event):
        results.append(r)
    # 生成器耗尽后再快照，确保 send() 记录完整
    return HandlerResults(results, event.sent)


async def run_native(plugin, event, name):
    """调用原生指令 handler（模拟 AstrBot CommandFilter 命中后的调用）。"""
    results = []
    async for r in getattr(plugin, name)(event):
        results.append(r)
    return HandlerResults(results, event.sent)


def assert_no_reply(r, name, results, detail=""):
    """断言「完全没有回复」：既没有 yield 结果，也没有 event.send 直发。"""
    sent = getattr(results, "sent", [])
    r.check(
        name,
        len(results) == 0 and not sent,
        f"res={list(results)} sent={sent} {detail}".strip(),
    )


class RangeSpy:
    """临时替换 builtins.range 记录调用参数。

    用于验证「先校验区间大小再 list(range(...))」：展开 1e8 个 pid 会产生
    巨大列表，通过断言没有出现跨度 > 1000 的 range 调用来抓住回退。
    """

    def __init__(self):
        self.calls: list[tuple] = []
        self._orig = builtins.range

    def __enter__(self):
        self._orig = builtins.range
        calls = self.calls

        def spy(*args):
            calls.append(args)
            return self._orig(*args)

        builtins.range = spy
        return self

    def __exit__(self, *exc):
        builtins.range = self._orig
        return False


def make_plugin(config=None):
    # 默认开启兼容监听模式，保持既有用例经由 on_message 触发；
    # 触发模式相关用例显式传入 listen_all_messages=False
    cfg = {"listen_all_messages": True}
    cfg.update(config or {})
    return GalleryPlugin(types.SimpleNamespace(), dict(cfg))


async def add_pic(plugin, gall, color=(200, 30, 30)):
    from PIL import Image as PImage
    p = os.path.join(plugin.tmp_dir, f"rt_{color}.png")
    PImage.new("RGBA", (60, 60), color + (255,)).save(p, format="PNG")
    return await plugin.gallery_manager.async_add_pic(gall, p, check_duplicated=False)


def chain_text(res):
    """把 chain/plain 结果里的文本拼出来。"""
    out = []
    for kind, payload in res:
        if kind == "chain":
            out.extend(str(getattr(c, "text", "")) for c in payload)
        elif kind == "plain":
            out.append(str(payload))
    return "\n".join(out)


def make_review_provider(outputs):
    """按调用顺序返回预设 completion_text 的 mock LLM 提供者。"""
    calls = []

    async def text_chat(prompt=None, session_id=None, image_urls=None,
                        contexts=None, **kw):
        calls.append({"prompt": prompt, "image": (image_urls or [None])[0]})
        i = len(calls) - 1
        if isinstance(outputs, Exception):
            raise outputs
        return types.SimpleNamespace(
            completion_text=outputs[min(i, len(outputs) - 1)]
        )

    prov = types.SimpleNamespace(text_chat=text_chat)
    prov.calls = calls
    return prov


def make_review_img(color: int):
    """用整数做种子生成 8px 随机灰度方块图。

    感知哈希缩图后仍保留大尺度结构，保证图片间查重判定互不重复
    （纯色/细噪声图缩到 16x16 后几乎一致，会被判重）。
    """
    import random as _random
    from PIL import Image as _PImgR
    rng = _random.Random(color)
    p = os.path.join(tempfile.gettempdir(), f"gallery_review_{color}.png")
    img = _PImgR.new("RGB", (64, 64))
    px = img.load()
    for bx in range(8):
        for by in range(8):
            v = rng.randrange(256)
            for x in range(bx * 8, bx * 8 + 8):
                for y in range(by * 8, by * 8 + 8):
                    px[x, y] = (v, v, v)
    img.save(p, format="PNG")
    return p


async def test_regressions(r):
    """M1/M6/M7/M8/M9/M2/S1 的指令层回归用例（可单独调用）。"""
    # ---- 13. 回归：指令解析 / force / 删除区间 / 日志 / 无法识别图片 ----
    print("\n[测试] 回归：指令解析与上传边界")

    async def upload_file(plugin_inst, msg, path):
        ev = MockEvent(msg)
        ev.message_obj.message = [Comp.Image(file=path)]
        return await run_handler(plugin_inst, ev)

    # M1：写操作指令必须指令名后为空白或行尾；查询类「看miku」保持无空格前缀匹配
    plugin_m1 = make_plugin()
    plugin_m1.gallery_manager.ensure_loaded()
    plugin_m1.gallery_manager.open_gall("miku")
    await plugin_m1.gallery_manager._save()
    await add_pic(plugin_m1, "miku")

    ev_m1 = MockEvent("创建画廊了")
    res_m1 = await run_handler(plugin_m1, ev_m1)
    assert_no_reply(r, "创建画廊了 不回复", res_m1, f"stopped={ev_m1.stopped}")
    r.check(
        "创建画廊了 未创建画廊且不拦截",
        "了" not in plugin_m1.gallery_manager.galleries
        and "miku" in plugin_m1.gallery_manager.galleries
        and ev_m1.stopped is False,
        f"galls={list(plugin_m1.gallery_manager.galleries)} stopped={ev_m1.stopped}",
    )

    ev_m1b = MockEvent("看miku")
    res_m1b = await run_handler(plugin_m1, ev_m1b)
    chains_m1b = [x for x in ev_m1b.sent if isinstance(x, tuple) and x[0] == "chain"]
    r.check(
        "看miku 无空格仍能看图",
        len(res_m1b) == 0 and len(chains_m1b) == 1 and ev_m1b.stopped is True,
        f"res={list(res_m1b)} sent={ev_m1b.sent}",
    )

    ev_m1c = MockEvent("取消上传们")
    res_m1c = await run_handler(plugin_m1, ev_m1c)
    assert_no_reply(r, "取消上传们 不误触发撤销", res_m1c, f"stopped={ev_m1c.stopped}")
    r.check("取消上传们 不拦截", ev_m1c.stopped is False)

    # M1b：「上传奶龙」无空格可用（画廊已存在）；「上传失败了」仍按普通聊天放行
    plugin_m1x = make_plugin()
    plugin_m1x.gallery_manager.ensure_loaded()
    plugin_m1x.gallery_manager.open_gall("奶龙")
    await plugin_m1x.gallery_manager._save()

    res_m1x = await upload_file(plugin_m1x, "上传奶龙", make_review_img(43001))
    text_m1x = chain_text(res_m1x)
    r.check(
        "上传奶龙 无空格也能上传",
        "成功上传1/1" in text_m1x
        and '到"奶龙"' in text_m1x
        and len(plugin_m1x.gallery_manager.galleries["奶龙"].pics) == 1,
        f"res={text_m1x}",
    )

    res_m1y = await upload_file(plugin_m1x, "添加奶龙", make_review_img(43002))
    text_m1y = chain_text(res_m1y)
    r.check(
        "添加奶龙 无空格也能上传",
        "成功上传1/1" in text_m1y
        and len(plugin_m1x.gallery_manager.galleries["奶龙"].pics) == 2,
        f"res={text_m1y}",
    )

    ev_m1z = MockEvent("上传失败了")
    res_m1z = await run_handler(plugin_m1x, ev_m1z)
    assert_no_reply(r, "上传失败了 不回复", res_m1z, f"stopped={ev_m1z.stopped}")
    r.check("上传失败了 不拦截", ev_m1z.stopped is False, f"stopped={ev_m1z.stopped}")

    # M6：force 按独立 token 识别，画廊名里的 force 子串不能误伤
    plugin_m6 = make_plugin()
    plugin_m6.gallery_manager.ensure_loaded()
    for _name in ("myforce", "mikuforce", "miku"):
        plugin_m6.gallery_manager.open_gall(_name)
    await plugin_m6.gallery_manager._save()

    res_m6a = await upload_file(plugin_m6, "上传 myforce", make_review_img(61001))
    text_m6a = chain_text(res_m6a)
    r.check(
        "画廊名 myforce 能正常上传",
        "成功上传1/1" in text_m6a
        and '到"myforce"' in text_m6a
        and len(plugin_m6.gallery_manager.galleries["myforce"].pics) == 1,
        f"res={text_m6a}",
    )

    m6_img = make_review_img(61002)
    await upload_file(plugin_m6, "上传 mikuforce", m6_img)
    r.check(
        "mikuforce 首张上传成功",
        len(plugin_m6.gallery_manager.galleries["mikuforce"].pics) == 1,
        f"pics={len(plugin_m6.gallery_manager.galleries['mikuforce'].pics)}",
    )
    res_m6b = await upload_file(plugin_m6, "上传 mikuforce", m6_img)
    text_m6b = chain_text(res_m6b)
    r.check(
        "mikuforce 不会被当成 force 参数",
        "重复" in text_m6b
        and len(plugin_m6.gallery_manager.galleries["mikuforce"].pics) == 1
        and len(plugin_m6.gallery_manager.galleries["miku"].pics) == 0,
        f"res={text_m6b} "
        f"mikuforce={len(plugin_m6.gallery_manager.galleries['mikuforce'].pics)} "
        f"miku={len(plugin_m6.gallery_manager.galleries['miku'].pics)}",
    )
    res_m6c = await upload_file(plugin_m6, "上传 mikuforce force", m6_img)
    r.check(
        "mikuforce force 才强制上传",
        "成功上传1/1" in chain_text(res_m6c)
        and len(plugin_m6.gallery_manager.galleries["mikuforce"].pics) == 2,
        f"res={chain_text(res_m6c)}",
    )

    # M7：/gall del 超大区间先校验再展开；「1 -2」按逐个 pid 解析
    plugin_m7 = make_plugin()
    plugin_m7.gallery_manager.ensure_loaded()
    plugin_m7.gallery_manager.open_gall("删除")
    await plugin_m7.gallery_manager._save()
    for _color in ((10, 10, 10), (20, 20, 20), (30, 30, 30), (40, 40, 40)):
        await add_pic(plugin_m7, "删除", color=_color)
    g_m7 = plugin_m7.gallery_manager.galleries["删除"]

    ev_del_big = MockEvent("gall del 1-100000000", admin=True)
    with RangeSpy() as range_spy:
        res_del_big = await run_handler(plugin_m7, ev_del_big)
    text_del_big = chain_text(res_del_big)
    huge_range = [a for a in range_spy.calls if len(a) >= 2 and a[1] - a[0] > 1000]
    r.check(
        "超大删除区间被拒",
        "一次最多删除20张连续图片" in text_del_big,
        f"res={text_del_big}",
    )
    r.check(
        "超大删除区间未展开大列表",
        huge_range == [],
        f"huge_range_calls={huge_range}",
    )
    r.check(
        "超大删除区间未删除图片",
        sorted(p.pid for p in g_m7.pics) == [1, 2, 3, 4],
        f"pids={[p.pid for p in g_m7.pics]}",
    )

    ev_del_neg = MockEvent("gall del 1 -2", admin=True)
    res_del_neg = await run_handler(plugin_m7, ev_del_neg)
    remaining = sorted(p.pid for p in g_m7.pics)
    # -2 是全局倒数第 2 张（pid=3），所以留下 [2,4]；
    # 若被误解析成区间 1..2 会留下 [3,4]，此断言能抓住旧行为
    r.check(
        "gall del 1 -2 按逐个 pid 解析而非区间",
        remaining == [2, 4],
        f"remaining={remaining} res={chain_text(res_del_neg)}",
    )

    # M8：/gall log 1 不能命中 pid=10 / pid=100
    plugin_m8 = make_plugin()
    plugin_m8.gallery_manager.ensure_loaded()
    log_path = os.path.join(plugin_m8.data_dir, "add.log")
    with open(log_path, "w", encoding="utf-8") as _f:
        _f.write('2026-01-01 00:00:00 | @u upload pid=10 to "g"\n')
        _f.write('2026-01-01 00:00:01 | @u upload pid=1 to "g"\n')
        _f.write('2026-01-01 00:00:02 | @u upload pid=100 to "g"\n')
    ev_log1 = MockEvent("gall log 1", admin=True)
    res_log1 = await run_handler(plugin_m8, ev_log1)
    text_log1 = chain_text(res_log1)
    r.check(
        "gall log 1 不命中 pid=10/100",
        "pid=1 to" in text_log1
        and "pid=10" not in text_log1
        and "pid=100" not in text_log1,
        f"res={text_log1}",
    )
    ev_log10 = MockEvent("gall log 10", admin=True)
    res_log10 = await run_handler(plugin_m8, ev_log10)
    r.check(
        "gall log 10 命中 pid=10",
        "pid=10 to" in chain_text(res_log10),
        f"res={chain_text(res_log10)}",
    )

    # M9：Image 组件没有 url/file 时回执必须点名「无法识别」
    plugin_m9 = make_plugin()
    plugin_m9.gallery_manager.ensure_loaded()
    plugin_m9.gallery_manager.open_gall("无法识别")
    await plugin_m9.gallery_manager._save()

    ev_u1 = MockEvent("上传 无法识别")
    ev_u1.message_obj.message = [Comp.Image()]  # url/file 都没有
    res_u1 = await run_handler(plugin_m9, ev_u1)
    text_u1 = chain_text(res_u1)
    r.check(
        "无url/file的Image回执提示无法识别",
        "无法识别" in text_u1,
        f"res={text_u1}",
    )
    r.check(
        "无法识别的图片未入库",
        len(plugin_m9.gallery_manager.galleries["无法识别"].pics) == 0,
        f"pics={plugin_m9.gallery_manager.galleries['无法识别'].pics}",
    )

    ev_u2 = MockEvent("上传 无法识别")
    ev_u2.message_obj.message = [
        Comp.Image(file=make_review_img(91001)),
        Comp.Image(),
    ]
    res_u2 = await run_handler(plugin_m9, ev_u2)
    text_u2 = chain_text(res_u2)
    r.check(
        "部分无法识别时回执分母含跳过数",
        "成功上传1/2" in text_u2 and "1 张图片无法识别已跳过" in text_u2,
        f"res={text_u2}",
    )

    # M2：兜底异常只回固定文案，绝对路径/异常原文只进日志
    print("\n[测试] 回归：兜底异常不泄露细节")
    import astrbot_plugin_gallery_rt.main as main_mod

    plugin_err = make_plugin()
    plugin_err.gallery_manager.ensure_loaded()

    async def _boom_dispatch(event, cmd, args):
        raise RuntimeError(r"读取 C:\server\secret\gallery.json 失败")
        yield  # pragma: no cover - 让函数成为异步生成器

    plugin_err._dispatch = _boom_dispatch
    log_calls = []
    orig_logger = main_mod.logger
    main_mod.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: log_calls.append((a, k)),
        debug=lambda *a, **k: None,
    )
    try:
        ev_err = MockEvent("看 miku")
        res_err = await run_handler(plugin_err, ev_err)
    finally:
        main_mod.logger = orig_logger
    text_err = chain_text(res_err)
    r.check(
        "兜底异常群回执为固定文案",
        text_err == "处理出错，已记录日志",
        f"res={text_err}",
    )
    r.check(
        "兜底异常不回显绝对路径/异常原文",
        "C:\\server" not in text_err and "RuntimeError" not in text_err,
        f"res={text_err}",
    )
    logged = " ".join(str(x) for x in log_calls[0][0]) if log_calls else ""
    r.check(
        "兜底异常写入 error 日志并带堆栈",
        bool(log_calls)
        and log_calls[0][1].get("exc_info") is True
        and "C:\\server" in logged,
        f"log_calls={log_calls}",
    )

    # S1：非法画廊名「..」在指令层也要被拒
    print("\n[测试] 回归：非法画廊名")
    plugin_s1 = make_plugin()
    plugin_s1.gallery_manager.ensure_loaded()
    data_dir_s1 = plugin_s1.data_dir
    ev_s1 = MockEvent("创建画廊 ..")
    res_s1 = await run_handler(plugin_s1, ev_s1)
    text_s1 = chain_text(res_s1)
    r.check(
        '创建画廊 ".." 被拒',
        "无效" in text_s1 and ".." not in plugin_s1.gallery_manager.galleries,
        f"res={text_s1}",
    )
    r.check(
        '创建画廊 ".." 后数据目录仍在',
        os.path.isdir(data_dir_s1),
        f"data_dir={data_dir_s1}",
    )


async def main():
    r = TestResults()
    print("\n[测试] 指令路由层")

    plugin = make_plugin()
    plugin.gallery_manager.ensure_loaded()

    # ---- 1. 非画廊指令放行 ----
    ev = MockEvent("你好呀今天天气不错")
    results = await run_handler(plugin, ev)
    assert_no_reply(r, "普通消息不回复", results)
    r.check("普通消息不拦截", ev.stopped is False)

    ev2 = MockEvent("/help")  # 其他插件指令
    results2 = await run_handler(plugin, ev2)
    assert_no_reply(r, "其他指令前缀不回复", results2)
    r.check("其他指令不拦截", ev2.stopped is False)

    # ---- 2. / 兼容与裸指令 ----
    plugin.gallery_manager.open_gall("表情")
    await plugin.gallery_manager._save()
    await add_pic(plugin, "表情", color=(200, 30, 30))
    await add_pic(plugin, "表情", color=(30, 200, 30))
    await add_pic(plugin, "表情", color=(30, 30, 200))

    def _sent_chains(ev):
        return [r for r in ev.sent if isinstance(r, tuple) and r[0] == "chain"]

    # 看图必须用 event.send 直接发整条消息链（合并消息），
    # 不能 yield——yield 会走 AstrBot 分段回复，把多图拆成多条独立消息
    ev3 = MockEvent("看 表情")  # 裸指令
    res3 = await run_handler(plugin, ev3)
    chains3 = _sent_chains(ev3)
    r.check(
        "裸指令 触发看图(直发不yield)",
        len(res3) == 0 and len(chains3) == 1,
        f"res={res3} sent={ev3.sent}",
    )
    r.check("看图后停止传播", ev3.stopped is True)

    ev4 = MockEvent("/看 表情")  # 带 / 前缀
    res4 = await run_handler(plugin, ev4)
    chains4 = _sent_chains(ev4)
    r.check(
        "/前缀 触发看图(直发)",
        len(res4) == 0 and len(chains4) == 1,
        f"res={res4} sent={ev4.sent}",
    )
    r.check(
        "图片组件在链中",
        chains4 and any(isinstance(c, Comp.Image) for c in chains4[0][1]),
    )

    # 多图必须合并在一条消息里发送（合并消息，非合并转发）
    ev_multi = MockEvent("看 表情 x3")
    res_multi = await run_handler(plugin, ev_multi)
    chains_m = _sent_chains(ev_multi)
    imgs = chains_m[0][1] if chains_m else []
    r.check(
        "x3 多图单条直发",
        len(res_multi) == 0 and len(chains_m) == 1,
        f"res={res_multi} sent={ev_multi.sent}",
    )
    r.check(
        "x3 三张图在同一条消息链",
        sum(isinstance(c, Comp.Image) for c in imgs) == 3,
        f"chain={imgs}",
    )
    r.check(
        "x3 未使用 Node 合并转发",
        not any(c.__class__.__name__ in ("Node", "Nodes") for c in imgs),
    )

    # 原版 lunabot 是前缀匹配：看miku / 看表情 中间可以没有空格
    ev_ns = MockEvent("看表情")
    res_ns = await run_handler(plugin, ev_ns)
    chains_ns = [r for r in ev_ns.sent if isinstance(r, tuple) and r[0] == "chain"]
    r.check(
        "无空格 看表情 触发看图",
        len(res_ns) == 0 and len(chains_ns) == 1,
        f"res={res_ns} sent={ev_ns.sent} stopped={ev_ns.stopped}",
    )
    r.check("无空格看图后停止传播", ev_ns.stopped is True)

    ev_ns2 = MockEvent("/看表情")
    res_ns2 = await run_handler(plugin, ev_ns2)
    chains_ns2 = [
        r for r in ev_ns2.sent if isinstance(r, tuple) and r[0] == "chain"
    ]
    r.check(
        "/看表情 无空格触发",
        len(res_ns2) == 0 and len(chains_ns2) == 1,
        f"res={res_ns2}",
    )

    ev_list_ns = MockEvent("看所有表情")
    res_list_ns = await run_handler(plugin, ev_list_ns)
    r.check(
        "看所有表情 无空格走画廊网格",
        len(res_list_ns) == 1 and res_list_ns[0][0] == "image",
        f"res={res_list_ns}",
    )

    ev_false = MockEvent("gallery")
    res_false = await run_handler(plugin, ev_false)
    assert_no_reply(r, "gallery 不误触发 gall", res_false)
    r.check("gallery 不拦截", ev_false.stopped is False)

    # ---- 3. enable_slash_prefix=False 仅裸指令 ----
    plugin2 = make_plugin({"enable_slash_prefix": False})
    plugin2.gallery_manager.ensure_loaded()
    plugin2.gallery_manager.open_gall("表情")
    await plugin2.gallery_manager._save()
    await add_pic(plugin2, "表情")

    ev5 = MockEvent("/看 表情")
    res5 = await run_handler(plugin2, ev5)
    assert_no_reply(r, "关闭斜杠兼容后 / 指令被忽略", res5)
    r.check("关闭斜杠兼容后 / 指令不拦截", ev5.stopped is False)

    ev6 = MockEvent("看 表情")
    res6 = await run_handler(plugin2, ev6)
    chains6 = [r for r in ev6.sent if isinstance(r, tuple) and r[0] == "chain"]
    r.check(
        "关闭斜杠兼容后裸指令仍生效",
        len(res6) == 0 and len(chains6) == 1,
        f"res={res6} sent={ev6.sent}",
    )

    # ---- 4. gall 子指令权限门禁（open/alias add 对所有用户开放） ----
    plugin3 = make_plugin()
    plugin3.gallery_manager.ensure_loaded()

    ev7 = MockEvent("gall open 新画廊", admin=False)
    res7 = await run_handler(plugin3, ev7)
    r.check(
        "普通用户 open 创建画廊成功",
        len(res7) == 1 and "创建成功" in res7[0][1],
        f"res={res7}",
    )
    r.check("创建后停止传播", ev7.stopped is True)
    r.check("画廊已创建", "新画廊" in plugin3.gallery_manager.galleries)

    ev8 = MockEvent("/gall open 画廊二", admin=True)
    res8 = await run_handler(plugin3, ev8)
    r.check(
        "管理员 /gall open 创建成功",
        "画廊二" in plugin3.gallery_manager.galleries
        and any("创建成功" in x[1] for x in res8 if x[0] == "plain"),
        f"res={res8}",
    )

    # ---- 4b. alias add 用户可用 / alias del 仅管理员 / close 仅管理员 ----
    ev_a1 = MockEvent("gall alias add 新画廊 mk", admin=False)
    res_a1 = await run_handler(plugin3, ev_a1)
    r.check(
        "普通用户 alias add 成功",
        len(res_a1) == 1 and "添加别名" in res_a1[0][1],
        f"res={res_a1}",
    )
    g_mk = plugin3.gallery_manager.find_gall("mk")
    r.check("别名已生效", g_mk is not None and g_mk.name == "新画廊")

    ev_a2 = MockEvent("gall alias del 新画廊 mk", admin=False)
    res_a2 = await run_handler(plugin3, ev_a2)
    r.check(
        "普通用户 alias del 被拒绝",
        len(res_a2) == 1 and "仅限管理员" in res_a2[0][1],
        f"res={res_a2}",
    )
    r.check("别名未被删除", plugin3.gallery_manager.find_gall("mk") is not None)

    ev_a3 = MockEvent("gall alias del 新画廊 mk", admin=True)
    res_a3 = await run_handler(plugin3, ev_a3)
    r.check(
        "管理员 alias del 成功",
        len(res_a3) == 1 and "删除别名" in res_a3[0][1],
        f"res={res_a3}",
    )
    r.check("别名已删除", plugin3.gallery_manager.find_gall("mk") is None)

    ev_a4 = MockEvent("gall close 画廊二", admin=False)
    res_a4 = await run_handler(plugin3, ev_a4)
    r.check(
        "普通用户 close 删除画廊被拒绝",
        len(res_a4) == 1 and "仅限管理员" in res_a4[0][1],
        f"res={res_a4}",
    )
    r.check("画廊未被删除", "画廊二" in plugin3.gallery_manager.galleries)

    ev_a5 = MockEvent("gall close 画廊二", admin=True)
    res_a5 = await run_handler(plugin3, ev_a5)
    r.check(
        "管理员 close 删除画廊成功",
        len(res_a5) == 1 and "删除成功" in res_a5[0][1],
        f"res={res_a5}",
    )
    r.check("画廊已删除", "画廊二" not in plugin3.gallery_manager.galleries)

    # ---- 4c. 中文指令：创建画廊 / 添加别名 ----
    ev_c1 = MockEvent("创建画廊 中文画廊", admin=False)
    res_c1 = await run_handler(plugin3, ev_c1)
    r.check(
        "创建画廊 中文指令创建成功",
        len(res_c1) == 1 and "创建成功" in res_c1[0][1]
        and "中文画廊" in plugin3.gallery_manager.galleries,
        f"res={res_c1}",
    )

    # "添加别名" 不能被前缀匹配成 "添加"(上传指令)
    ev_c2 = MockEvent("添加别名 中文画廊 mk别名", admin=False)
    res_c2 = await run_handler(plugin3, ev_c2)
    r.check(
        "添加别名 中文指令成功",
        len(res_c2) == 1 and "添加别名" in res_c2[0][1]
        and plugin3.gallery_manager.find_gall("mk别名") is not None,
        f"res={res_c2}",
    )

    # 原生注册路径（模拟 AstrBot CommandFilter 命中后调用）
    ev_c3 = MockEvent("创建画廊 原生中文", wake=True)
    await run_native(plugin3, ev_c3, "cmd_open_native")
    r.check(
        "原生 创建画廊 成功",
        "原生中文" in plugin3.gallery_manager.galleries,
        f"sent={ev_c3.sent}",
    )
    ev_c4 = MockEvent("添加别名 原生中文 mk2", wake=True)
    await run_native(plugin3, ev_c4, "cmd_alias_add_native")
    r.check(
        "原生 添加别名 成功",
        plugin3.gallery_manager.find_gall("mk2") is not None,
        f"sent={ev_c4.sent}",
    )

    ev_c5 = MockEvent("添加别名", wake=False)
    res_c5 = await run_handler(plugin3, ev_c5)
    r.check(
        "添加别名 缺参数返回使用方式",
        len(res_c5) == 1 and "使用方式" in res_c5[0][1],
        f"res={res_c5}",
    )

    # ---- 5. /gall 无参数返回帮助 ----
    ev9 = MockEvent("gall", admin=True)
    res9 = await run_handler(plugin3, ev9)
    r.check("gall 无子指令返回帮助", len(res9) == 1 and "画廊指令" in res9[0][1])

    # ---- 6. 中文别名指令分发 ----
    ev10 = MockEvent("看所有")
    res10 = await run_handler(plugin3, ev10)
    # 无画廊时（plugin3 有一个），应返回图片结果
    r.check("看所有 返回画廊列表图", len(res10) == 1 and res10[0][0] == "image")

    ev11 = MockEvent("下载图包")
    res11 = await run_handler(plugin3, ev11)
    assert_no_reply(r, "下载图包 已移除，消息放行", res11, f"stopped={ev11.stopped}")
    r.check("下载图包 不拦截", ev11.stopped is False)

    # ---- 7. 参数缺失/无法解析：静默放行（群聊里以「看」开头极易误触发） ----
    ev12 = MockEvent("看")
    res12 = await run_handler(plugin3, ev12)
    assert_no_reply(r, "看 无参数静默放行", res12, f"stopped={ev12.stopped}")
    r.check("看 无参数不拦截", ev12.stopped is False)

    ev12b = MockEvent("看x2")
    res12b = await run_handler(plugin3, ev12b)
    assert_no_reply(r, "看x2 空画廊名静默放行", res12b, f"stopped={ev12b.stopped}")
    r.check("看x2 不拦截", ev12b.stopped is False)

    ev12c = MockEvent("看 中文画廊 -x")
    res12c = await run_handler(plugin3, ev12c)
    assert_no_reply(r, "-N 后缀解析失败静默放行", res12c, f"stopped={ev12c.stopped}")
    r.check("-N 后缀解析失败不拦截", ev12c.stopped is False)

    ev12d = MockEvent("看 中文画廊 xabc")
    res12d = await run_handler(plugin3, ev12d)
    assert_no_reply(r, "xN 后缀解析失败静默放行", res12d, f"stopped={ev12d.stopped}")
    r.check("xN 后缀解析失败不拦截", ev12d.stopped is False)

    ev12e = MockEvent("看 - 1")
    res12e = await run_handler(plugin3, ev12e)
    assert_no_reply(r, "看 - 1 空画廊名静默放行", res12e, f"stopped={ev12e.stopped}")
    r.check("看 - 1 不拦截", ev12e.stopped is False)

    # 原生注册路径共用 _handle_cmd，同样静默放行
    ev12f = MockEvent("看", wake=True)
    res12f = await run_native(plugin3, ev12f, "cmd_pick_native")
    assert_no_reply(r, "原生 看 无参数静默放行", res12f, f"stopped={ev12f.stopped}")
    r.check("原生 看 无参数不拦截", ev12f.stopped is False)

    ev12g = MockEvent("看x2", wake=True)
    res12g = await run_native(plugin3, ev12g, "cmd_pick_native")
    assert_no_reply(r, "原生 看x2 静默放行", res12g, f"stopped={ev12g.stopped}")
    r.check("原生 看x2 不拦截", ev12g.stopped is False)

    ev12h = MockEvent("看 中文画廊 -x", wake=True)
    res12h = await run_native(plugin3, ev12h, "cmd_pick_native")
    assert_no_reply(r, "原生 看 后缀解析失败静默放行", res12h, f"stopped={ev12h.stopped}")
    r.check("原生 看 后缀解析失败不拦截", ev12h.stopped is False)

    # ---- 7b. 画廊不存在：查询类指令静默放行；写操作仍提示错误 ----
    ev_nf = MockEvent("看 不存在的画廊")
    res_nf = await run_handler(plugin3, ev_nf)
    assert_no_reply(r, "看 不存在画廊 不回复", res_nf)
    r.check("看 不存在画廊 不拦截(放行传播)", ev_nf.stopped is False)

    ev_nf2 = MockEvent("gall close 不存在的画廊", admin=True)
    res_nf2 = await run_handler(plugin3, ev_nf2)
    r.check(
        "gall close 不存在画廊 提示错误",
        len(res_nf2) == 1 and "不存在" in res_nf2[0][1],
        f"res={res_nf2}",
    )
    r.check("原有画廊未受影响", "新画廊" in plugin3.gallery_manager.galleries)

    # 上传到不存在的画廊必须提示错误，否则表现为「完全无响应」难以排查
    ev_nf3 = MockEvent("上传 不存在的画廊", admin=True)
    res_nf3 = await run_handler(plugin3, ev_nf3)
    r.check(
        "上传 不存在画廊 提示错误",
        len(res_nf3) == 1 and "不存在" in res_nf3[0][1],
        f"res={res_nf3}",
    )

    # ---- 7c. 指令触发模式：原生注册为主，listen_all 开启才启用兼容监听 ----
    print("\n[测试] 指令触发模式")
    plugin_w = make_plugin({"listen_all_messages": False})
    plugin_w.gallery_manager.ensure_loaded()
    plugin_w.gallery_manager.open_gall("唤醒测试")
    await plugin_w.gallery_manager._save()
    await add_pic(plugin_w, "唤醒测试")

    # 兼容监听关闭（默认）：on_message 不做任何事
    ev_w1 = MockEvent("看 唤醒测试", wake=False)
    res_w1 = await run_handler(plugin_w, ev_w1)
    assert_no_reply(r, "listen_all 关闭时监听器不触发", res_w1, f"stopped={ev_w1.stopped}")
    r.check("listen_all 关闭时不拦截", ev_w1.stopped is False)
    ev_w2 = MockEvent("随便聊聊天气", wake=False)
    res_w2 = await run_handler(plugin_w, ev_w2)
    assert_no_reply(r, "listen_all 关闭时普通消息放行", res_w2, f"stopped={ev_w2.stopped}")
    r.check("listen_all 关闭时普通消息不拦截", ev_w2.stopped is False)

    # 原生指令注册：模拟 AstrBot CommandFilter 命中后调用对应 handler
    ev_w3 = MockEvent("看 唤醒测试", wake=True)
    await run_native(plugin_w, ev_w3, "cmd_pick_native")
    chains_w3 = [r for r in ev_w3.sent if isinstance(r, tuple) and r[0] == "chain"]
    r.check("原生指令 看图 触发", len(chains_w3) == 1, f"sent={ev_w3.sent}")
    r.check("原生指令处理后停止传播", ev_w3.stopped is True)

    ev_w4 = MockEvent("gall open 原生画廊", wake=True)
    await run_native(plugin_w, ev_w4, "cmd_gall_native")
    r.check(
        "原生指令 gall open 创建画廊",
        "原生画廊" in plugin_w.gallery_manager.galleries,
        f"sent={ev_w4.sent}",
    )

    ev_w4b = MockEvent("gall alias add 唤醒测试 mk", wake=True)
    await run_native(plugin_w, ev_w4b, "cmd_gall_native")
    r.check(
        "原生指令 alias add 成功",
        plugin_w.gallery_manager.find_gall("mk") is not None,
        f"sent={ev_w4b.sent}",
    )

    # 开启 listen_all_messages：未唤醒裸指令由 on_message 兜底（含无空格写法）
    plugin_w2 = make_plugin()
    plugin_w2.gallery_manager.ensure_loaded()
    plugin_w2.gallery_manager.open_gall("唤醒测试")
    await plugin_w2.gallery_manager._save()
    await add_pic(plugin_w2, "唤醒测试")
    ev_w5 = MockEvent("看唤醒测试", wake=False)
    await run_handler(plugin_w2, ev_w5)
    chains_w5 = [r for r in ev_w5.sent if isinstance(r, tuple) and r[0] == "chain"]
    r.check(
        "开启全监听后无空格裸指令触发",
        len(chains_w5) == 1,
        f"sent={ev_w5.sent}",
    )
    ev_w6 = MockEvent("随便聊聊天气", wake=False)
    res_w6 = await run_handler(plugin_w2, ev_w6)
    assert_no_reply(r, "开启全监听后非指令仍放行", res_w6, f"stopped={ev_w6.stopped}")
    r.check("开启全监听后非指令不拦截", ev_w6.stopped is False)

    # ---- 8. 合并转发消息解析 ----
    print("\n[测试] 合并转发消息解析")

    # 8a. 内联 Nodes/Node 节点中的图片
    evf1 = MockEvent("上传 合并")
    evf1.message_obj.message = [
        Comp.Nodes(nodes=[
            Comp.Node(content=[Comp.Image(url="http://ex.com/fw1.jpg")]),
            Comp.Node(content=[
                Comp.Plain("看看这张"),
                Comp.Image(url="http://ex.com/fw2.png"),
            ]),
            Comp.Node(content=[]),  # 空节点应跳过
        ]),
    ]
    urls1 = await plugin._extract_image_urls(evf1)
    r.check(
        "内联 Node/Nodes 提取图片",
        urls1 == ["http://ex.com/fw1.jpg", "http://ex.com/fw2.png"],
        f"urls={urls1}",
    )

    # 单个 Node 直接出现
    # 单个 Node 直接出现；file 指向真实本地文件才可提取
    # （内部 hash 或不存在的路径既非 URL 也非本地文件，会被过滤）
    from PIL import Image as _PImgN
    node_img = os.path.join(tempfile.gettempdir(), "gallery_node_img.png")
    _PImgN.new("RGBA", (40, 40), (9, 9, 9, 255)).save(node_img, format="PNG")
    evf2 = MockEvent("上传 合并2")
    evf2.message_obj.message = [Comp.Node(content=[Comp.Image(file=node_img)])]
    urls2 = await plugin._extract_image_urls(evf2)
    r.check("单个 Node 提取图片", urls2 == [node_img], f"urls={urls2}")

    # 8b. Forward 组件：通过 get_forward_msg API 拉取
    fwd_payload = {
        "messages": [
            {"content": [
                {"type": "text", "data": {"text": "聊天记录"}},
                {"type": "image", "data": {"url": "http://ex.com/api1.jpg"}},
            ]},
            # 嵌套 node-as-segment 形态（部分协议端）
            {"type": "node", "data": {"content": [
                {"type": "image", "data": {"url": "http://ex.com/api2.jpg"}},
            ]}},
        ]
    }
    bot_api = MockBotAPI({
        ("get_forward_msg", "FWD123"): fwd_payload,
    })
    evf3 = MockEvent("上传 合并3", bot_api=bot_api)
    evf3.message_obj.message = [Comp.Forward(id="FWD123")]
    urls3 = await plugin._extract_image_urls(evf3)
    r.check(
        "Forward 经 get_forward_msg 提取图片(含嵌套node)",
        urls3 == ["http://ex.com/api1.jpg", "http://ex.com/api2.jpg"],
        f"urls={urls3}",
    )
    r.check("get_forward_msg 使用 message_id 参数",
            any(a == "get_forward_msg" and k.get("message_id") == "FWD123"
                for a, k in bot_api.calls))

    # 同一 Forward 只拉取一次
    evf4 = MockEvent("上传 合并4", bot_api=MockBotAPI({
        ("get_forward_msg", "FWD123"): fwd_payload,
    }))
    evf4.message_obj.message = [Comp.Forward(id="FWD123"), Comp.Forward(id="FWD123")]
    await plugin._extract_image_urls(evf4)
    calls_cnt = sum(1 for a, _ in evf4.bot.api.calls if a == "get_forward_msg")
    r.check("重复 Forward 去重只拉取一次", calls_cnt == 1, f"calls={calls_cnt}")

    # API 失败时静默返回空（不抛错）
    class _BoomAPI:
        async def call_action(self, action, **kwargs):
            raise RuntimeError("boom")
    evf5 = MockEvent("上传 合并5")
    evf5.bot = types.SimpleNamespace(api=_BoomAPI())
    evf5.message_obj.message = [Comp.Forward(id="X")]
    try:
        urls5 = await plugin._extract_image_urls(evf5)
        r.check("API 失败不崩溃返回空", urls5 == [], f"urls={urls5}")
    except Exception as e:
        r.check("API 失败不崩溃返回空", False, f"异常: {e}")

    # 引用消息里带 Forward（回复合并转发）
    evf6 = MockEvent("上传 回复合并", bot_api=MockBotAPI({
        ("get_forward_msg", "FWDR"): fwd_payload,
    }))
    evf6.message_obj.message = [
        Comp.Reply(chain=[Comp.Forward(id="FWDR")]),
    ]
    urls6 = await plugin._extract_image_urls(evf6)
    r.check(
        "Reply 内嵌 Forward 解析",
        len(urls6) == 2 and all(u.startswith("http://ex.com/api") for u in urls6),
        f"urls={urls6}",
    )

    # 8c. Reply 无 chain：经 get_msg 拉取被引用消息（适配器拉取失败时的兜底）
    evr1 = MockEvent("上传 引用1", bot_api=MockBotAPI({
        ("get_msg", "R1"): {
            "message_id": "R1",
            "message": [
                {"type": "text", "data": {"text": "看看这张"}},
                {"type": "image", "data": {"url": "http://ex.com/r1.jpg"}},
            ],
        },
    }))
    evr1.message_obj.message = [Comp.Reply(id="R1")]
    urls_r1 = await plugin._extract_image_urls(evr1)
    r.check(
        "Reply 无chain 经 get_msg 提取",
        urls_r1 == ["http://ex.com/r1.jpg"],
        f"urls={urls_r1}",
    )

    # Reply 有 chain 时直接用 chain，不发 API
    evr2 = MockEvent("上传 引用2", bot_api=MockBotAPI({}))
    evr2.message_obj.message = [
        Comp.Reply(id="R2", chain=[Comp.Image(url="http://ex.com/r2.jpg")]),
    ]
    urls_r2 = await plugin._extract_image_urls(evr2)
    r.check(
        "Reply 有chain 直接提取不发API",
        urls_r2 == ["http://ex.com/r2.jpg"] and len(evr2.bot.api.calls) == 0,
        f"urls={urls_r2} calls={evr2.bot.api.calls}",
    )

    # get_msg 失败时静默返回空（不崩）
    evr3 = MockEvent("上传 引用3")
    evr3.bot = types.SimpleNamespace(api=_BoomAPI())
    evr3.message_obj.message = [Comp.Reply(id="R3")]
    try:
        urls_r3 = await plugin._extract_image_urls(evr3)
        r.check("get_msg 失败不崩溃", urls_r3 == [], f"urls={urls_r3}")
    except Exception as e:
        r.check("get_msg 失败不崩溃", False, f"异常: {e}")

    # ---- 8d. 文件形式图片（File 组件） ----
    print("\n[测试] 文件形式图片提取")

    # 8d-1. Lagrange/新版适配器：File 带 name + url
    evF1 = MockEvent("上传 文件1")
    evF1.message_obj.message = [
        Comp.File(name="表情.jpg", url="http://ex.com/file1.jpg"),
        Comp.File(name="文档.pdf", url="http://ex.com/doc.pdf"),  # 非图片应跳过
        Comp.File(name="表情2.png", url="http://ex.com/file2.png"),
    ]
    urls_f1 = await plugin._extract_image_urls(evF1)
    r.check(
        "File 组件按扩展名提取图片",
        urls_f1 == ["http://ex.com/file1.jpg", "http://ex.com/file2.png"],
        f"urls={urls_f1}",
    )

    # 8d-2. File 带本地路径（file_ 字段）
    file_img = os.path.join(tempfile.gettempdir(), "gallery_file_upload.png")
    from PIL import Image as _PImgF
    _PImgF.new("RGBA", (40, 40), (30, 20, 10, 255)).save(file_img, format="PNG")
    evF2 = MockEvent("上传 文件2")
    evF2.message_obj.message = [Comp.File(name="图.png", file_=file_img)]
    urls_f2 = await plugin._extract_image_urls(evF2)
    r.check("File file_ 本地路径提取", urls_f2 == [file_img], f"urls={urls_f2}")

    # 8d-3. 旧版 File：file 是内部 hash、无 url → 经 get_group_file_url 换链接
    evF3 = MockEvent("上传 文件3", bot_api=MockBotAPI({
        "get_group_file_url": {"url": "http://ex.com/gf1.gif"},
    }))
    evF3.get_group_id = lambda: "12345"  # MockEvent 默认私聊，改为群聊
    evF3.message_obj.message = [Comp.File(name="梗.gif", file="ABC123.hash", id="FID1")]
    urls_f3 = await plugin._extract_image_urls(evF3)
    r.check(
        "旧版 File 经 get_group_file_url 解析",
        urls_f3 == ["http://ex.com/gf1.gif"],
        f"urls={urls_f3}",
    )
    r.check(
        "get_group_file_url 参数正确",
        any(a == "get_group_file_url"
            and k.get("file_id") == "FID1" and k.get("group_id") == 12345
            for a, k in evF3.bot.api.calls),
        f"calls={evF3.bot.api.calls}",
    )

    # 8d-4. 新版 File 仅有 get_file() 方法（旧签名，无 allow_return_url）
    class _FileLike(Comp.File):
        name = "表情.webp"

        async def get_file(self):
            return file_img
    evF4 = MockEvent("上传 文件4")
    evF4.message_obj.message = [_FileLike()]
    urls_f4 = await plugin._extract_image_urls(evF4)
    r.check("File.get_file() 兜底解析", urls_f4 == [file_img], f"urls={urls_f4}")

    # 8d-5. 转发消息里的 file segment（raw 层解析）
    fwd_file_payload = {
        "messages": [
            {"content": [
                {"type": "file", "data": {
                    "url": "http://ex.com/fwfile.jpg", "file_name": "a.jpg"}},
                # 无 url 时经 file_id 解析
                {"type": "file", "data": {"file_id": "FID2", "file_name": "b.png"}},
            ]},
            {"content": [
                {"type": "file", "data": {"file_id": "FID3", "file_name": "readme.txt"}},
            ]},
        ]
    }
    evF5 = MockEvent("上传 文件5", bot_api=MockBotAPI({
        ("get_forward_msg", "FWDF"): fwd_file_payload,
        "get_group_file_url": {"url": "http://ex.com/fid2.png"},
    }))
    evF5.get_group_id = lambda: "12345"
    evF5.message_obj.message = [Comp.Forward(id="FWDF")]
    urls_f5 = await plugin._extract_image_urls(evF5)
    r.check(
        "转发内 file segment 提取(含API兜底/非图片过滤)",
        urls_f5 == ["http://ex.com/fwfile.jpg", "http://ex.com/fid2.png"],
        f"urls={urls_f5}",
    )

    # 8d-6. 引用消息里的 file segment（get_msg 兜底链路）
    evF6 = MockEvent("上传 文件6", bot_api=MockBotAPI({
        ("get_msg", "RF1"): {
            "message_id": "RF1",
            "message": [
                {"type": "file", "data": {
                    "url": "http://ex.com/rfile.png", "file_name": "c.png"}},
            ],
        },
    }))
    evF6.message_obj.message = [Comp.Reply(id="RF1")]
    urls_f6 = await plugin._extract_image_urls(evF6)
    r.check(
        "Reply 引用 file segment 提取",
        urls_f6 == ["http://ex.com/rfile.png"],
        f"urls={urls_f6}",
    )

    ok = r.summary()

    # ---- 9. 上传：本地路径图片不应被当 URL 下载 ----
    print("\n[测试] 本地路径图片上传")
    plugin4 = make_plugin()
    plugin4.gallery_manager.ensure_loaded()
    plugin4.gallery_manager.open_gall("本地图")
    await plugin4.gallery_manager._save()

    # aiocqhttp 下 Image.file 常是本地相对路径或 file:/// 路径，不是 URL
    import tempfile as _tf
    local_img = os.path.join(_tf.gettempdir(), "gallery_local_upload.png")
    from PIL import Image as _PImg
    _PImg.new("RGBA", (40, 40), (10, 20, 30, 255)).save(local_img, format="PNG")

    ev_local = MockEvent("上传 本地图")
    ev_local.message_obj.message = [Comp.Image(file=local_img)]  # 纯本地路径，无 url
    res_local = await run_handler(plugin4, ev_local)
    # 不应出现 "下载图片失败" / InvalidUrlClientError，且应上传成功
    uploaded = len(plugin4.gallery_manager.galleries["本地图"].pics)
    r.check(
        "本地路径图片直接使用不下载",
        uploaded == 1 and not any("下载图片失败" in str(x) for x in res_local),
        f"uploaded={uploaded} res={res_local}",
    )

    # file:/// 前缀也应识别为本地
    plugin4.gallery_manager.galleries["本地图"].pics.clear()
    local_img2 = os.path.join(_tf.gettempdir(), "gallery_local_upload2.png")
    _PImg.new("RGBA", (40, 40), (40, 50, 60, 255)).save(local_img2, format="PNG")
    ev_local2 = MockEvent("上传 本地图")
    ev_local2.message_obj.message = [Comp.Image(file=f"file:///{local_img2}")]
    await run_handler(plugin4, ev_local2)
    r.check(
        "file:/// 路径识别为本地",
        len(plugin4.gallery_manager.galleries["本地图"].pics) == 1,
        f"pics={len(plugin4.gallery_manager.galleries['本地图'].pics)}",
    )

    # ---- 10. GIF 动图经 URL 上传后应保留 .gif 与多帧 ----
    print("\n[测试] GIF 动图上传保留格式")
    plugin5 = make_plugin()
    plugin5.gallery_manager.ensure_loaded()
    plugin5.gallery_manager.open_gall("动图")
    await plugin5.gallery_manager._save()

    # 造一张真正的多帧 gif
    from PIL import Image as _PImg2
    frames = [_PImg2.new("P", (64, 64)) for _ in range(3)]
    for i, f in enumerate(frames):
        f.putpalette(list(((255, 0, 0), (0, 255, 0), (0, 0, 255))[i]) * 3)
    gif_buf = _tf.NamedTemporaryFile(suffix=".gif", delete=False)
    frames[0].save(
        gif_buf, format="GIF", save_all=True,
        append_images=frames[1:], duration=200, loop=0, disposal=2,
    )
    gif_buf.close()
    with open(gif_buf.name, "rb") as _f:
        gif_bytes = _f.read()
    _url_to_bytes["http://ex.com/anim.gif"] = gif_bytes

    ev_gif = MockEvent("上传 动图")
    ev_gif.message_obj.message = [Comp.Image(url="http://ex.com/anim.gif")]
    await run_handler(plugin5, ev_gif)

    pics = plugin5.gallery_manager.galleries["动图"].pics
    r.check("GIF 经 URL 上传成功", len(pics) == 1, f"pics={len(pics)}")
    if pics:
        pic = pics[0]
        r.check(
            "落库扩展名为 .gif",
            pic.file.lower().endswith(".gif"),
            f"file={pic.file}",
        )
        im = _PImg2.open(pic.path)
        r.check(
            "落库仍是动图(多帧)",
            getattr(im, "n_frames", 1) == 3 and getattr(im, "is_animated", False),
            f"frames={getattr(im, 'n_frames', 1)}",
        )

    # aiocqhttp 常见情况：Image.file 是本地 .jpg/.png 路径，但内容其实是 GIF
    plugin6 = make_plugin()
    plugin6.gallery_manager.ensure_loaded()
    plugin6.gallery_manager.open_gall("动图2")
    await plugin6.gallery_manager._save()
    misnamed = os.path.join(_tf.gettempdir(), "media_image_gif_as_png.png")
    with open(misnamed, "wb") as _f:
        _f.write(gif_bytes)
    ev_mis = MockEvent("上传 动图2")
    ev_mis.message_obj.message = [Comp.Image(file=misnamed)]
    await run_handler(plugin6, ev_mis)
    pics6 = plugin6.gallery_manager.galleries["动图2"].pics
    r.check("误标 png 的 GIF 仍能上传", len(pics6) == 1, f"pics={len(pics6)}")
    if pics6:
        pic6 = pics6[0]
        r.check(
            "误标 png 的 GIF 落库为 .gif",
            pic6.file.lower().endswith(".gif"),
            f"file={pic6.file}",
        )
        im6 = _PImg2.open(pic6.path)
        r.check(
            "误标 png 的 GIF 仍是动图",
            getattr(im6, "n_frames", 1) == 3 and getattr(im6, "is_animated", False),
            f"frames={getattr(im6, 'n_frames', 1)} fmt={im6.format}",
        )

    # 静态图经 sub_type=1 转为 gif 后，落库扩展名必须与内容一致（.gif）
    plugin7 = make_plugin()
    plugin7.gallery_manager.ensure_loaded()
    plugin7.gallery_manager.open_gall("静图")
    await plugin7.gallery_manager._save()
    static_png = os.path.join(_tf.gettempdir(), "gallery_static_upload.png")
    _PImg2.new("RGBA", (60, 60), (10, 100, 200, 255)).save(static_png, format="PNG")
    ev_s = MockEvent("上传 静图")
    ev_s.message_obj.message = [Comp.Image(file=static_png)]
    await run_handler(plugin7, ev_s)
    pics7 = plugin7.gallery_manager.galleries["静图"].pics
    r.check("静态图上传成功", len(pics7) == 1, f"pics={len(pics7)}")
    if pics7:
        im7 = _PImg2.open(pics7[0].path)
        r.check(
            "静态图转gif后扩展名对齐",
            pics7[0].file.lower().endswith(".gif") and im7.format == "GIF",
            f"file={pics7[0].file} fmt={im7.format}",
        )

    # ---- 11. 引用图片上传端到端（NapCat 真实形态）----
    print("\n[测试] 引用图片上传端到端")
    import io as _io

    def _png_bytes(color):
        buf = _io.BytesIO()
        _PImg2.new("RGBA", (30, 30), color + (255,)).save(buf, format="PNG")
        return buf.getvalue()

    _url_to_bytes["http://ex.com/e2e1.jpg"] = _png_bytes((1, 2, 3))
    _url_to_bytes["http://ex.com/e2e2.jpg"] = _png_bytes((4, 5, 6))

    def _png_bytes_sized(color, size):
        buf = _io.BytesIO()
        _PImg2.new("RGBA", size, color + (255,)).save(buf, format="PNG")
        return buf.getvalue()

    # 纯色小图感知哈希相同会被判重，11d 用不同尺寸与颜色
    _url_to_bytes["http://ex.com/e2e3.jpg"] = _png_bytes_sized((200, 100, 50), (60, 40))

    # 11a. 适配器已填充 chain：回复图片 + 上传
    plugin8 = make_plugin()
    plugin8.gallery_manager.ensure_loaded()
    plugin8.gallery_manager.open_gall("引用传")
    await plugin8.gallery_manager._save()
    ev_e1 = MockEvent("上传 引用传")
    ev_e1.message_obj.message = [
        Comp.Reply(id="E1", chain=[Comp.Image(url="http://ex.com/e2e1.jpg")]),
    ]
    await run_handler(plugin8, ev_e1)
    r.check(
        "回复图片(chain)上传成功",
        len(plugin8.gallery_manager.galleries["引用传"].pics) == 1,
        f"pics={len(plugin8.gallery_manager.galleries['引用传'].pics)}",
    )

    # 11b. 适配器未填充 chain：经 get_msg 兜底后上传
    plugin9 = make_plugin()
    plugin9.gallery_manager.ensure_loaded()
    plugin9.gallery_manager.open_gall("引用传2")
    await plugin9.gallery_manager._save()
    ev_e2 = MockEvent("上传 引用传2", bot_api=MockBotAPI({
        ("get_msg", "E2"): {
            "message_id": "E2",
            "message": [
                {"type": "image", "data": {"url": "http://ex.com/e2e2.jpg"}},
            ],
        },
    }))
    ev_e2.message_obj.message = [Comp.Reply(id="E2")]
    await run_handler(plugin9, ev_e2)
    r.check(
        "回复图片(无chain兜底)上传成功",
        len(plugin9.gallery_manager.galleries["引用传2"].pics) == 1,
        f"pics={len(plugin9.gallery_manager.galleries['引用传2'].pics)}",
    )

    # 11c. chain 里的 Image 丢了 url/file（NapCat 部分版本行为）：
    # chain 提取不到时必须回退 get_msg 拉原始 segment
    plugin10 = make_plugin()
    plugin10.gallery_manager.ensure_loaded()
    plugin10.gallery_manager.open_gall("引用传3")
    await plugin10.gallery_manager._save()
    ev_e3 = MockEvent("上传 引用传3", bot_api=MockBotAPI({
        ("get_msg", "E3"): {
            "message_id": "E3",
            "message": [
                {"type": "image", "data": {"url": "http://ex.com/e2e1.jpg"}},
            ],
        },
    }))
    ev_e3.message_obj.message = [
        Comp.Reply(id="E3", chain=[Comp.Image(file="", url="")]),  # 字段全空
    ]
    await run_handler(plugin10, ev_e3)
    r.check(
        "chain字段全空时回退get_msg上传成功",
        len(plugin10.gallery_manager.galleries["引用传3"].pics) == 1,
        f"pics={len(plugin10.gallery_manager.galleries['引用传3'].pics)}",
    )

    # 11d. file 为 NapCat 内部 hash（"ABC.image"，既非路径也非URL）不应直接当 URL 下载
    ev_e4 = MockEvent("上传 引用传3", bot_api=MockBotAPI({
        ("get_msg", "E4"): {
            "message_id": "E4",
            "message": [
                {"type": "image", "data": {"url": "http://ex.com/e2e3.jpg"}},
            ],
        },
    }))
    ev_e4.message_obj.message = [
        Comp.Reply(id="E4", chain=[Comp.Image(file="ABC123.image", url="")]),
    ]
    await run_handler(plugin10, ev_e4)
    r.check(
        "file为内部hash时回退get_msg上传成功",
        len(plugin10.gallery_manager.galleries["引用传3"].pics) == 2,
        f"pics={len(plugin10.gallery_manager.galleries['引用传3'].pics)}",
    )

    # ---- 12. AI 审核 ----
    print("\n[测试] AI 审核")

    # 12a. 审核不通过：图片拒绝入库
    prov_a = make_review_provider(['{"pass": false, "reason": "疑似广告二维码"}'])
    plugin_ra = make_plugin({"ai_review": {"enable": True, "batch_notice_threshold": 3}})
    plugin_ra.context.get_using_provider = lambda: prov_a
    plugin_ra.gallery_manager.ensure_loaded()
    plugin_ra.gallery_manager.open_gall("审核画廊")
    await plugin_ra.gallery_manager._save()
    ev_ra = MockEvent("上传 审核画廊")
    ev_ra.message_obj.message = [Comp.Image(file=make_review_img(112233))]
    res_ra = await run_handler(plugin_ra, ev_ra)
    r.check(
        "AI不通过拒绝上传",
        "未通过AI审核" in chain_text(res_ra),
        f"res={chain_text(res_ra)}",
    )
    r.check(
        "被拒图片未入库",
        len(plugin_ra.gallery_manager.galleries["审核画廊"].pics) == 0,
        f"pics={plugin_ra.gallery_manager.galleries['审核画廊'].pics}",
    )
    r.check("审核调用了LLM", len(prov_a.calls) == 1, f"calls={len(prov_a.calls)}")

    # 12a-2. pass 为字符串 "false"：必须判为不通过
    # （bool("false") 是 True，旧实现会把模型拒绝当成通过）
    prov_a2 = make_review_provider(['{"pass": "false", "reason": "疑似广告二维码"}'])
    plugin_ra2 = make_plugin({"ai_review": {"enable": True, "batch_notice_threshold": 99}})
    plugin_ra2.context.get_using_provider = lambda: prov_a2
    plugin_ra2.gallery_manager.ensure_loaded()
    plugin_ra2.gallery_manager.open_gall("审核画廊1b")
    await plugin_ra2.gallery_manager._save()
    ev_ra2 = MockEvent("上传 审核画廊1b")
    ev_ra2.message_obj.message = [Comp.Image(file=make_review_img(223344))]
    res_ra2 = await run_handler(plugin_ra2, ev_ra2)
    r.check(
        'AI返回pass字符串"false"时拒绝',
        "未通过AI审核" in chain_text(res_ra2),
        f"res={chain_text(res_ra2)}",
    )
    r.check(
        'pass字符串"false"的图片未入库',
        len(plugin_ra2.gallery_manager.galleries["审核画廊1b"].pics) == 0,
        f"pics={plugin_ra2.gallery_manager.galleries['审核画廊1b'].pics}",
    )

    # 12a-3. JSON 里缺 pass 键：按「解析失败」放行，不能默认判定
    prov_a3 = make_review_provider(['{"reason": "模型只给了理由", "score": 0}'])
    plugin_ra3 = make_plugin({"ai_review": {"enable": True, "batch_notice_threshold": 99}})
    plugin_ra3.context.get_using_provider = lambda: prov_a3
    plugin_ra3.gallery_manager.ensure_loaded()
    plugin_ra3.gallery_manager.open_gall("审核画廊1c")
    await plugin_ra3.gallery_manager._save()
    ev_ra3 = MockEvent("上传 审核画廊1c")
    ev_ra3.message_obj.message = [Comp.Image(file=make_review_img(334455))]
    res_ra3 = await run_handler(plugin_ra3, ev_ra3)
    r.check(
        "AI输出缺pass键时按解析失败放行",
        "成功上传1/1" in chain_text(res_ra3)
        and len(plugin_ra3.gallery_manager.galleries["审核画廊1c"].pics) == 1,
        f"res={chain_text(res_ra3)}",
    )

    # 12b. 审核通过：正常入库
    prov_b = make_review_provider(['{"pass": true}'])
    plugin_rb = make_plugin({"ai_review": {"enable": True, "batch_notice_threshold": 3}})
    plugin_rb.context.get_using_provider = lambda: prov_b
    plugin_rb.gallery_manager.ensure_loaded()
    plugin_rb.gallery_manager.open_gall("审核画廊2")
    await plugin_rb.gallery_manager._save()
    ev_rb = MockEvent("上传 审核画廊2")
    ev_rb.message_obj.message = [Comp.Image(file=make_review_img(445566))]
    res_rb = await run_handler(plugin_rb, ev_rb)
    r.check(
        "AI通过正常上传",
        "成功上传1/1" in chain_text(res_rb)
        and len(plugin_rb.gallery_manager.galleries["审核画廊2"].pics) == 1,
        f"res={chain_text(res_rb)}",
    )

    # 12c. 批量上传达到阈值：先提示等待 AI 审核
    prov_c = make_review_provider(['{"pass": true}'])
    plugin_rc = make_plugin({"ai_review": {"enable": True, "batch_notice_threshold": 2}})
    plugin_rc.context.get_using_provider = lambda: prov_c
    plugin_rc.gallery_manager.ensure_loaded()
    plugin_rc.gallery_manager.open_gall("审核画廊3")
    await plugin_rc.gallery_manager._save()
    ev_rc = MockEvent("上传 审核画廊3")
    ev_rc.message_obj.message = [
        Comp.Image(file=make_review_img(10203)),
        Comp.Image(file=make_review_img(30405)),
        Comp.Image(file=make_review_img(50607)),
    ]
    res_rc = await run_handler(plugin_rc, ev_rc)
    r.check(
        "批量上传先提示AI审核等待",
        len(res_rc) >= 2 and "AI 审核" in chain_text(res_rc[:1]),
        f"res={[x[0] for x in res_rc]}",
    )
    r.check("批量审核每张都送审", len(prov_c.calls) == 3, f"calls={len(prov_c.calls)}")
    r.check(
        "批量全部通过入库",
        len(plugin_rc.gallery_manager.galleries["审核画廊3"].pics) == 3,
        f"pics={len(plugin_rc.gallery_manager.galleries['审核画廊3'].pics)} "
        f"msg={chain_text(res_rc)}",
    )

    # 12d. 未达阈值不提示
    prov_d = make_review_provider(['{"pass": true}'])
    plugin_rd = make_plugin({"ai_review": {"enable": True, "batch_notice_threshold": 10}})
    plugin_rd.context.get_using_provider = lambda: prov_d
    plugin_rd.gallery_manager.ensure_loaded()
    plugin_rd.gallery_manager.open_gall("审核画廊4")
    await plugin_rd.gallery_manager._save()
    ev_rd = MockEvent("上传 审核画廊4")
    ev_rd.message_obj.message = [Comp.Image(file=make_review_img(121314))]
    res_rd = await run_handler(plugin_rd, ev_rd)
    r.check(
        "未达阈值不提示直接上传",
        len(res_rd) == 1 and "成功上传1/1" in chain_text(res_rd),
        f"res={[x[0] for x in res_rd]} {chain_text(res_rd)}",
    )

    # 12e. 审核服务异常：降级放行不阻塞上传
    prov_e = make_review_provider(RuntimeError("provider down"))
    plugin_re = make_plugin({"ai_review": {"enable": True, "batch_notice_threshold": 99}})
    plugin_re.context.get_using_provider = lambda: prov_e
    plugin_re.gallery_manager.ensure_loaded()
    plugin_re.gallery_manager.open_gall("审核画廊5")
    await plugin_re.gallery_manager._save()
    ev_re = MockEvent("上传 审核画廊5")
    ev_re.message_obj.message = [Comp.Image(file=make_review_img(212223))]
    res_re = await run_handler(plugin_re, ev_re)
    r.check(
        "审核服务异常降级放行",
        "成功上传1/1" in chain_text(res_re)
        and len(plugin_re.gallery_manager.galleries["审核画廊5"].pics) == 1,
        f"res={chain_text(res_re)}",
    )

    # 12f. 输出无法解析：放行
    prov_f = make_review_provider(["模型说了一堆没有JSON的话"])
    plugin_rf = make_plugin({"ai_review": {"enable": True, "batch_notice_threshold": 99}})
    plugin_rf.context.get_using_provider = lambda: prov_f
    plugin_rf.gallery_manager.ensure_loaded()
    plugin_rf.gallery_manager.open_gall("审核画廊6")
    await plugin_rf.gallery_manager._save()
    ev_rf = MockEvent("上传 审核画廊6")
    ev_rf.message_obj.message = [Comp.Image(file=make_review_img(313233))]
    res_rf = await run_handler(plugin_rf, ev_rf)
    r.check(
        "审核输出无法解析时放行",
        "成功上传1/1" in chain_text(res_rf),
        f"res={chain_text(res_rf)}",
    )

    # 12g. 审核关闭：完全不调用 provider（连获取 provider 都不应发生）
    plugin_rg = make_plugin()
    provider_calls_g = []

    def _spy_using_provider():
        provider_calls_g.append("using")
        return make_review_provider(['{"pass": true}'])

    def _spy_provider_by_id(pid):
        provider_calls_g.append(("id", pid))
        return make_review_provider(['{"pass": true}'])

    plugin_rg.context.get_using_provider = _spy_using_provider
    plugin_rg.context.get_provider_by_id = _spy_provider_by_id
    plugin_rg.gallery_manager.ensure_loaded()
    plugin_rg.gallery_manager.open_gall("审核画廊7")
    await plugin_rg.gallery_manager._save()
    ev_rg = MockEvent("上传 审核画廊7")
    ev_rg.message_obj.message = [Comp.Image(file=make_review_img(414243))]
    res_rg = await run_handler(plugin_rg, ev_rg)
    r.check(
        "审核关闭时正常上传",
        "成功上传1/1" in chain_text(res_rg),
        f"res={chain_text(res_rg)}",
    )
    r.check(
        "审核关闭时 provider 完全未被调用",
        provider_calls_g == [],
        f"calls={provider_calls_g}",
    )

    # 12h. 指定 provider_id 走 get_provider_by_id
    prov_h = make_review_provider(['{"pass": true}'])
    plugin_rh = make_plugin({
        "ai_review": {"enable": True, "batch_notice_threshold": 99,
                      "provider_id": "my_review_llm"},
    })
    plugin_rh.context.get_provider_by_id = lambda pid: (
        prov_h if pid == "my_review_llm" else None
    )
    def _no_default():
        raise AssertionError("不应使用默认提供者")
    plugin_rh.context.get_using_provider = _no_default
    plugin_rh.gallery_manager.ensure_loaded()
    plugin_rh.gallery_manager.open_gall("审核画廊8")
    await plugin_rh.gallery_manager._save()
    ev_rh = MockEvent("上传 审核画廊8")
    ev_rh.message_obj.message = [Comp.Image(file=make_review_img(515253))]
    res_rh = await run_handler(plugin_rh, ev_rh)
    r.check(
        "provider_id 指定审核提供者",
        "成功上传1/1" in chain_text(res_rh) and len(prov_h.calls) == 1,
        f"res={chain_text(res_rh)} calls={len(prov_h.calls)}",
    )

    await test_regressions(r)
    ok2 = r.summary()
    sys.exit(0 if ok and ok2 else 1)


if __name__ == "__main__":
    asyncio.run(main())
