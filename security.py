"""
security.py — Protections anti-hack pour FougahShop
====================================================
- Rate limiting par IP
- Blocage brute force login
- Headers de sécurité HTTP
- Logging des tentatives suspectes
"""

import time
import asyncio
from collections import defaultdict
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import os

# ══════════════════════════════════════════════════════════════
# STOCKAGE EN MÉMOIRE (réinitialisé au redémarrage — suffisant)
# ══════════════════════════════════════════════════════════════

# { ip: [timestamp, timestamp, ...] }
_request_log:  dict = defaultdict(list)
_login_log:    dict = defaultdict(list)
_blocked_ips:  dict = {}  # { ip: unblock_timestamp }

# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════

# Rate limiting général
RATE_LIMIT_REQUESTS = 200        # max requêtes par fenêtre (augmenté — app SPA fait beaucoup de requêtes)
RATE_LIMIT_WINDOW   = 60         # 60 secondes

# Rate limiting strict (routes sensibles : paiement, création commande)
# ✅ CORRECTION : était 20/30s — trop restrictif pour une SPA
# L'admin seul fait 5-10 requêtes au chargement + polling
# Un client fait 3-5 requêtes par visite
STRICT_RATE_REQUESTS = 60        # 60 requêtes (was 20 — trop bas)
STRICT_RATE_WINDOW   = 60        # sur 60 secondes (was 30s — trop court)

# Brute force login — garder strict
LOGIN_MAX_ATTEMPTS  = 5
LOGIN_WINDOW        = 300        # 5 minutes
LOGIN_BLOCK_SECONDS = 1800       # 30 minutes de blocage

# Routes sensibles — RETIRER /api/commandes/ du strict
# car /suivi/ et /historique/ sont des GET publics très fréquents
# Garder strict seulement sur les routes d'écriture et d'auth
STRICT_ROUTES = [
    "/api/auth/login",
    "/api/auth/logout",
    "/api/promos/verifier",
    "/api/parrainage/verifier",
]

# Routes de création uniquement — limite plus stricte
CREATE_ROUTES = [
    "/api/commandes/annuler",
]

CREATE_RATE_REQUESTS = 10        # max 10 annulations / 60s par IP
CREATE_RATE_WINDOW   = 60

LOGIN_ROUTES = [
    "/api/auth/login",
]

# IPs toujours autorisées (dev)
WHITELIST = ["127.0.0.1", "::1"]

# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def get_client_ip(request: Request) -> str:
    """Récupère la vraie IP (derrière Cloudflare/Render proxy)."""
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def clean_old_entries(log: list, window: int) -> list:
    """Supprimer les entrées trop vieilles."""
    now = time.time()
    return [t for t in log if now - t < window]


def is_blocked(ip: str) -> bool:
    if ip in _blocked_ips:
        if time.time() < _blocked_ips[ip]:
            return True
        else:
            del _blocked_ips[ip]
    return False


def block_ip(ip: str, duration: int = LOGIN_BLOCK_SECONDS):
    _blocked_ips[ip] = time.time() + duration
    print(f"🚨 IP bloquée: {ip} pour {duration}s")


# ══════════════════════════════════════════════════════════════
# MIDDLEWARE PRINCIPAL
# ══════════════════════════════════════════════════════════════

class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        ip = get_client_ip(request)
        path = request.url.path
        method = request.method

        # ── 1. OPTIONS → toujours laisser passer (CORS preflight) ──
        if method == "OPTIONS":
            return await call_next(request)

        # ── 2. Whitelist ──
        if ip in WHITELIST:
            response = await call_next(request)
            return self._add_security_headers(response)

        # ── 3. IP bloquée ──
        if is_blocked(ip):
            remaining = int(_blocked_ips.get(ip, 0) - time.time())
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Trop de tentatives. Réessayez dans {remaining//60} min."
                },
                headers={"Retry-After": str(remaining)},
            )

        # ── 4. Rate limiting brute force login ──
        if any(path.startswith(r) for r in LOGIN_ROUTES) and method == "POST":
            _login_log[ip] = clean_old_entries(_login_log[ip], LOGIN_WINDOW)
            _login_log[ip].append(time.time())
            if len(_login_log[ip]) > LOGIN_MAX_ATTEMPTS:
                block_ip(ip, LOGIN_BLOCK_SECONDS)
                print(f"🚨 Brute force login détecté: {ip}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Trop de tentatives de connexion. Compte bloqué 30 min."},
                    headers={"Retry-After": str(LOGIN_BLOCK_SECONDS)},
                )

        # ── 5. Rate limiting routes de création (annulation, etc.) ──
        if any(path.startswith(r) for r in CREATE_ROUTES) and method == "POST":
            key = f"create:{ip}"
            _request_log[key] = clean_old_entries(
                _request_log.get(key, []), CREATE_RATE_WINDOW
            )
            _request_log[key].append(time.time())
            if len(_request_log[key]) > CREATE_RATE_REQUESTS:
                print(f"⚠️  Rate limit création: {ip} sur {path}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Trop de requêtes. Ralentissez."},
                    headers={"Retry-After": "60"},
                )

        # ── 6. Rate limiting strict (auth, promos, parrainage) ──
        if any(path.startswith(r) for r in STRICT_ROUTES):
            key = f"strict:{ip}"
            _request_log[key] = clean_old_entries(
                _request_log.get(key, []), STRICT_RATE_WINDOW
            )
            _request_log[key].append(time.time())
            if len(_request_log[key]) > STRICT_RATE_REQUESTS:
                print(f"⚠️  Rate limit strict: {ip} sur {path}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Trop de requêtes. Ralentissez."},
                    headers={"Retry-After": "60"},
                )

        # ── 7. Rate limiting général ──
        _request_log[ip] = clean_old_entries(_request_log[ip], RATE_LIMIT_WINDOW)
        _request_log[ip].append(time.time())
        if len(_request_log[ip]) > RATE_LIMIT_REQUESTS * 2:
            _request_log[ip] = _request_log[ip][-RATE_LIMIT_REQUESTS:]
        if len(_request_log[ip]) > RATE_LIMIT_REQUESTS:
            print(f"⚠️  Rate limit général: {ip}")
            return JSONResponse(
                status_code=429,
                content={"detail": "Trop de requêtes. Réessayez dans une minute."},
                headers={"Retry-After": "60"},
            )

        # ── 8. Traiter la requête ──
        response = await call_next(request)
        return self._add_security_headers(response)

    def _add_security_headers(self, response):
        """Ajouter les headers de sécurité HTTP."""
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
# NETTOYAGE PÉRIODIQUE (éviter fuite mémoire)
# ══════════════════════════════════════════════════════════════

async def cleanup_rate_limits():
    """Nettoyer les logs toutes les 15 minutes."""
    while True:
        await asyncio.sleep(900)
        now = time.time()
        expired = [ip for ip, t in _blocked_ips.items() if now > t]
        for ip in expired:
            del _blocked_ips[ip]
        for key in list(_request_log.keys()):
            _request_log[key] = clean_old_entries(_request_log[key], RATE_LIMIT_WINDOW)
            if not _request_log[key]:
                del _request_log[key]
        print(f"🧹 Rate limits nettoyés — {len(_blocked_ips)} IPs bloquées")
