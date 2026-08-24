# astrbot_plugin_gallery

群内图片画廊插件，移植自 lunabot 的画廊服务。支持在群组中上传、查看、查重、管理表情包/梗图。

## 功能特性

- **画廊管理**：创建/删除画廊、设置别名、切换模式（编辑/只读/关闭）、设置封面
- **图片上传**：回复图片即可上传，自动查重（两级哈希），支持 `force` 强制上传
- **图片查看**：随机看、指定数量、按 pid 看、倒数第 N 张；多图以单条合并消息发送（不经分段回复拆分，非合并转发）
- **图片查重**：感知哈希（粗筛）+ 像素 MAE（精筛）两级算法，可重新计算 hash
- **图片管理**：批量删除（含连续范围）、替换、重载（从磁盘扫描）、查重
- **上传历史**：记录每次上传，支持撤销自己的上传（24h 时效）、管理员撤销任意记录
- **图包下载**：打包成 zip 上传群文件、获取分享链接
- **百度网盘同步**：定时通过 bypy 上传画廊到百度网盘
- **指令兼容**：兼容 `/` 前缀，也允许不带 `/` 的裸指令（如直接发 `看 表情包`）

## 安装

将本插件放入 `data/plugins/astrbot_plugin_gallery/`，安装依赖：

```
pip install pillow numpy aiohttp
```

如需百度网盘同步，还需在服务器安装并授权 `bypy`。

## 数据存储

所有持久化数据存放于 `data/plugin_data/astrbot_plugin_gallery/`：

- `gallery.json`：画廊元数据（画廊列表、别名、模式、封面、每张图的 pid/hash/文件名）。不存绝对路径，整目录拷到 Linux/Docker 即可用
- `add_history.json`：上传历史记录
- `add.log`：上传日志（纯文本）
- `pics/{画廊名}/`：实际图片文件 + `{原文件名}_thumb.jpg` 缩略图

运行时路径由 `data_dir/pics/{画廊名}/{file}` 拼出。旧版带绝对路径的 `gallery.json` 仍能加载，保存时会自动改写成文件名。

## 指令一览

### 普通用户
| 指令 | 说明 |
| --- | --- |
| `看 画廊名` / `看画廊名` / `看 画廊名 x2` / `看 画廊名 -1` | 随机/指定数量/倒数第 N 张（中文指令后可以不空格） |
| `看 123 456` / `看 -1` | 按 pid 看图 |
| `看所有` / `看全部` | 查看所有画廊列表 |
| `看所有 画廊名` | 查看指定画廊图片网格 |
| `上传 画廊名` / `添加 画廊名` | 上传（回复图片），加 `force` 禁用查重 |
| `取消上传` / `撤销上传` | 撤销自己最近一次上传 |
| `上传记录 记录ID` | 查看上传记录 |
| `下载图包` | 获取图包分享链接 |

### 管理员
| 指令 | 说明 |
| --- | --- |
| `gall open 画廊名` | 新建画廊 |
| `gall close 画廊名` | 删除画廊 |
| `gall mode 画廊名 [edit/view/off]` | 查看/设置模式 |
| `gall cover 画廊名 图片ID` | 设置封面 |
| `gall alias add 画廊名 别名` | 添加别名 |
| `gall alias del 画廊名 别名` | 删除别名 |
| `gall del 123 456` / `gall del 100-119` | 批量删除（最多连续 20 张） |
| `gall replace pid` | 替换图片（回复图），支持 `force` |
| `gall reload 画廊名` | 从磁盘重新加载 |
| `gall check 画廊名 [rehash]` / `gall check all` | 查重 |
| `gall log pid` | 查询上传日志 |
| `gall download 画廊名` | 打包上传群文件（仅群聊） |
| `取消上传 记录ID` | 撤销指定上传记录 |

## 配置

在 WebUI 插件管理处可配置：

- `size_limit_mb`：单图大小上限（MB），超限自动缩小
- `pick_limit`：一次 `/看` 最多图片数
- `hash1_difference_threshold` / `hash2_difference_threshold`：查重阈值
- `user_recent_revert_expired_hours`：用户撤销上传的时效（小时）
- `enable_slash_prefix`：是否兼容 `/` 前缀（开启时 `/看` 与 `看` 都生效）
- `sync.*`：百度网盘同步设置

## 相对原版的修复

移植时修复了原 lunabot 画廊代码的若干问题：

1. **`async_reload_gall` 重复加载**：原 `continue` 只跳过内层循环导致所有图片被重复加载，改用路径集合判断
2. **`gall replace` force 失效**：原 `args.remove('force')` 在字符串上调用列表方法，改用 `replace`
3. **`%-S` 时间格式跨平台**：改用 `%S`，避免 Windows 下文件名异常
4. **历史记录 id 复用**：原用 `len(history)+1`，清理后 id 会回退，改用自增计数器 `next_id`
5. **查重闭包结构错误**：重构时修复了 `is_same` 传入列表而非首图的 bug
6. **并发安全**：所有写操作加 `asyncio.Lock`，防止 pid 重复与数据库竞争
7. **原子写入**：数据库写临时文件后 `os.replace`，避免崩溃损坏

## 测试

```bash
python test_core.py
```

覆盖查重哈希、画廊 CRUD、重载、历史撤销、图片处理、持久化、查重等核心逻辑（不依赖 AstrBot 运行时）。
