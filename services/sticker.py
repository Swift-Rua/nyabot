"""
表情包系统 — 检测/收集/发送 QQ 表情和图片。
- 识别群友发的表情并自动收藏
- 支持上下文匹配的表情回复
- 偷表情：群友发的图自动存，以后牛牛喵自己发
"""
import os
import uuid
import json
import time
import random
import re
import hashlib
import aiohttp
from urllib.parse import urlparse

from nonebot.adapters.onebot.v11 import MessageSegment

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STICKER_DIR = os.path.join(BASE_DIR, "data", "stickers")
COLLECTED_DIR = os.path.join(STICKER_DIR, "collected")
NYA_DIR = os.path.join(STICKER_DIR, "nya")        # 手动表情包库
INDEX_FILE = os.path.join(STICKER_DIR, "index.json")
_HTTP_SESSION: aiohttp.ClientSession | None = None
_SAFE_IMAGE_SCHEME = "https"
_SAFE_IMAGE_HOST_SUFFIX = (
    "qpic.cn",
    "qq.com",
)
_SAFE_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_IMAGE_MIN_BYTES = 1 * 1024
_IMAGE_MAX_BYTES = 3 * 1024 * 1024
_IMAGES_PER_EVENT = 2

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://qq.com/",
}
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)


def _get_session() -> aiohttp.ClientSession:
    """复用一个 HTTP session，避免频繁建连。"""
    global _HTTP_SESSION
    if _HTTP_SESSION is None or _HTTP_SESSION.closed:
        _HTTP_SESSION = aiohttp.ClientSession(headers=_HTTP_HEADERS, timeout=_HTTP_TIMEOUT)
    return _HTTP_SESSION


def _is_safe_image_url(url: str) -> bool:
    """Validate image source url is trusted."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if (parsed.scheme or "").lower() != _SAFE_IMAGE_SCHEME:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _SAFE_IMAGE_HOST_SUFFIX)


def _is_safe_content_type(content_type: str) -> bool:
    """Only accept non-svg image content types."""
    if not content_type:
        return False
    base_type = content_type.split(";", 1)[0].strip().lower()
    return base_type.startswith("image/") and base_type != "image/svg+xml"


def _make_content_hash(content: bytes) -> str:
    """根据内容计算 SHA256，避免同图重复写入。"""
    return hashlib.sha256(content).hexdigest()


def _is_duplicate_sticker(data: dict, url: str, content_hash: str) -> bool:
    """判断是否重复：同 URL 或同内容哈希。"""
    for item in data.get("stickers", []):
        if item.get("url") == url:
            return True
        if content_hash and item.get("sha256") == content_hash:
            return True
    return False

# ── QQ 表情 ID → 情绪含义 ──
FACE_MAP: dict[int, str] = {
    0: "惊讶", 1: "撇嘴", 2: "色", 3: "发呆", 4: "得意",
    12: "偷笑", 14: "可爱", 15: "白眼", 16: "傲慢",
    18: "坏笑", 21: "酷", 27: "呲牙", 28: "害羞",
    39: "调皮", 50: "抠鼻", 59: "笑哭", 63: "打脸",
    66: "爱心", 74: "赞", 76: "强", 79: "弱",
    85: "哭", 96: "尴尬", 101: "惊吓", 104: "委屈",
    105: "可怜", 106: "菜狗", 109: "吃瓜", 111: "社会",
    112: "耶", 114: "抱拳", 116: "握手", 123: "好的",
    124: "ok", 173: "笑哭2", 174: "点赞", 175: "吃瓜2",
    176: "狗头", 177: "玫瑰", 178: "爱心2",
    183: "笑哭3", 212: "暗中观察", 240: "打call",
    262: "裂开", 263: "让我看看", 264: "叹气",
    265: "苦涩", 266: "旺柴", 277: "打脸2",
    281: "叹气2", 282: "恐惧", 311: "翻白眼",
}

# 情绪 → 推荐 QQ 表情 ID
MOOD_FACES: dict[str, list[int]] = {
    "happy": [14, 27, 39, 112, 240],       # 可爱/呲牙/调皮/耶/打call
    "sad": [5, 85, 264, 265, 104],          # 哭/叹气/委屈
    "shy": [28, 104, 263],                   # 害羞/委屈/让我看看
    "roast": [15, 50, 63, 176, 311],        # 白眼/抠鼻/打脸/狗头/翻白眼
    "surprised": [0, 101, 262, 282],        # 惊讶/惊吓/裂开/恐惧
    "love": [66, 177, 178],                  # 爱心/玫瑰
    "cool": [16, 21, 111],                   # 傲慢/酷/社会
    "funny": [12, 18, 59, 173, 183, 109],   # 偷笑/坏笑/笑哭/吃瓜
}


def _ensure_dirs():
    os.makedirs(COLLECTED_DIR, exist_ok=True)


def load_index() -> dict:
    if not os.path.exists(INDEX_FILE):
        return {"stickers": []}
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"stickers": []}


def save_index(data: dict):
    _ensure_dirs()
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════
# 检测消息中的表情/图片
# ═══════════════════════════════════════════

def detect(event) -> dict:
    """
    检测 OneBot 事件中的表情和图片。
    返回: {"faces": [id,...], "images": [{"url":..., "file":...},...], "has_sticker": bool}
    """
    result = {"faces": [], "images": [], "has_sticker": False}

    for seg in event.get_message():
        if seg.type == "face":
            fid = int(seg.data.get("id", 0))
            result["faces"].append(fid)
            result["has_sticker"] = True
        elif seg.type == "image":
            result["images"].append({
                "url": seg.data.get("url", ""),
                "file": seg.data.get("file", ""),
            })
            result["has_sticker"] = True

    return result


def face_to_text(face_ids: list[int]) -> str:
    """将 QQ 表情 ID 列表转为文字描述"""
    names = [FACE_MAP.get(f, f"表情{f}") for f in face_ids]
    return "、".join(names) if names else ""


# ═══════════════════════════════════════════
# 收集群友表情包
# ═══════════════════════════════════════════

async def collect_from_event(event, sender_name: str):
    """从事件中收集图片表情，下载保存到本地库"""
    info = detect(event)
    if not info["images"]:
        return
    _ensure_dirs()
    session = _get_session()
    data = load_index()

    for img in info["images"][:_IMAGES_PER_EVENT]:
        url = img.get("url", "")
        if not url:
            print(f"[sticker] no url in image: {img.get('file','?')[:20]}")
            continue
        if not _is_safe_image_url(url):
            print(f"[sticker] rejected image url: {url[:60]}")
            continue

        try:
            # 根据 URL 或 Content-Type 推断扩展名
            ext = ".gif"
            if ".jpg" in url.lower() or "jpeg" in url.lower():
                ext = ".jpg"
            elif ".png" in url.lower():
                ext = ".png"

            filename = f"{uuid.uuid4().hex[:8]}{ext}"
            filepath = os.path.join(COLLECTED_DIR, filename)

            async with session.get(url) as resp:
                ct = resp.headers.get("Content-Type", "")
                if not _is_safe_content_type(ct):
                    print(f"[sticker] rejected content-type: {ct} for {url[:60]}...")
                    continue

                cl = resp.headers.get("Content-Length")
                if cl:
                    try:
                        clen = int(cl)
                        if not (_IMAGE_MIN_BYTES <= clen <= _IMAGE_MAX_BYTES):
                            print(f"[sticker] skipped by Content-Length: {cl}B for {url[:60]}...")
                            continue
                    except ValueError:
                        pass

                if resp.status == 200:
                    content = await resp.read()
                    content_hash = _make_content_hash(content)
                    if _is_duplicate_sticker(data, url, content_hash):
                        print(f"[sticker] skipped duplicate: {url[:60]}...")
                        continue
                    # 根据实际 Content-Type 修正扩展名
                    ct = resp.headers.get("Content-Type", "")
                    if "jpeg" in ct or "jpg" in ct:
                        ext = ".jpg"
                    elif "png" in ct:
                        ext = ".png"
                    elif "webp" in ct:
                        ext = ".webp"
                    if not filename.endswith(ext):
                        filename = filename.rsplit(".", 1)[0] + ext
                        filepath = os.path.join(COLLECTED_DIR, filename)

                    if _IMAGE_MIN_BYTES <= len(content) <= _IMAGE_MAX_BYTES and ext in _SAFE_IMAGE_EXTS:
                        with open(filepath, "wb") as f:
                            f.write(content)

                        data["stickers"].append({
                            "file": filename,
                            "source_user": sender_name,
                            "collected_at": time.strftime("%m-%d %H:%M"),
                            "url": url,
                            "sha256": content_hash,
                        })
                        if len(data["stickers"]) > 500:
                            data["stickers"] = data["stickers"][-500:]
                        save_index(data)
                        print(f"[sticker] collected from {sender_name}: {filename} ({len(content)}B)")
                    else:
                        print(f"[sticker] skipped {filename}: size={len(content)}B")
                else:
                    print(f"[sticker] download failed: HTTP {resp.status} for {url[:60]}...")
        except Exception as e:
            print(f"[sticker] error: {type(e).__name__}: {e}")


# ═══════════════════════════════════════════
# 发送表情
# ═══════════════════════════════════════════

def random_face(mood: str | None = None) -> MessageSegment:
    """随机选一个 QQ 表情，可按情绪筛选"""
    if mood and mood in MOOD_FACES:
        pool = MOOD_FACES[mood]
    else:
        pool = [14, 27, 39, 12, 18, 59, 109, 176]  # 通用可爱/搞笑
    fid = random.choice(pool)
    return MessageSegment.face(fid)


def _scan_nya_dir() -> list[str]:
    """扫描手动表情包库，返回文件路径列表"""
    _ensure_dirs()
    if not os.path.exists(NYA_DIR):
        return []
    files = []
    for f in os.listdir(NYA_DIR):
        if f.lower().endswith((".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp")):
            files.append(os.path.join(NYA_DIR, f))
    return files


def random_sticker(nya_only: bool = False) -> MessageSegment | None:
    """
    随机选一张表情包。
    nya_only=True 仅从手动库选，否则从收集+手动库混合选。
    """
    # 收集库
    collected = []
    if not nya_only:
        data = load_index()
        for s in data.get("stickers", []):
            fp = os.path.join(COLLECTED_DIR, s["file"])
            if os.path.exists(fp):
                collected.append(fp)

    # 手动库
    nya_files = _scan_nya_dir()

    all_files = collected + nya_files if not nya_only else nya_files
    if not all_files:
        return None

    filepath = random.choice(all_files)
    uri = "file:///" + filepath.replace("\\", "/")
    return MessageSegment.image(file=uri)


def reply_with_sticker() -> tuple[MessageSegment, str]:
    """
    回复用表情：手动库优先(60%) → 收集库(20%) → QQ 表情(20%)。
    返回 (MessageSegment, 文字描述用于 AI 上下文)。
    """
    r = random.random()

    # 60% 手动表情包库
    if r < 0.6:
        seg = random_sticker(nya_only=True)
        if seg:
            return seg, "[表情包]"

    # 20% 收集的群友表情
    if r < 0.8:
        seg = random_sticker()
        if seg:
            return seg, "[群友表情包]"

    # 20% 兜底 QQ 表情
    face = random_face()
    fid = face.data.get("id", 14)
    return face, f"[QQ表情:{FACE_MAP.get(int(fid), '可爱')}]"


# ═══════════════════════════════════════════
# 回复消息中的表情
# ═══════════════════════════════════════════

def reply_to_sticker(event) -> MessageSegment | None:
    """
    如果对方发了表情包，用另一个表情包回击。
    """
    info = detect(event)
    if not info["has_sticker"]:
        return None

    # 对方发 QQ 表情 → 回 QQ 表情
    if info["faces"]:
        # 根据对方表情的情绪，回同风格
        fid = info["faces"][0]
        mood = None
        for m, ids in MOOD_FACES.items():
            if fid in ids:
                mood = m
                break
        return random_face(mood)

    # 对方发图片表情 → 回手动库表情（有的话），否则回收集的
    seg = random_sticker(nya_only=True)
    return seg if seg else random_sticker()
