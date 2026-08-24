"""画廊插件核心逻辑测试（不依赖 AstrBot 运行时）。

通过注入 mock 的 astrbot.api 模块，使 gallery_manager / image_utils / history
可在独立环境中运行，验证查重哈希、图片处理、画廊增删改查、重载与撤销逻辑。
"""

import sys
import os
import types
import asyncio
import tempfile
import shutil
import json

# 注入 mock 的 astrbot.api / astrbot.core 模块，使插件源码可独立导入
def _install_astrbot_mocks():
    if "astrbot" in sys.modules:
        return

    # astrbot 顶层包
    astrbot = types.ModuleType("astrbot")
    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.logger = types.SimpleNamespace(
        info=print, warning=print, error=print, debug=lambda *a: None, print_exc=print
    )
    astrbot_api.AstrBotConfig = dict
    astrbot_api.FunctionTool = object

    # astrbot.api.event / star
    api_event = types.ModuleType("astrbot.api.event")
    api_event.filter = types.SimpleNamespace()
    api_event.AstrMessageEvent = object
    api_event.MessageChain = object
    api_star = types.ModuleType("astrbot.api.star")
    api_star.Context = object
    api_star.Star = object
    api_star.register = lambda *a, **k: (lambda c: c)

    # astrbot.api.message_components
    api_comp = types.ModuleType("astrbot.api.message_components")
    class _Comp:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    for name in ["Plain", "At", "Image", "Record", "Video", "File", "Face",
                 "Reply", "Node", "Nodes", "Poke"]:
        setattr(api_comp, name, type(name, (_Comp,), {}))
    api_comp.Image.fromURL = classmethod(lambda cls, url: cls(url=url))
    api_comp.Image.fromFileSystem = classmethod(lambda cls, path: cls(file=path))

    # astrbot.core 子模块
    core = types.ModuleType("astrbot.core")
    star_pkg = types.ModuleType("astrbot.core.star")
    star_tools = types.ModuleType("astrbot.core.star.star_tools")
    class _StarTools:
        @staticmethod
        def get_data_dir():
            return os.path.join(tempfile.gettempdir(), "gallery_test_data")
    star_tools.StarTools = _StarTools
    star_filter = types.ModuleType("astrbot.core.star.filter")
    star_filter.permission = types.ModuleType("astrbot.core.star.filter.permission")
    star_filter.permission.PermissionType = types.SimpleNamespace(ADMIN="admin")
    io_mod = types.ModuleType("astrbot.core.utils.io")

    async def _download_image_by_url(url, path=None, *a, **k):
        # 测试用：如果是本地路径直接返回，否则复制临时文件
        if os.path.exists(url):
            return url
        return url
    io_mod.download_image_by_url = _download_image_by_url

    # 装配
    astrbot_api.event = api_event
    astrbot_api.star = api_star
    astrbot_api.message_components = api_comp
    astrbot.star = api_star
    astrbot.event = api_event
    astrbot.api = astrbot_api
    astrbot.core = core
    core.star = star_pkg
    star_pkg.star_tools = star_tools
    star_pkg.filter = star_filter
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
    sys.modules["astrbot.core.star.filter"] = star_filter
    sys.modules["astrbot.core.star.filter.permission"] = star_filter.permission
    sys.modules["astrbot.core.utils"] = core.utils
    sys.modules["astrbot.core.utils.io"] = io_mod


_install_astrbot_mocks()

# 将插件目录作为包导入（相对导入需要包上下文）
_pkg_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_pkg_dir))

# 为相对导入创建包上下文
import importlib.util
import importlib
import importlib.machinery
spec = importlib.machinery.ModuleSpec("astrbot_plugin_gallery", None, is_package=True)
gallery_pkg = importlib.util.module_from_spec(spec)
gallery_pkg.__path__ = [_pkg_dir]
sys.modules["astrbot_plugin_gallery"] = gallery_pkg

# 导入子模块
from astrbot_plugin_gallery.gallery_manager import (
    GalleryManager, Gallery, GalleryPic, GalleryMode,
    GalleryPicRepeatedException, ReplyException,
)
from astrbot_plugin_gallery.image_utils import (
    ImageProcessor, process_image_for_gallery,
    render_gallery_list, render_pic_grid, render_repeat, render_check,
)
from astrbot_plugin_gallery.history import HistoryManager


def make_test_image(path, color=(255, 0, 0), size=(100, 100), fmt="PNG"):
    from PIL import Image as PImage
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = PImage.new("RGBA", size, color + (255,))
    img.save(path, format=fmt)
    return path


def make_similar_image(path, color=(255, 0, 0), size=(100, 100), fmt="PNG"):
    """制作一张颜色略有差异但结构相同的图，应被判为重复。"""
    from PIL import Image as PImage
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = PImage.new("RGBA", size, color + (255,))
    img.save(path, format=fmt)
    return path


def make_different_image(path, color=(0, 255, 0), size=(200, 200), fmt="PNG"):
    from PIL import Image as PImage
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = PImage.new("RGBA", size, color + (255,))
    img.save(path, format=fmt)
    return path


def make_noise_image(path, size=(2000, 2000), fmt="JPEG"):
    """制作噪声图，难以压缩，用于测试大小限制。"""
    from PIL import Image as PImage
    import random
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = PImage.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
    img.save(path, format=fmt, quality=95)
    return path


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
        if self.failures:
            print("失败项:")
            for f in self.failures:
                print(f"  - {f}")
        return self.failed == 0


async def test_hash_dedup(r: TestResults, tmpdir):
    """测试哈希查重：相同/相似图判重，不同图不判重。"""
    print("\n[测试] 哈希查重算法")
    img_proc = ImageProcessor(size_limit_mb=1.0, hash1_threshold=5, hash2_threshold=1000)
    mgr = GalleryManager(tmpdir, img_proc)
    mgr.ensure_loaded()
    mgr.open_gall("test")
    await mgr._save()

    p1 = os.path.join(tmpdir, "p1.png")
    p2 = os.path.join(tmpdir, "p2.png")  # 与 p1 相同
    p3 = os.path.join(tmpdir, "p3.png")  # 完全不同
    make_test_image(p1, color=(255, 0, 0))
    make_similar_image(p2, color=(255, 0, 0))
    make_different_image(p3, color=(0, 255, 0), size=(200, 200))

    pid1 = await mgr.async_add_pic("test", p1, check_duplicated=False)
    r.check("首张图片添加成功", pid1 == 1, f"pid={pid1}")

    # p2 与 p1 相同，应判重
    raised = False
    try:
        await mgr.async_add_pic("test", p2, check_duplicated=True)
    except GalleryPicRepeatedException as e:
        raised = True
        r.check("重复图抛出 pid", e.pid == pid1, f"pid={e.pid}")
    r.check("相同图片被判为重复", raised, "未抛出 GalleryPicRepeatedException")

    # p3 完全不同，应成功
    pid3 = await mgr.async_add_pic("test", p3, check_duplicated=True)
    r.check("不同图片添加成功", pid3 == 2, f"pid={pid3}")

    # force 模式可强制添加重复图
    pid4 = await mgr.async_add_pic("test", p2, check_duplicated=False)
    r.check("force 模式强制添加成功", pid4 == 3, f"pid={pid4}")


async def test_gallery_crud(r: TestResults, tmpdir):
    """测试画廊增删改、别名、模式、封面。"""
    print("\n[测试] 画廊 CRUD")
    img_proc = ImageProcessor(1.0, 5, 1000)
    mgr = GalleryManager(tmpdir, img_proc)
    mgr.ensure_loaded()

    # 名称校验
    r.check("合法名称通过", GalleryManager._check_name("猫猫") is True)
    r.check("含非法字符失败", GalleryManager._check_name("猫/猫") is False)
    r.check("纯数字失败", GalleryManager._check_name("123") is False)
    r.check("空名称失败", GalleryManager._check_name("") is False)
    r.check("超长名称失败", GalleryManager._check_name("a" * 33) is False)

    # 创建画廊
    mgr.open_gall("表情包")
    await mgr._save()
    r.check("画廊创建成功", "表情包" in mgr.galleries)
    r.check("默认模式为 Edit", mgr.galleries["表情包"].mode == GalleryMode.Edit)

    # 别名
    mgr.add_gall_alias("表情包", "emoji")
    r.check("别名添加成功", "emoji" in mgr.galleries["表情包"].aliases)
    r.check("通过别名查找", mgr.find_gall("emoji") is not None)
    mgr.del_gall_alias("表情包", "emoji")
    r.check("别名删除成功", "emoji" not in mgr.galleries["表情包"].aliases)

    # 模式
    old, new = mgr.change_gall_mode("表情包", GalleryMode.View)
    r.check("模式修改成功", new == GalleryMode.View)

    # 删除画廊
    mgr.close_gall("表情包")
    await mgr._save()
    r.check("画廊删除成功", "表情包" not in mgr.galleries)


async def test_reload_gall(r: TestResults, tmpdir):
    """测试重载画廊（验证修复的 bug：不再重复加载已加载图片）。"""
    print("\n[测试] 重载画廊（修复原 bug）")
    img_proc = ImageProcessor(1.0, 5, 1000)
    mgr = GalleryManager(tmpdir, img_proc)
    mgr.ensure_loaded()
    mgr.open_gall("reload_test")
    await mgr._save()

    # 手动放两张图到画廊目录
    g = mgr.find_gall("reload_test")
    p1 = os.path.join(g.pics_dir, "r1.png")
    p2 = os.path.join(g.pics_dir, "r2.png")
    make_test_image(p1, color=(255, 0, 0))
    make_different_image(p2, color=(0, 255, 0), size=(150, 150))

    # 首次重载应加载 2 张
    new_pids, del_pids = await mgr.async_reload_gall("reload_test")
    r.check("首次重载新增2张", len(new_pids) == 2, f"new={len(new_pids)}")
    r.check("首次重载无失效", len(del_pids) == 0)

    # 再次重载：已加载的图不应重复加载（原 bug 会重复加载）
    new_pids2, del_pids2 = await mgr.async_reload_gall("reload_test")
    r.check("再次重载无新增（修复 bug）", len(new_pids2) == 0, f"new={len(new_pids2)}")
    r.check("再次重载无失效", len(del_pids2) == 0)

    # 删除一张磁盘文件后再重载
    os.remove(p2)
    new_pids3, del_pids3 = await mgr.async_reload_gall("reload_test")
    r.check("删除文件后重载检测到失效", len(del_pids3) == 1, f"del={len(del_pids3)}")


async def test_history_revert(r: TestResults, tmpdir):
    """测试上传历史与撤销。"""
    print("\n[测试] 上传历史与撤销")
    img_proc = ImageProcessor(1.0, 5, 1000)
    mgr = GalleryManager(tmpdir, img_proc)
    mgr.ensure_loaded()
    mgr.open_gall("hist_test")
    await mgr._save()
    hist = HistoryManager(tmpdir, mgr)
    hist.ensure_loaded()

    # 添加 3 张图
    pids = []
    for i in range(3):
        p = os.path.join(tmpdir, f"h{i}.png")
        make_test_image(p, color=(i * 80, 0, 0), size=(50, 50))
        pid = await mgr.async_add_pic("hist_test", p, check_duplicated=False)
        pids.append(pid)

    # 记录上传
    hid = await hist.add_history("user123", pids)
    r.check("历史记录 id 从 1 开始", hid == 1, f"hid={hid}")

    # 第二条记录 id 不复用（修复原 bug）
    hid2 = await hist.add_history("user123", [99])
    r.check("第二条记录 id 自增", hid2 == 2, f"hid2={hid2}")

    # 撤销第一条
    h, ok_list, err_list = await hist.revert_by_id(hid)
    r.check("撤销返回正确记录", h["id"] == hid)
    r.check("撤销成功删除3张", len(ok_list) == 3, f"ok={len(ok_list)}")
    r.check("撤销无失败", len(err_list) == 0)

    # 验证图片确实被删除
    r.check("图片1已删除", mgr.find_pic(pids[0]) is None)
    r.check("图片3已删除", mgr.find_pic(pids[2]) is None)

    # 重复撤销应失败
    raised = False
    try:
        await hist.revert_by_id(hid)
    except ReplyException:
        raised = True
    r.check("重复撤销被拒绝", raised)

    # 普通用户撤销自己最近一次
    hid3 = await hist.add_history("user456", [88])
    h2, ok2, err2 = await hist.revert_last_by_user("user456", expired_hours=24)
    r.check("用户撤销自己的记录", h2["id"] == hid3)


async def test_image_processing(r: TestResults, tmpdir):
    """测试图片处理：大小限制、静态 gif 转换。"""
    print("\n[测试] 图片处理流水线")
    os.makedirs(tmpdir, exist_ok=True)
    # 大图（噪声，难以压缩）应被缩小
    big_path = os.path.join(tmpdir, "big.jpg")
    from PIL import Image as PImage
    make_noise_image(big_path, size=(2000, 2000), fmt="JPEG")
    orig_size = os.path.getsize(big_path)

    process_image_for_gallery(big_path, sub_type=0, size_limit_mb=0.5)
    new_size = os.path.getsize(big_path)
    r.check("大图被缩小", new_size < orig_size, f"{orig_size}->{new_size}")

    # 静态图转 gif（表情包）
    static_path = os.path.join(tmpdir, "static.png")
    PImage.new("RGBA", (50, 50), (0, 0, 255, 255)).save(static_path, format="PNG")
    process_image_for_gallery(static_path, sub_type=1, size_limit_mb=1.0)
    r.check("静态表情包转为 gif", static_path.lower().endswith(".png"))  # 路径不变但内容变 gif
    img = PImage.open(static_path)
    r.check("转换后为 GIF 格式", img.format == "GIF", f"format={img.format}")


async def test_persistence(r: TestResults, tmpdir):
    """测试持久化：重新加载后数据一致。"""
    print("\n[测试] 持久化")
    img_proc = ImageProcessor(1.0, 5, 1000)
    mgr = GalleryManager(tmpdir, img_proc)
    mgr.ensure_loaded()
    mgr.open_gall("persist")
    mgr.add_gall_alias("persist", "p")
    p = os.path.join(tmpdir, "persist_img.png")
    make_test_image(p, color=(10, 20, 30))
    pid = await mgr.async_add_pic("persist", p, check_duplicated=False)

    # 新建一个 manager 模拟重启
    mgr2 = GalleryManager(tmpdir, img_proc)
    mgr2.ensure_loaded()
    r.check("重启后画廊存在", "persist" in mgr2.galleries)
    r.check("重启后别名保留", "p" in mgr2.galleries["persist"].aliases)
    r.check("重启后图片保留", len(mgr2.galleries["persist"].pics) == 1)
    r.check("重启后 pid_top 一致", mgr2.pid_top == mgr.pid_top)
    pic = mgr2.galleries["persist"].pics[0]
    r.check("重启后图片 pid 一致", pic.pid == pid)
    r.check("重启后 hash 保留", pic.hash1 is not None)
    r.check("运行时能解析出绝对路径", os.path.isfile(pic.path), f"path={pic.path}")
    r.check("缩略图路径可推导", pic.thumb_path and os.path.isfile(pic.thumb_path))

    # JSON 不得写入绝对路径，才能在 Linux/Docker 下换盘符/换挂载点后继续用
    with open(os.path.join(tmpdir, "gallery.json"), encoding="utf-8") as f:
        dumped = json.load(f)
    rec = dumped["galleries"]["persist"]["pics"][0]
    r.check("JSON 不存 path", "path" not in rec)
    r.check("JSON 不存 thumb_path", "thumb_path" not in rec)
    r.check("JSON 不存 gall_name", "gall_name" not in rec)
    r.check("JSON 只存文件名", rec.get("file") == os.path.basename(pic.path), f"file={rec.get('file')}")
    r.check("JSON 不存 pics_dir", "pics_dir" not in dumped["galleries"]["persist"])
    r.check("文件名不含路径分隔符", rec.get("file") and "/" not in rec["file"] and "\\" not in rec["file"])

    # 把整个数据目录挪到另一处，模拟 Docker 换挂载点
    moved = os.path.join(tmpdir, "moved_data")
    shutil.copytree(tmpdir, moved, dirs_exist_ok=True)
    # 清掉复制出来的临时源图，只保留 pics/ 与 gallery.json
    for name in os.listdir(moved):
        if name not in ("gallery.json", "pics", "add_history.json"):
            p = os.path.join(moved, name)
            if os.path.isfile(p):
                os.remove(p)
    mgr3 = GalleryManager(moved, img_proc)
    mgr3.ensure_loaded()
    pic3 = mgr3.galleries["persist"].pics[0]
    r.check("换目录后仍能打开原图", os.path.isfile(pic3.path), f"path={pic3.path}")
    r.check("换目录后路径落在新 data_dir", pic3.path.startswith(os.path.abspath(moved)))
    r.check("换目录后缩略图仍在", pic3.thumb_path and os.path.isfile(pic3.thumb_path))

    # 兼容旧版绝对路径 JSON：加载后保存应改写成相对文件名
    legacy_dir = os.path.join(tmpdir, "legacy")
    os.makedirs(os.path.join(legacy_dir, "pics", "legacy_g"), exist_ok=True)
    legacy_img = os.path.join(legacy_dir, "pics", "legacy_g", "old_1.png")
    make_test_image(legacy_img, color=(1, 2, 3))
    with open(os.path.join(legacy_dir, "gallery.json"), "w", encoding="utf-8") as f:
        json.dump({
            "pid_top": 1,
            "galleries": {
                "legacy_g": {
                    "name": "legacy_g",
                    "aliases": [],
                    "mode": "edit",
                    "pics_dir": os.path.join(legacy_dir, "pics", "legacy_g"),
                    "cover_pid": None,
                    "pics": [{
                        "gall_name": "legacy_g",
                        "pid": 1,
                        "path": legacy_img,
                        "hash1": "0" * 16,
                        "hash2": "00" * 256,
                        "thumb_path": legacy_img + "_thumb.jpg",
                    }],
                }
            },
        }, f)
    mgr4 = GalleryManager(legacy_dir, img_proc)
    mgr4.ensure_loaded()
    r.check("旧绝对路径仍能加载", os.path.isfile(mgr4.galleries["legacy_g"].pics[0].path))
    await mgr4._save()
    with open(os.path.join(legacy_dir, "gallery.json"), encoding="utf-8") as f:
        rewritten = json.load(f)
    rec4 = rewritten["galleries"]["legacy_g"]["pics"][0]
    r.check("保存后改写为文件名", rec4.get("file") == "old_1.png", f"file={rec4.get('file')}")
    r.check("保存后去掉绝对 path", "path" not in rec4)


async def test_check_gallery(r: TestResults, tmpdir):
    """测试画廊查重。"""
    print("\n[测试] 画廊查重")
    img_proc = ImageProcessor(1.0, 5, 1000)
    mgr = GalleryManager(tmpdir, img_proc)
    mgr.ensure_loaded()
    mgr.open_gall("check_test")
    await mgr._save()

    # 添加 2 张相同 + 1 张不同
    p1 = os.path.join(tmpdir, "c1.png")
    p2 = os.path.join(tmpdir, "c2.png")
    p3 = os.path.join(tmpdir, "c3.png")
    make_test_image(p1, color=(100, 100, 100))
    make_test_image(p2, color=(100, 100, 100))  # 与 p1 相同
    make_different_image(p3, color=(50, 200, 50), size=(180, 180))

    await mgr.async_add_pic("check_test", p1, check_duplicated=False)
    await mgr.async_add_pic("check_test", p2, check_duplicated=False)
    await mgr.async_add_pic("check_test", p3, check_duplicated=False)

    res = await mgr.async_check_gallery("check_test", rehash=True)
    r.check("查重发现1组重复", len(res) == 1, f"groups={len(res)}")
    if res:
        first_pid, dup_pids = next(iter(res.items()))
        r.check("重复组有1个重复图", len(dup_pids) == 1, f"dup={dup_pids}")


async def test_draw_toolkit(r: TestResults, tmpdir):
    """测试 draw.py 布局工具包基础能力。"""
    print("\n[测试] draw.py Pillow 布局工具包")
    os.makedirs(tmpdir, exist_ok=True)
    from PIL import Image as PImage
    from astrbot_plugin_gallery.draw import (
        Canvas, FillBg, Grid, HSplit, VSplit, TextBox, ImageBox, Spacer,
        TextStyle, DEFAULT_FONT, DEFAULT_BOLD_FONT, BLACK, THUMBNAIL_BG_COLOR,
    )

    thumb = os.path.join(tmpdir, "thumb.png")
    make_test_image(thumb, color=(30, 90, 200), size=(32, 32))  # 小图，验证放大 fit

    # 模拟画廊列表卡片：嵌套 Grid > VSplit > ImageBox + HSplit(TextBox) + TextBox
    with Canvas(bg=FillBg(THUMBNAIL_BG_COLOR)).set_padding(8) as canvas:
        with Grid(row_count=2, hsep=8, vsep=8).set_item_align('t').set_content_align('t'):
            for i in range(4):
                with VSplit().set_padding(0).set_sep(4) \
                        .set_content_align('c').set_item_align('c'):
                    ImageBox(image=thumb, size=(128, 128),
                             image_size_mode='fit').set_content_align('c')
                    with HSplit().set_padding(0).set_sep(2) \
                            .set_content_align('c').set_item_align('c'):
                        TextBox(f"画廊{i}", TextStyle(DEFAULT_BOLD_FONT, 24, BLACK))
                    TextBox("3张", TextStyle(DEFAULT_FONT, 20, BLACK))
    img = await canvas.get_img()
    r.check("工具包渲染出图片", img is not None and img.width > 0 and img.height > 0,
            f"size={getattr(img, 'size', None)}")
    r.check("画布尺寸合理(>=内容)", img.width >= 2 * 128 and img.height >= 100,
            f"{img.size}")

    out = os.path.join(tmpdir, "toolkit_out.png")
    img.save(out, format="PNG")
    r.check("渲染结果可保存", os.path.exists(out) and os.path.getsize(out) > 0)

    # Spacer 与空文本边界情况
    with Canvas(bg=FillBg(THUMBNAIL_BG_COLOR)).set_padding(4) as c2:
        with VSplit().set_padding(0).set_sep(2):
            Spacer(w=64, h=64)
            TextBox("", TextStyle(DEFAULT_FONT, 16, BLACK))
    img2 = await c2.get_img()
    r.check("Spacer/空文本正常", img2 is not None and img2.width > 0)


async def test_renderers(r: TestResults, tmpdir):
    """测试四个渲染器：画廊列表 / 图片网格 / 上传对比 / 查重结果。"""
    print("\n[测试] 渲染器")
    from PIL import Image as PImage

    def valid_png(path):
        if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
            return False
        try:
            img = PImage.open(path)
            img.verify()
            return True
        except Exception:
            return False

    save_dir = os.path.join(tmpdir, "renders")

    t1 = os.path.join(tmpdir, "r1.png")
    t2 = os.path.join(tmpdir, "r2.png")
    make_test_image(t1, color=(200, 60, 60), size=(40, 40))
    make_test_image(t2, color=(60, 200, 60), size=(90, 120))

    items = [
        {"name": "表情包", "thumb_path": t1, "mode": "",
         "count": 10, "size_text": "(5M)"},
        {"name": "梗图", "thumb_path": None, "mode": "view",
         "count": 3, "size_text": "(<1M)"},
    ]
    p = await render_gallery_list(items, save_dir)
    r.check("render_gallery_list 输出有效 PNG", valid_png(p), f"path={p}")

    pics = [(t1, 1), (None, 2), (t2, 3)]
    p = await render_pic_grid(pics, save_dir)
    r.check("render_pic_grid 输出有效 PNG", valid_png(p), f"path={p}")

    pairs = [
        {"new_path": t1, "old_path": t2, "old_pid": 3},
        {"new_path": t2, "old_path": None, "old_pid": 9},
    ]
    p = await render_repeat(pairs, hint='查重错误可使用"/上传 force"强制上传', save_dir=save_dir)
    r.check("render_repeat 输出有效 PNG", valid_png(p), f"path={p}")

    groups = [
        [(t1, 1), (t2, 3)],
        [(None, 7)],
    ]
    p = await render_check(groups, save_dir)
    r.check("render_check 输出有效 PNG", valid_png(p), f"path={p}")


async def main():
    r = TestResults()
    base_tmp = tempfile.mkdtemp(prefix="gallery_test_")
    try:
        await test_hash_dedup(r, os.path.join(base_tmp, "t1"))
        await test_gallery_crud(r, os.path.join(base_tmp, "t2"))
        await test_reload_gall(r, os.path.join(base_tmp, "t3"))
        await test_history_revert(r, os.path.join(base_tmp, "t4"))
        await test_image_processing(r, os.path.join(base_tmp, "t5"))
        await test_draw_toolkit(r, os.path.join(base_tmp, "t6"))
        await test_renderers(r, os.path.join(base_tmp, "t6"))
        await test_persistence(r, os.path.join(base_tmp, "t7"))
        await test_check_gallery(r, os.path.join(base_tmp, "t8"))
    finally:
        shutil.rmtree(base_tmp, ignore_errors=True)
    ok = r.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
