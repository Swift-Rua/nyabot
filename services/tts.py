"""
TTS 语音合成 + 语音回复发送。
使用微软 Edge TTS（免费），纯文本模式 + 语速音调参数。
"""
import os
import uuid
import edge_tts
import time

from nonebot import get_bot
from nonebot.adapters.onebot.v11 import MessageSegment

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOICE_DIR = os.path.join(BASE_DIR, "data", "voice")
VOICE_MAX_FILES = 120
VOICE_MAX_DAYS = 2

# ── 语音参数 ──
VOICE = "zh-CN-XiaoxiaoNeural"   # Edge 免费唯一中文女声
RATE = "-12%"                     # 稍慢，温柔宠溺
PITCH = "+18Hz"                   # 高音调，幼态萝莉感


def _cleanup_voice_cache():
    """清理超时/过量的语音文件，避免目录无限增长。"""
    if not os.path.exists(VOICE_DIR):
        return

    now = time.time()
    max_age = VOICE_MAX_DAYS * 24 * 60 * 60
    files = []
    for name in os.listdir(VOICE_DIR):
        if not name.lower().endswith(".mp3"):
            continue
        path = os.path.join(VOICE_DIR, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue

        # 过期则删
        if now - mtime > max_age:
            try:
                os.remove(path)
                continue
            except OSError:
                pass
        files.append((mtime, path))

    # 超过上限时，按时间清理旧文件
    if len(files) > VOICE_MAX_FILES:
        files.sort()  # oldest first
        remove_count = len(files) - VOICE_MAX_FILES
        for _, path in files[:remove_count]:
            try:
                os.remove(path)
            except OSError:
                pass


async def text_to_speech(text: str) -> str | None:
    """
    将文字转为 mp3 语音文件。
    返回文件绝对路径，失败返回 None。
    """
    if not text or not text.strip():
        return None

    _cleanup_voice_cache()
    os.makedirs(VOICE_DIR, exist_ok=True)

    filename = f"{uuid.uuid4().hex[:8]}.mp3"
    filepath = os.path.join(VOICE_DIR, filename)

    try:
        communicate = edge_tts.Communicate(
            text=text.strip(),
            voice=VOICE,
            rate=RATE,
            pitch=PITCH,
        )
        await communicate.save(filepath)

        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            return filepath
        return None
    except Exception as e:
        print(f"[TTS] error: {e}")
        return None


async def send_voice_reply(group_id: int, text: str):
    """
    以语音条形式发送回复（TTS 失败则退化为文字）。
    """
    filepath = await text_to_speech(text)

    bot = get_bot()

    if filepath:
        # file:// URI 格式，正斜杠
        uri = "file:///" + filepath.replace("\\", "/")
        try:
            await bot.send_group_msg(
                group_id=group_id,
                message=MessageSegment.record(file=uri),
            )
            return
        except Exception as e:
            print(f"[TTS] send voice failed, fallback to text: {e}")

    # 退化为文字
    await bot.send_group_msg(
        group_id=group_id,
        message=text,
    )
