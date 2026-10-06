"""URL canonicalisation: the dedupe key for a posting."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query params that identify the posting itself and must survive canonicalisation.
KEEP_PARAMS = {"gh_jid", "jid", "id", "job", "jobid", "job_id", "reqid", "requisitionid", "p"}
TRACKING_PREFIXES = ("utm_", "mc_", "fbclid", "gclid", "ref", "source", "src", "gh_src", "lever-", "trk", "tracking")


def canonicalize(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    # Postings often end in /apply, /application or /login; the posting is the parent.
    for suffix in ("/apply", "/application", "/login"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)]
    path = path.rstrip("/") or "/"
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() in KEEP_PARAMS or not k.lower().startswith(TRACKING_PREFIXES)
    ]
    query.sort()
    return urlunsplit(("https", host, path, urlencode(query), ""))


def normalize_company(name: str) -> str:
    import re

    n = name.lower().strip()
    n = re.sub(r"[.,&']", " ", n)
    n = re.sub(r"\b(inc|llc|ltd|limited|pvt|private|corp|corporation|co|gmbh|plc|technologies|technology|india)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()
