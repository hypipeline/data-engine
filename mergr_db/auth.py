"""
Data Engine — access control via Google sign-in (OIDC), restricted to Origination
Network emails.

Design
------
* Authentication  : "Sign in with Google". We only accept Google accounts whose
                    verified email is on ALLOWED_DOMAIN (originationnetwork.com).
                    No passwords are ever handled or stored.
* Authorisation   : two admins (ADMIN_EMAILS) see everything and can manage access;
                    every other user sees ONLY the sections an admin has granted.
* Impersonation   : an admin can "view as" any user for testing. Admin powers are
                    suspended while impersonating; a banner + one click restores them.
* Storage         : grants / audit / seen-users live in the Data Engine's own
                    Postgres (schema `de_access`). The ON database is never written.

Everything is wired onto the FastAPI app by `setup_auth(app, templates)`.
"""
import os
import json
import time
import secrets
import urllib.parse

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

# ---------------------------------------------------------------- config
ALLOWED_DOMAIN = os.environ.get("DE_ALLOWED_DOMAIN", "originationnetwork.com").lower()
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get(
    "DE_ADMIN_EMAILS",
    "craig.anderson@originationnetwork.com,lewis.pomeroy@originationnetwork.com",
).split(",") if e.strip()}

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get(
    "OAUTH_REDIRECT_URI", "https://dataengine.hyndlandpartners.com/auth/callback")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
# Local-only escape hatch: when DE_AUTH_DEV=1 the /login page offers a
# "dev sign-in" that trusts a typed (allowed-domain) email WITHOUT Google.
# Never enable in production.
DEV_MODE = os.environ.get("DE_AUTH_DEV", "").lower() in ("1", "true", "yes")

_G_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_G_TOKEN = "https://oauth2.googleapis.com/token"
_G_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"

DSN = os.environ.get("DATABASE_URL", "postgres://mergr:mergr@127.0.0.1:5433/mergr")

# The gate-able sections. `key` matches the tool prefix used in the URL / base.html.
SECTIONS = [
    {"key": "mergr",             "label": "Mergr",                  "href": "/mergr"},
    {"key": "entity",            "label": "Entity Lookup",          "href": "/entity"},
    {"key": "buyer-match",       "label": "Buyer Match",            "href": "/buyer-match"},
    {"key": "linkedin",          "label": "LinkedIn Finder",        "href": "/linkedin"},
    {"key": "linkedin-profiles", "label": "LinkedIn Profile Finder", "href": "/linkedin-profiles"},
    {"key": "acquirer-finder",   "label": "Acquirer Finder",        "href": "/acquirer-finder"},
    {"key": "pedb",              "label": "PE DB",                  "href": "/pedb"},
]
SECTION_KEYS = [s["key"] for s in SECTIONS]

# Paths that never require a session (auth handshake, health, assets).
_PUBLIC_PREFIXES = ("/login", "/auth/", "/logout", "/health", "/static", "/favicon")


# ---------------------------------------------------------------- db
def _pg():
    return psycopg2.connect(DSN)


def ensure_tables():
    ddl = """
    CREATE SCHEMA IF NOT EXISTS de_access;
    CREATE TABLE IF NOT EXISTS de_access.section_grants (
        email       text        NOT NULL,
        section     text        NOT NULL,
        granted_by  text,
        granted_at  timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (email, section)
    );
    CREATE TABLE IF NOT EXISTS de_access.users (
        email       text PRIMARY KEY,
        name        text,
        first_seen  timestamptz NOT NULL DEFAULT now(),
        last_seen   timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS de_access.audit (
        id      bigserial   PRIMARY KEY,
        ts      timestamptz NOT NULL DEFAULT now(),
        actor   text,
        action  text,
        target  text,
        detail  text
    );
    """
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
    finally:
        conn.close()


def _audit(actor, action, target=None, detail=None):
    try:
        conn = _pg()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO de_access.audit (actor, action, target, detail) "
                "VALUES (%s,%s,%s,%s)", (actor, action, target, detail))
        conn.commit()
        conn.close()
    except Exception:
        pass  # auditing must never break a request


def _remember_user(email, name):
    try:
        conn = _pg()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO de_access.users (email, name) VALUES (%s,%s) "
                "ON CONFLICT (email) DO UPDATE SET name=EXCLUDED.name, last_seen=now()",
                (email, name))
        conn.commit()
        conn.close()
    except Exception:
        pass


def grants_for(email):
    """Set of section keys explicitly granted to this email."""
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT section FROM de_access.section_grants WHERE email=%s",
                        (email.lower(),))
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def set_grant(email, section, on, by):
    email = email.lower()
    conn = _pg()
    try:
        with conn.cursor() as cur:
            if on:
                cur.execute(
                    "INSERT INTO de_access.section_grants (email, section, granted_by) "
                    "VALUES (%s,%s,%s) ON CONFLICT (email, section) DO NOTHING",
                    (email, section, by))
            else:
                cur.execute("DELETE FROM de_access.section_grants WHERE email=%s AND section=%s",
                            (email, section))
        conn.commit()
    finally:
        conn.close()
    _audit(by, "grant" if on else "revoke", email, section)


def all_known_users():
    """Everyone we might want to manage: admins, anyone seen, anyone with a grant."""
    conn = _pg()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT email, name, last_seen FROM de_access.users")
            seen = {r["email"]: r for r in cur.fetchall()}
            cur.execute("SELECT email, section FROM de_access.section_grants")
            grants = {}
            for r in cur.fetchall():
                grants.setdefault(r["email"], set()).add(r["section"])
    finally:
        conn.close()
    emails = set(seen) | set(grants) | set(ADMIN_EMAILS)
    out = []
    for e in sorted(emails, key=lambda x: (x not in ADMIN_EMAILS, x)):  # admins first, then alphabetical
        out.append({
            "email": e,
            "name": (seen.get(e) or {}).get("name"),
            "last_seen": (seen.get(e) or {}).get("last_seen"),
            "is_admin": e in ADMIN_EMAILS,
            "sections": sorted(grants.get(e, set())),
        })
    return out


# ---------------------------------------------------------------- identity helpers
def is_admin(email):
    return bool(email) and email.lower() in ADMIN_EMAILS


def allowed_sections(email):
    """Section keys this identity may see. Admins get all; others get their grants."""
    if is_admin(email):
        return set(SECTION_KEYS)
    return grants_for(email)


def real_user(request: Request):
    """The account that actually signed in (unchanged by impersonation)."""
    return request.session.get("user")


def current_user(request: Request):
    """The EFFECTIVE identity — the impersonated user when impersonating, else the real one."""
    imp = request.session.get("impersonate")
    if imp:
        return {"email": imp, "name": imp.split("@")[0], "is_admin": False, "impersonated": True}
    return real_user(request)


def is_impersonating(request: Request):
    return bool(request.session.get("impersonate")) and bool(real_user(request))


def _email_allowed(email, verified, hd=None):
    if not email or not verified:
        return False
    email = email.lower()
    if hd and hd.lower() != ALLOWED_DOMAIN:
        return False
    return email.endswith("@" + ALLOWED_DOMAIN)


def _login_email(request, email, name):
    email = email.lower()
    request.session["user"] = {"email": email, "name": name, "is_admin": is_admin(email)}
    request.session.pop("impersonate", None)
    _remember_user(email, name)
    _audit(email, "login")


# ---------------------------------------------------------------- template context
def nav_context(request: Request):
    """Values base.html needs: the effective user, impersonation state, visible sections."""
    eff = current_user(request)
    email = (eff or {}).get("email")
    allowed = allowed_sections(email) if email else set()
    return {
        "auth_user": eff,
        "auth_real": real_user(request),
        "auth_impersonating": is_impersonating(request),
        "auth_is_admin": bool(real_user(request) and real_user(request).get("is_admin")),
        "auth_allowed": allowed,
        "auth_sections": SECTIONS,
    }


# ---------------------------------------------------------------- route + middleware wiring
def section_for_path(path):
    if path.startswith("/mergr"):
        return "mergr"
    if path.startswith("/entity"):
        return "entity"
    if path.startswith("/buyer-match"):
        return "buyer-match"
    if path.startswith("/linkedin-profiles") or path.startswith("/lpf"):
        return "linkedin-profiles"
    if path.startswith("/linkedin"):
        return "linkedin"
    if path.startswith("/pedb"):
        return "pedb"
    if path.startswith("/acquirer-finder") or path.startswith("/af/"):
        return "acquirer-finder"
    return None


AUTH_ENABLED = bool(GOOGLE_CLIENT_ID) or DEV_MODE


def setup_auth(app, templates):
    """Attach session middleware, the access guard, and the auth/admin routes.

    FAIL-SAFE: if Google sign-in isn't configured (no GOOGLE_CLIENT_ID) and dev
    mode is off, access control stays DORMANT — the Data Engine runs exactly as
    before (open, behind the Caddy basic_auth front door). It activates
    automatically the moment GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are set on
    the box. This lets the code deploy without risking a prod lock-out.
    """
    import sys
    if not AUTH_ENABLED:
        print("[auth] DISABLED — no GOOGLE_CLIENT_ID and DEV mode off. Data Engine "
              "runs open behind Caddy; set GOOGLE_CLIENT_ID/SECRET to turn on Google "
              "sign-in + section access control.", file=sys.stderr)
        return
    ensure_tables()

    def _wants_html(request):
        return "text/html" in request.headers.get("accept", "")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith(_PUBLIC_PREFIXES):         # handshake / assets / health
            return await call_next(request)

        eff = current_user(request)
        if not eff:                                   # not signed in
            if _wants_html(request):
                return RedirectResponse("/login?next=" + urllib.parse.quote(path), 302)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        # admin-only areas
        if path.startswith("/admin"):
            if not (real_user(request) and real_user(request).get("is_admin")):
                return _forbidden(request, templates, "Admins only.")
            return await call_next(request)

        # section gate ("/" and other hub pages have no section -> always allowed once in)
        section = section_for_path(path)
        if section:
            allowed = allowed_sections(eff.get("email"))
            # Acquirer Finder is surfaced *inside* Buyer Match (its "AI Generate" mode calls
            # /acquirer-finder/*), so a user granted buyer-match can use those endpoints too.
            ok = (section in allowed) or (section == "acquirer-finder" and "buyer-match" in allowed)
            if not ok:
                return _forbidden(request, templates, None, section=section)
        return await call_next(request)

    # SessionMiddleware is added LAST so it sits OUTERMOST and wraps the guard,
    # guaranteeing request.session is populated before the guard reads it.
    app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET,
                       same_site="lax", https_only=False)

    # -------- auth routes
    @app.get("/login", response_class=HTMLResponse)
    def login(request: Request, next: str = "/", err: str = ""):
        if current_user(request):
            return RedirectResponse(next or "/", 302)
        return templates.TemplateResponse(request, "login.html", {
            "dev_mode": DEV_MODE, "domain": ALLOWED_DOMAIN,
            "configured": bool(GOOGLE_CLIENT_ID), "next": next, "err": err,
        })

    @app.get("/auth/start")
    def auth_start(request: Request, next: str = "/"):
        if not GOOGLE_CLIENT_ID:
            return RedirectResponse("/login?err=" + urllib.parse.quote(
                "Google sign-in is not configured (missing GOOGLE_CLIENT_ID)."), 302)
        state = secrets.token_urlsafe(24)
        request.session["oauth_state"] = state
        request.session["oauth_next"] = next or "/"
        params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "hd": ALLOWED_DOMAIN,
            "access_type": "online",
            "prompt": "select_account",
        }
        return RedirectResponse(_G_AUTH + "?" + urllib.parse.urlencode(params), 302)

    @app.get("/auth/callback")
    async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        if error:
            return RedirectResponse("/login?err=" + urllib.parse.quote(error), 302)
        if not code or not state or state != request.session.get("oauth_state"):
            return RedirectResponse("/login?err=" + urllib.parse.quote("Invalid sign-in state."), 302)
        request.session.pop("oauth_state", None)
        nxt = request.session.pop("oauth_next", "/") or "/"
        try:
            async with httpx.AsyncClient(timeout=15) as cx:
                tok = await cx.post(_G_TOKEN, data={
                    "code": code,
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri": REDIRECT_URI,
                    "grant_type": "authorization_code",
                })
                tok.raise_for_status()
                access = tok.json().get("access_token")
                ui = await cx.get(_G_USERINFO, headers={"Authorization": "Bearer " + access})
                ui.raise_for_status()
                info = ui.json()
        except Exception as e:
            return RedirectResponse("/login?err=" + urllib.parse.quote(
                "Google sign-in failed: " + str(e)[:120]), 302)
        email = (info.get("email") or "").lower()
        if not _email_allowed(email, info.get("email_verified"), info.get("hd")):
            _audit(email or "?", "login_denied", detail="domain/verify check failed")
            return RedirectResponse("/login?err=" + urllib.parse.quote(
                "Only @%s accounts may sign in." % ALLOWED_DOMAIN), 302)
        _login_email(request, email, info.get("name") or email.split("@")[0])
        return RedirectResponse(nxt, 302)

    @app.post("/auth/dev")
    def auth_dev(request: Request, email: str = Form(...), next: str = Form("/")):
        if not DEV_MODE:
            return RedirectResponse("/login", 302)
        email = (email or "").strip().lower()
        if not _email_allowed(email, True):
            return RedirectResponse("/login?err=" + urllib.parse.quote(
                "Only @%s accounts may sign in." % ALLOWED_DOMAIN), 302)
        _login_email(request, email, email.split("@")[0])
        return RedirectResponse(next or "/", 302)

    @app.get("/logout")
    def logout(request: Request):
        u = real_user(request)
        if u:
            _audit(u.get("email"), "logout")
        request.session.clear()
        return RedirectResponse("/login", 302)

    # -------- impersonation
    @app.post("/admin/impersonate")
    def impersonate(request: Request, email: str = Form(...)):
        admin = real_user(request)
        if not (admin and admin.get("is_admin")):
            return _forbidden(request, templates, "Admins only.")
        target = (email or "").strip().lower()
        if not target.endswith("@" + ALLOWED_DOMAIN):
            return RedirectResponse("/admin/access?err=bad_email", 302)
        request.session["impersonate"] = target
        _audit(admin.get("email"), "impersonate_start", target)
        return RedirectResponse("/", 302)

    @app.get("/stop-impersonate")
    def stop_impersonate(request: Request):
        admin = real_user(request)
        target = request.session.pop("impersonate", None)
        if admin and target:
            _audit(admin.get("email"), "impersonate_stop", target)
        return RedirectResponse("/admin/access", 302)

    # -------- admin: grant management
    @app.get("/admin/access", response_class=HTMLResponse)
    def admin_access(request: Request, err: str = ""):
        return templates.TemplateResponse(request, "admin_access.html", {
            "users": all_known_users(), "sections": SECTIONS,
            "err": err, **nav_context(request),
        })

    @app.post("/admin/access")
    async def admin_access_save(request: Request):
        admin = real_user(request)
        form = await request.form()
        email = (form.get("email") or "").strip().lower()
        if not email.endswith("@" + ALLOWED_DOMAIN):
            return RedirectResponse("/admin/access?err=bad_email", 302)
        checked = set(form.getlist("sections"))
        for s in SECTION_KEYS:
            set_grant(email, s, s in checked, admin.get("email"))
        return RedirectResponse("/admin/access", 302)


def _forbidden(request, templates, message, section=None):
    """Render the friendly 'no access' screen (403). For XHR/API callers return JSON, not HTML,
    so a fetch()->response.json() gets a clean error instead of choking on '<!doctype ...'."""
    if "text/html" not in request.headers.get("accept", ""):   # XHR/API caller -> JSON, not the HTML page
        return JSONResponse(
            {"error": "forbidden", "section": section,
             "message": message or "You don't have access to this section."},
            status_code=403)
    ctx = {"message": message, "section": section}
    try:
        ctx.update(nav_context(request))
    except Exception:
        pass
    return templates.TemplateResponse(request, "no_access.html", ctx, status_code=403)
