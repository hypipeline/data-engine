"""
Free / public email-domain filter for buyer↔Mergr email-domain matching.

We match buyers to Mergr firms on shared CORPORATE email domains (a strong identity signal).
To avoid mass false positives we must exclude free/public webmail + disposable providers —
otherwise every buyer and firm with a gmail.com contact would collapse together.

FREE_EMAIL_DOMAINS is the canonical denylist (source of truth). It seeds a Postgres table
(buyer_match.free_email_domains) + a `buyer_match.is_free_email_domain(text)` function via
ensure_schema(), so the SQL linker and the Python sync sampling share the exact same list.

Base list: the ~top-100 popular email domains from
https://email-verify.my-addr.com/list-of-most-popular-email-domains.php
plus modern webmail (proton/zoho/icloud/…) and common disposable providers.
"""
from __future__ import annotations

import re

# Top-100 popular email domains (my-addr.com list)
_POPULAR = """
gmail.com yahoo.com hotmail.com aol.com hotmail.co.uk hotmail.fr msn.com yahoo.fr wanadoo.fr
orange.fr comcast.net yahoo.co.uk yahoo.com.br yahoo.co.in live.com rediffmail.com free.fr
gmx.de web.de yandex.ru ymail.com libero.it outlook.com uol.com.br bol.com.br mail.ru cox.net
hotmail.it sbcglobal.net sfr.fr live.fr verizon.net live.co.uk googlemail.com yahoo.es ig.com.br
live.nl bigpond.com terra.com.br yahoo.it neuf.fr yahoo.de alice.it rocketmail.com att.net
laposte.net facebook.com bellsouth.net yahoo.in hotmail.es charter.net yahoo.ca yahoo.com.au
rambler.ru hotmail.de tiscali.it shaw.ca yahoo.co.jp sky.com earthlink.net optonline.net
freenet.de t-online.de aliceadsl.fr virgilio.it home.nl qq.com telenet.be me.com yahoo.com.ar
tiscali.co.uk yahoo.com.mx voila.fr gmx.net mail.com planet.nl tin.it live.it ntlworld.com
arcor.de yahoo.co.id frontiernet.net hetnet.nl live.com.au yahoo.com.sg zonnet.nl club-internet.fr
juno.com optusnet.com.au blueyonder.co.uk bluewin.ch skynet.be sympatico.ca windstream.net mac.com
centurytel.net chello.nl live.ca aim.com bigpond.net.au
"""

# Modern webmail + regional providers not in the 2012-era list
_MODERN = """
icloud.com protonmail.com proton.me pm.me zoho.com zohomail.com gmx.com gmx.co.uk gmx.at
fastmail.com fastmail.fm hushmail.com tutanota.com tuta.io tutanota.de yandex.com ya.ru
inbox.ru list.ru bk.ru internet.ru 163.com 126.com yeah.net sina.com sina.cn sohu.com
naver.com hanmail.net daum.net nate.com seznam.cz email.cz o2.pl wp.pl interia.pl onet.pl
op.pl gazeta.pl live.de outlook.fr outlook.de outlook.es outlook.it outlook.com.au hey.com
btinternet.com talktalk.net ntlworld.co.uk virginmedia.com googlemail.co.uk yahoo.com.hk
yahoo.com.tw yahoo.com.ph yahoo.com.vn ukr.net i.ua meta.ua abv.bg mynet.com hotmail.com.br
outlook.pt live.se live.dk live.no telia.com telus.net rogers.com bt.com bigpond.com.au
"""

# Common disposable / throwaway providers
_DISPOSABLE = """
mailinator.com guerrillamail.com 10minutemail.com yopmail.com temp-mail.org tempmail.com
throwaway.email trashmail.com getnada.com sharklasers.com maildrop.cc mohmal.com dispostable.com
fakeinbox.com mailnesia.com spamgourmet.com
"""

FREE_EMAIL_DOMAINS = frozenset(
    d for chunk in (_POPULAR, _MODERN, _DISPOSABLE) for d in chunk.split()
)


def email_domain(email: str) -> str:
    """Normalized domain from an email (or a bare domain). '' if unparseable."""
    s = (email or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"^.*@", "", s)          # strip local-part if present
    s = s.strip().strip(".").split()[0] if s else ""   # trim stray whitespace/dots
    # must look like a domain (has a dot, only sane chars)
    if "." not in s or not re.match(r"^[a-z0-9.-]+$", s):
        return ""
    return s


def is_free_email_domain(value: str) -> bool:
    """True if the email/domain belongs to a free/public/disposable provider."""
    return email_domain(value) in FREE_EMAIL_DOMAINS


def corporate_domains(emails) -> list[str]:
    """Distinct corporate (non-free, valid) email domains from a list of emails, sorted."""
    out = set()
    for e in emails or []:
        d = email_domain(e)
        if d and d not in FREE_EMAIL_DOMAINS:
            out.add(d)
    return sorted(out)


# ── SQL mirror: table + function so the linker uses the identical list ─────────
def ensure_schema(conn) -> None:
    """Create buyer_match.free_email_domains (+ is_free_email_domain function) and sync the
    canonical set into it. Idempotent — safe to re-run; keeps the table == the Python truth."""
    doms = sorted(FREE_EMAIL_DOMAINS)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS buyer_match.free_email_domains (domain text PRIMARY KEY);
            CREATE OR REPLACE FUNCTION buyer_match.is_free_email_domain(p text)
            RETURNS boolean LANGUAGE sql STABLE AS $fn$
                SELECT EXISTS (
                    SELECT 1 FROM buyer_match.free_email_domains
                    WHERE domain = lower(btrim(regexp_replace(coalesce(p,''), '^.*@', ''))));
            $fn$;
        """)
        # replace contents with the canonical set (delete rows no longer in the list, upsert rest)
        cur.execute("DELETE FROM buyer_match.free_email_domains WHERE domain <> ALL(%s)", (doms,))
        psyco_execute_values(cur,
            "INSERT INTO buyer_match.free_email_domains (domain) VALUES %s ON CONFLICT DO NOTHING",
            [(d,) for d in doms])
    conn.commit()


def psyco_execute_values(cur, sql, rows):
    try:
        from psycopg2.extras import execute_values
        execute_values(cur, sql, rows)
    except Exception:  # noqa: BLE001 — fallback for tiny sets / non-psycopg2
        for r in rows:
            cur.execute(sql.replace("%s", "(%s)"), r)
