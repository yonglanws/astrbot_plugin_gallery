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
    astrbot_api.logger = types.SimpleNamespace(
        info=print, warning=print, error=print, debug=lambda *a: None
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

    def __init__(self, message_str, admin=False, bot_api: MockBotAPI | None = None):
        self.message_str = message_str
        self._admin = admin
        self.message_obj = types.SimpleNamespace(message=[])
        self.stopped = False
        self.sent = []
        if bot_api is not None:
            self.bot = types.SimpleNamespace(api=bot_api)

    def is_admin(self):
        return self._admin

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


async def run_handler(plugin, event):
    """收集 on_message 异步生成器的所有结果。"""
    results = []
    async for r in plugin.on_message(event):
        results.append(r)
    return results


def make_plugin(config=None):
    return GalleryPlugin(types.SimpleNamespace(), dict(config or {}))


async def add_pic(plugin, gall, color=(200, 30, 30)):
    from PIL import Image as PImage
    p = os.path.join(plugin.tmp_dir, f"rt_{color}.png")
    PImage.new("RGBA", (60, 60), color + (255,)).save(p, format="PNG")
    return await plugin.gallery_manager.async_add_pic(gall, p, check_duplicated=False)


async def main():
    r = TestResults()
    print("\n[测试] 指令路由层")

    plugin = make_plugin()
    plugin.gallery_manager.ensure_loaded()

    # ---- 1. 非画廊指令放行 ----
    ev = MockEvent("你好呀今天天气不错")
    results = await run_handler(plugin, ev)
    r.check("普通消息不回复", len(results) == 0, f"results={results}")
    r.check("普通消息不拦截", ev.stopped is False)

    ev2 = MockEvent("/help")  # 其他插件指令
    results2 = await run_handler(plugin, ev2)
    r.check("其他指令前缀不回复", len(results2) == 0)
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
    r.check("gallery 不误触发 gall", len(res_false) == 0 and ev_false.stopped is False)

    # ---- 3. enable_slash_prefix=False 仅裸指令 ----
    plugin2 = make_plugin({"enable_slash_prefix": False})
    plugin2.gallery_manager.ensure_loaded()
    plugin2.gallery_manager.open_gall("表情")
    await plugin2.gallery_manager._save()
    await add_pic(plugin2, "表情")

    ev5 = MockEvent("/看 表情")
    res5 = await run_handler(plugin2, ev5)
    r.check("关闭斜杠兼容后 / 指令被忽略", len(res5) == 0 and ev5.stopped is False)

    ev6 = MockEvent("看 表情")
    res6 = await run_handler(plugin2, ev6)
    chains6 = [r for r in ev6.sent if isinstance(r, tuple) and r[0] == "chain"]
    r.check(
        "关闭斜杠兼容后裸指令仍生效",
        len(res6) == 0 and len(chains6) == 1,
        f"res={res6} sent={ev6.sent}",
    )

    # ---- 4. gall 子指令权限门禁 ----
    plugin3 = make_plugin()
    plugin3.gallery_manager.ensure_loaded()

    ev7 = MockEvent("gall open 新画廊", admin=False)
    res7 = await run_handler(plugin3, ev7)
    r.check(
        "非管理员 open 被拒绝",
        len(res7) == 1 and res7[0][0] == "plain" and "仅限管理员" in res7[0][1],
        f"res={res7}",
    )
    r.check("拒绝后停止传播", ev7.stopped is True)
    r.check("画廊未创建", "新画廊" not in plugin3.gallery_manager.galleries)

    ev8 = MockEvent("/gall open 新画廊", admin=True)
    res8 = await run_handler(plugin3, ev8)
    r.check(
        "管理员 /gall open 创建成功",
        "新画廊" in plugin3.gallery_manager.galleries
        and any("创建成功" in x[1] for x in res8 if x[0] == "plain"),
        f"res={res8}",
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
    r.check(
        "下载图包 返回链接提示",
        len(res11) == 1 and res11[0][0] == "plain" and "分享链接" in res11[0][1],
        f"res={res11}",
    )

    # ---- 7. 错误指令的使用提示 ----
    ev12 = MockEvent("看")
    res12 = await run_handler(plugin3, ev12)
    r.check(
        "看 无参数返回使用方式",
        len(res12) == 1 and "使用方式" in res12[0][1],
    )

    # ---- 7b. 画廊不存在：静默放行，不回复也不拦截 ----
    ev_nf = MockEvent("看 不存在的画廊")
    res_nf = await run_handler(plugin3, ev_nf)
    r.check("看 不存在画廊 不回复", len(res_nf) == 0, f"res={res_nf}")
    r.check("看 不存在画廊 不拦截(放行传播)", ev_nf.stopped is False)

    ev_nf2 = MockEvent("gall close 不存在的画廊", admin=True)
    res_nf2 = await run_handler(plugin3, ev_nf2)
    r.check(
        "gall close 不存在画廊 不回复",
        len(res_nf2) == 0,
        f"res={res_nf2}",
    )
    r.check("原有画廊未受影响", "新画廊" in plugin3.gallery_manager.galleries)

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
    evf2 = MockEvent("上传 合并2")
    evf2.message_obj.message = [Comp.Node(content=[Comp.Image(file="/local/x.gif")])]
    urls2 = await plugin._extract_image_urls(evf2)
    r.check("单个 Node 提取图片", urls2 == ["/local/x.gif"], f"urls={urls2}")

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

    ok2 = r.summary()
    sys.exit(0 if ok and ok2 else 1)


if __name__ == "__main__":
    asyncio.run(main())
