"""Shared helpers for legacy source analyzer.

Currently holds URL normalization used by the Java parser endpoint extractor,
the React router scanner, and the DB menu loader so that all three sides
agree on how to join on URL strings.
"""

import logging
import re

logger = logging.getLogger(__name__)


_PARAM_PATTERNS = [
    re.compile(r"\{[^}]+\}"),   # Spring / menu {id}
    re.compile(r":\w+"),         # React Router v5 :id
    re.compile(r"\*\*"),         # Double wildcard
]


_STRIP_CACHE: dict[tuple, list] = {}


def _compile_strip_patterns(strip_patterns):
    """Compile a list of regex strings once and cache by tuple identity.

    Bad regexes (``re.error``) are dropped with a Korean warning so a
    single malformed pattern can't poison the whole run.
    """
    if not strip_patterns:
        return []
    key = tuple(strip_patterns)
    cached = _STRIP_CACHE.get(key)
    if cached is not None:
        return cached
    compiled = []
    for pat in strip_patterns:
        if not pat:
            continue
        try:
            compiled.append(re.compile(pat))
        except re.error as e:
            logger.warning("url_prefix_strip 정규식 무효 (%r): %s — 건너뜀", pat, e)
    _STRIP_CACHE[key] = compiled
    return compiled


def normalize_url(url: str, strip_patterns=None) -> str:
    """Return a canonical form of ``url`` for cross-source joining.

    * Lowercased
    * Leading ``/`` added
    * Trailing ``/`` stripped (except for the root ``/``)
    * Dynamic path segments replaced with the literal token ``{p}`` so that
      ``/user/{id}``, ``/user/:userId`` and ``/user/{userNo}`` all share one
      key.
    * Collapses ``//`` to ``/`` (defensive for menu tables with sloppy data)

    ``strip_patterns`` (optional): list of regex strings that are removed
    from the URL *after* protocol+host strip but *before* param-token
    replacement. Used to peel app prefixes like ``^/apps/[^/]+`` or API
    version prefixes like ``^/api/v\\d+`` so different conventions across
    repos collapse onto the same key. ``None`` or empty keeps the legacy
    behaviour unchanged.
    """
    if not url:
        return ""
    u = url.strip().lower()
    # Collapse protocol+host if someone stored full URL
    u = re.sub(r"^https?://[^/]+", "", u)
    # Strip caller-supplied prefix patterns (app prefix, API version, etc.)
    for pat in _compile_strip_patterns(strip_patterns):
        u = pat.sub("", u)
    # Collapse multiple slashes
    u = re.sub(r"/+", "/", u)
    # Replace dynamic params
    for pat in _PARAM_PATTERNS:
        u = pat.sub("{p}", u)
    # Single wildcard ``*`` but only as a whole segment
    u = re.sub(r"(?<=/)\*(?=/|$)", "{p}", u)
    if not u.startswith("/"):
        u = "/" + u
    if len(u) > 1 and u.endswith("/"):
        u = u[:-1]
    return u
