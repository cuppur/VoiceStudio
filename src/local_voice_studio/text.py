from __future__ import annotations

import re


_SENTENCE_END = re.compile(r"(?<=[。！？!?；;…])")


def split_text(text: str, max_chars: int = 120) -> list[str]:
    """Split Chinese/English mixed text without losing punctuation."""
    text = re.sub(r"\r\n?", "\n", text).strip()
    if not text:
        return []
    candidates: list[str] = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        candidates.extend(part.strip() for part in _SENTENCE_END.split(paragraph) if part.strip())

    result: list[str] = []
    current = ""
    for sentence in candidates:
        if len(sentence) > max_chars:
            if current:
                result.append(current)
                current = ""
            result.extend(_hard_split(sentence, max_chars))
        elif current and len(current) + len(sentence) > max_chars:
            result.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        result.append(current)
    return result


def _hard_split(text: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    while len(text) > max_chars:
        window = text[:max_chars]
        split_at = max(window.rfind(mark) for mark in ("，", ",", "、", " "))
        if split_at < max_chars // 2:
            split_at = max_chars
        else:
            split_at += 1
        parts.append(text[:split_at])
        text = text[split_at:]
    if text:
        parts.append(text)
    return parts
