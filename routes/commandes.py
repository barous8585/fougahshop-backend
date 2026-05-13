"""
security.py — Protections anti-hack pour FougahShop
====================================================
- Rate limiting par IP avec limites différenciées par type de route
- Blocage brute force login
- Headers de sécurité HTTP
"""

import time
import asyncio
from collections import defaultdict
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import os

# ══════════════════════════════════════════════════════════════
# STOCKAGE EN MÉMOIRE
# ══════════════════════════════════════════════════════════════

_request_log: dict = defaultdict(list)
_login_log:   dict = defaultdict(list)
_blocked_ips: dict = {}  # { ip: unblock_timestamp }

# ══════════════════════════════════════════════════════════════
# CONFIGURATION PAR TYPE DE ROUTE
# ══════════════════════════════════════════════════════════════

# 1. Rate général — toutes routes confondues
RATE_LIMIT_REQUESTS = 200
RATE_LIMIT_WINDOW   = 60   # 200 req / 60s par IP

# 2. Création de commande — POST /api/commandes/ uniquement
#    Un client normal en crée 1-2 par session. 5 en 60s = très généreux.
CREATE_COMMANDE_MAX    = 5
CREATE_COMMANDE_WINDOW = 60

# 3. Routes d'écriture sensibles (annulation, vérification promo/parrainage)
WRITE_RATE_MAX    = 15
WRITE_RATE_WINDOW = 60

# 4. Historique — GET /api/commandes/historique/
#    Appelé à la connexion + rafraîchissements. 30/60s suffisant.
HISTORIQUE_MAX    = 30
HISTORIQUE_WINDOW = 60

# 5. Auth — brute force login
LOGIN_MAX_ATTEMPTS  = 5
LOGIN_WINDOW        = 300   # 5 minutes
LOGIN_BLOCK_SECONDS = 1800  # 30 min de blocage

# Routes par catégorie
WRITE_ROUTES = [
    "/api/commandes/annuler",
    "/api/promos/verifier",
    "/api/parrainage/verifier",
    "/api/auth/logout",
]

LOGIN_ROUTES = ["/api/auth/login"]

WHITELIST = ["127.0.0.1", "::1"]

# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def get_client_ip(request: Request) -> str:
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def clean_old(log: list, window: int) -> list:
    now = time.time()
    return [t for t in log if now - t < window]


def is_blocked(ip: str) -> bool:
    if ip in _blocked_ips:
        if time.time() < _blocked_ips[ip]:
            return True
        del _blocked_ips[ip]
    return False


def block_ip(ip: str, duration: int = LOGIN_BLOCK_SECONDS):
    _blocked_ips[ip] = time.time() + duration
    print(f"🚨 IP bloquée: {ip} pour {duration}s")


def rate_check(key: str, max_req: int, window: int) -> bool:
    """Retourne True si la limite est dépassée."""
    _request_log[key] = clean_old(_request_log.get(key, []), window)
    _request_log[key].append(time.time())
    return len(_request_log[key]) > max_req


# ══════════════════════════════════════════════════════════════
# MIDDLEWARE
# ══════════════════════════════════════════════════════════════

class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        ip     = get_client_ip(request)
        path   = request.url.path
        method = request.method

        # OPTIONS → CORS preflight, toujours laisser passer
        if method == "OPTIONS":
            return await call_next(request)

        # Whitelist dev
        if ip in WHITELIST:
            return self._sec(await call_next(request))

        # IP bloquée
        if is_blocked(ip):
            remaining = int(_blocked_ips.get(ip, 0) - time.time())
            return JSONResponse(
                status_code=429,
                content={"detail": f"Trop de tentatives. Réessayez dans {remaining//60} min."},
                headers={"Retry-After": str(remaining)},
            )

        # ── Brute force login ──────────────────────────────────
        if path == "/api/auth/login" and method == "POST":
            _login_log[ip] = clean_old(_login_log[ip], LOGIN_WINDOW)
            _login_log[ip].append(time.time())
            if len(_login_log[ip]) > LOGIN_MAX_ATTEMPTS:
                block_ip(ip)
                print(f"🚨 Brute force login: {ip}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Trop de tentatives. Compte bloqué 30 min."},
                    headers={"Retry-After": str(LOGIN_BLOCK_SECONDS)},
                )

        # ── Création de commande ───────────────────────────────
        # POST /api/commandes/ uniquement (pas /calculer, /suivi, /historique)
        if path == "/api/commandes/" and method == "POST":
            if rate_check(f"cmd_create:{ip}", CREATE_COMMANDE_MAX, CREATE_COMMANDE_WINDOW):
                print(f"⚠️  Rate limit création commande: {ip}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Trop de commandes créées. Réessayez dans une minute."},
                    headers={"Retry-After": "60"},
                )

        # ── Routes d'écriture sensibles ────────────────────────
        elif any(path.startswith(r) for r in WRITE_ROUTES) and method == "POST":
            if rate_check(f"write:{ip}", WRITE_RATE_MAX, WRITE_RATE_WINDOW):
                print(f"⚠️  Rate limit écriture: {ip} sur {path}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Trop de requêtes. Ralentissez."},
                    headers={"Retry-After": "60"},
                )

        # ── Historique (énumération téléphones) ────────────────
        elif path.startswith("/api/commandes/historique/"):
            if rate_check(f"histo:{ip}", HISTORIQUE_MAX, HISTORIQUE_WINDOW):
                print(f"⚠️  Rate limit historique: {ip}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Trop de requêtes. Réessayez dans une minute."},
                    headers={"Retry-After": "60"},
                )

        # ── Rate général ───────────────────────────────────────
        if rate_check(ip, RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW):
            print(f"⚠️  Rate limit général: {ip}")
            return JSONResponse(
                status_code=429,
                content={"detail": "Trop de requêtes. Réessayez dans une minute."},
                headers={"Retry-After": "60"},
            )

        return self._sec(await call_next(request))

    def _sec(self, response):
        response.headers["X-Content-Type-Options"]    = "nosniff"
        response.headers["X-Frame-Options"]           = "DENY"
        response.headers["X-XSS-Protection"]          = "1; mode=block"
        response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]        = "geolocation=(), microphone=(), camera=()"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' https:; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
            "style-src 'self' 'unsafe-inline' https:; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https:; "
            "frame-src https://kkiapay.me https://*.kkiapay.me;"
        )
        return response


# ══════════════════════════════════════════════════════════════
# NETTOYAGE PÉRIODIQUE
# ══════════════════════════════════════════════════════════════

async def cleanup_rate_limits():
    while True:
        await asyncio.sleep(900)  # toutes les 15 min
        now = time.time()
        expired = [ip for ip, t in _blocked_ips.items() if now > t]
        for ip in expired:
            del _blocked_ips[ip]
        for key in list(_request_log.keys()):
            _request_log[key] = clean_old(_request_log[key], RATE_LIMIT_WINDOW)
            if not _request_log[key]:
                del _request_log[key]
        print(f"🧹 Rate limits nettoyés — {len(_blocked_ips)} IPs bloquées")
