"""Shared helpers for legacy source analyzer.

Currently holds URL normalization used by the Java parser endpoint extractor,
the React router scanner, and the DB menu loader so that all three sides
agree on how to join on URL strings.
"""

import re


_PARAM_PATTERNS = [
    re.compile(r"\{[^}]+\}"),   # Spring / menu {id}
    re.compile(r":\w+"),         # React Router v5 :id
    re.compile(r"\*\*"),         # Double wildcard
]


def normalize_url(url: str) -> str:
    """Return a canonical form of ``url`` for cross-source joining.

    * Lowercased
    * Leading ``/`` added
    * Trailing ``/`` stripped (except for the root ``/``)
    * Dynamic path segments replaced with the literal token ``{p}`` so that
      ``/user/{id}``, ``/user/:userId`` and ``/user/{userNo}`` all share one
      key.
    * Collapses ``//`` to ``/`` (defensive for menu tables with sloppy data)
    """
    if not url:
        return ""
    u = url.strip().lower()
    # Collapse protocol+host if someone stored full URL
    u = re.sub(r"^https?://[^/]+", "", u)
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
