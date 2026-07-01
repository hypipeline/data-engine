#!/usr/bin/env python3
"""
Normalize a website URL or bare domain to a canonical domain for matching.

Rules: lowercase; strip scheme, userinfo, port, path/query/fragment, and a
leading 'www.'. Multi-part TLDs (.co.uk, .com.br, .bc.ca) are preserved.
Reducing further to the registrable domain (eTLD+1) would need a public-suffix
list; the stored data is overwhelmingly 'http://www.<domain>', so www-stripping
canonicalizes ~all of it.
"""
from urllib.parse import urlparse


def website_to_domain(s):
    if not s:
        return None
    s = s.strip().lower()
    if not s:
        return None
    if "://" not in s:
        s = "http://" + s
    host = urlparse(s).netloc
    if not host:
        host = urlparse(s).path.split("/")[0]
    host = host.split("@")[-1]     # drop userinfo
    host = host.split(":")[0]      # drop port
    while host.startswith("www."):
        host = host[4:]
    return host or None
