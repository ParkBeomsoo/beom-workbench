"""Interface language.

Catalogs (``<lang>.json``) map the Korean source text to a translation. Keys may contain ``{0}``, ``{1}``…
placeholders for the parts that change (numbers, names); those keys become patterns. Korean is the source
language, so it needs no catalog. Agent output and the user's own text are never translated — they are not keys.

Language choice (prefs ``lang``): ``auto`` (default) follows the system language when it is one of
``LANGUAGES``, otherwise Korean; or one of ``LANGUAGES`` explicitly.
"""
from __future__ import annotations

import json
import locale
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

LANGUAGES = {"ko": "한국어", "en": "English", "ja": "日本語", "zh": "简体中文"}
DEFAULT = "ko"
_DIR = Path(__file__).parent
_HANGUL = re.compile(r"[가-힣]")


def system_language() -> str | None:
    """The operating system's interface language as a two-letter code, or None."""
    code = None
    if sys.platform == "win32":
        try:
            import ctypes
            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            code = locale.windows_locale.get(langid)
        except (AttributeError, OSError):
            code = None
    elif sys.platform == "darwin":  # Finder 에서 연 앱에는 LANG 이 없고, 있어도 터미널 설정이다 — 시스템 언어는 AppleLanguages
        try:
            import subprocess
            out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, timeout=5).stdout.decode("utf-8", "replace")
            m = re.search(r'"?([a-zA-Z]{2})[-_"\s,]', out)
            code = m.group(1) if m else None
        except (OSError, subprocess.SubprocessError):
            code = None
    if not code:
        for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
            if os.environ.get(var):
                code = os.environ[var]
                break
    if not code:
        try:
            code = locale.getlocale()[0]
        except ValueError:
            code = None
    code = (code or "").replace("-", "_").split(".")[0].lower()
    return code[:2] or None


def resolve(choice: str | None) -> str:
    """The language to show for a stored choice (``auto`` or a code)."""
    if choice in LANGUAGES:
        return choice
    sys_lang = system_language()
    return sys_lang if sys_lang in LANGUAGES else DEFAULT


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict[str, str]:
    """Korean → translation for ``lang`` (empty for Korean or a missing catalog)."""
    if lang == DEFAULT:
        return {}
    f = _DIR / f"{lang}.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}


# 자리표시자 하나가 맞을 수 있는 글 — 줄·문장 끝('. ' '? ' '! ')·' — ' 구분을 넘지 않는다.
# 짧고 일반적인 항목("{0} 는 {1}")이 여러 문장을 통째로 삼켜 나눠 번역할 기회를 없애지 않게.
_SLOT = r"((?:(?![.?!]\s| — )[^\n])*?)"


@lru_cache(maxsize=None)
def _patterns(lang: str) -> list[tuple[re.Pattern, str]]:
    out = []
    for k, v in catalog(lang).items():
        if "{0}" in k:
            if not _HANGUL.search(re.sub(r"\{\d\}", "", k)):
                continue  # 자리표시자뿐인 항목은 아무 글에나 맞으므로 쓰지 않는다
            rx = re.escape(k)
            for i in range(10):
                rx = rx.replace(re.escape("{%d}" % i), _SLOT)  # 빈 값도 맞게
            out.append((re.compile("^" + rx + "$"), v))  # 자리표시자는 줄을 넘지 않는다 (여러 줄 글은 줄마다 따로)
    out.sort(key=lambda p: -len(p[0].pattern))  # 긴(구체적인) 것부터
    return out


def tr(text: str, lang: str) -> str:
    """Translate one UI string; anything not in the catalog comes back unchanged."""
    if not text or lang == DEFAULT or not _HANGUL.search(text):
        return text
    cat = catalog(lang)
    if text in cat:  # 앞뒤 공백까지 키인 문구 (예: 터미널 질문 끝의 ": ")
        return cat[text]
    core = text.strip()
    if core in cat:
        return text.replace(core, cat[core])
    for rx, v in _patterns(lang):
        m = rx.match(core)
        if m:
            out = v
            for i, g in enumerate(m.groups()):
                out = out.replace("{%d}" % i, tr(g, lang))
            return text.replace(core, out)
    for sep in _SEPS:  # 번역된 조각을 이어 붙인 글 — 줄 → 문장 → ' — ' → ' · ' 순으로 나눠 조각마다 (조각은 다시 통째로 먼저)
        parts = sep.split(core)
        if len(parts) < 3:
            continue
        joined = "".join(p if i % 2 else tr(p, lang) for i, p in enumerate(parts))
        if joined != core:
            return text.replace(core, joined)
    return text


_SEPS = [re.compile(r"(\n)"), re.compile(r"(?<=[.?!)])(\s+)(?=\S)"), re.compile(r"( — )"), re.compile(r"( · )")]
