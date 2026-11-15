# -*- coding: utf-8 -*-
"""
WWM - AutoMod detection helpers (pure Python, no Discord imports).

Two jobs:
  1. find_slang(text, words)  -> list of matched slang words
  2. find_links(text)         -> list of hostnames found in the text
     is_whitelisted(host, whitelist) -> bool
"""

import json
import os
import re
import unicodedata

# ── Slang ────────────────────────────────────────────────────────────────────

_ZERO_WIDTH = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD], None
)
_LEET = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
    "@": "a", "$": "s", "!": "i", "|": "i", "+": "t",
})
# "word characters" including the Bengali block, so Bangla words match whole-word
_W = r"[\w\u0980-\u09FF]"
_DEFAULT_SLANG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "default_slang.json",
)


def load_default_slang() -> list:
    """Load the starter slang list from data/default_slang.json."""
    try:
        with open(_DEFAULT_SLANG_FILE, "r", encoding="utf-8") as f:
            return [w.lower() for w in json.load(f).get("words", [])]
    except (OSError, ValueError):
        return []


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    return text.lower()


def _word_pattern(word: str) -> "re.Pattern":
    word = word.strip().lower()
    if len(word) >= 4 and word.isascii():
        # tolerate f.u.c.k / f u c k / fuuuck for longer Latin words
        body = r"[\W_]*".join(re.escape(c) + "+" for c in word)
    else:
        body = re.escape(word)
    return re.compile(r"(?<!" + _W + r")" + body + r"(?!" + _W + r")")


def build_slang_patterns(words) -> list:
    """Compile once, reuse for every message."""
    out = []
    for w in words:
        if w and w.strip():
            out.append((w.strip().lower(), _word_pattern(w)))
    return out


def find_slang(text: str, patterns: list) -> list:
    """Return the list of slang words found in `text` (empty list = clean)."""
    if not text or not patterns:
        return []
    plain = _normalize(text)
    leet = plain.translate(_LEET)
    found = []
    for word, pat in patterns:
        if pat.search(plain) or pat.search(leet):
            found.append(word)
    return found


# ── Links ────────────────────────────────────────────────────────────────────

_TLDS = {
    "com", "net", "org", "io", "gg", "me", "co", "xyz", "info", "biz", "tv",
    "ly", "app", "dev", "gl", "to", "cc", "us", "uk", "bd", "in", "pk", "ru",
    "de", "fr", "jp", "cn", "site", "online", "store", "shop", "club", "live",
    "link", "click", "top", "vip", "fun", "pro", "ai", "gift", "gifts", "ws",
    "tk", "ml", "ga", "cf", "gq", "icu", "cyou", "lol", "wtf", "buzz", "page",
}
_SCHEME_URL = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>\"')\]]+")
_WWW_URL = re.compile(r"(?i)\bwww\.[^\s<>\"')\]]+")
_BARE = re.compile(
    r"(?i)(?<![\w@.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})"
    r"(?::\d{1,5})?(?:[/?#][^\s<>\"')\]]*)?(?![\w-])"
)


def normalize_domain(value: str) -> str:
    """'https://www.Example.com/path' -> 'example.com' (for the whitelist)."""
    v = value.strip().lower()
    v = re.sub(r"^[a-z]+://", "", v)
    v = v.split("/")[0].split("?")[0].split("#")[0]
    v = v.split("@")[-1].split(":")[0].strip(".")
    if v.startswith("www."):
        v = v[4:]
    return v


def _host_from_url(url: str) -> str:
    return normalize_domain(url)


def find_links(text: str) -> list:
    """Return a de-duplicated list of hostnames found in `text`."""
    if not text:
        return []
    t = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    # de-obfuscate  example[.]com  example(.)com
    t = re.sub(r"\s?[\[\(\{]\s?\.\s?[\]\)\}]\s?", ".", t)
    hosts = []
    for m in _SCHEME_URL.finditer(t):
        hosts.append(_host_from_url(m.group(0)))
    for m in _WWW_URL.finditer(t):
        hosts.append(_host_from_url(m.group(0)))
    for m in _BARE.finditer(t):
        host = normalize_domain(m.group(1))
        if host.rsplit(".", 1)[-1] in _TLDS:
            hosts.append(host)
    seen, out = set(), []
    for h in hosts:
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


def is_whitelisted(host: str, whitelist) -> bool:
    """True if host equals a whitelisted domain or is a subdomain of one."""
    host = host.lower()
    for d in whitelist:
        d = normalize_domain(d)
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def blocked_links(text: str, whitelist) -> list:
    """Hostnames in `text` that are NOT whitelisted."""
    return [h for h in find_links(text) if not is_whitelisted(h, whitelist)]
