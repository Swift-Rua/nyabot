"""
共享工具 — 噪声过滤、文本清理等。
全项目唯一的噪声判断逻辑，修改一处全局生效。
"""
import re


def clean_text(text: str) -> str:
    """规范化文本：去首尾空白、合并连续空白"""
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text


def is_noise(text: str) -> bool:
    """
    判断一条消息是否为噪声（不应触发 AI）。
    规则统一，ai_gate / context_compressor / event 判断全部复用此函数。
    """
    if not text:
        return True

    t = clean_text(text)

    # 空消息
    if not t:
        return True

    # 太短
    if len(t) <= 1:
        return True

    # 纯符号/表情刷屏
    if re.fullmatch(r"[\W_]+", t):
        return True

    # 重复单字符 (aaaa, !!!!!)
    if re.fullmatch(r"(.)\1{2,}", t):
        return True

    # 常见无意义消息
    noise_words = {
        "1", "哈哈", "哈哈哈", "hhh", "hh", "lol",
        "？", "??", "？？", "...", "。。。",
        "在吗", "在？", "!", "！",
    }
    if t.lower() in noise_words:
        return True

    return False


def short(text: str, max_len: int = 40) -> str:
    """安全截断文本"""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"
