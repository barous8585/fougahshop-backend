from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, Any
import secrets
from database import get_db
from models import Config, Employe

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_MAX_AGE = 7 * 24 * 3600   # 7 jours
COOKIE_NAME    = "fg_admin_session"
SESSION_TTL_DAYS = 7              # Expiration automatique


# ── Migration — à appeler UNE SEULE FOIS au startup ──────────

def ensure_sessions_table(db: Session):
    """Crée la table admin_sessions. À appeler au startup dans main.py."""
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS admin_sessions (
                token      VARCHAR PRIMARY KEY,
                role       VARCHAR NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        db.commit()
    except Exception:
        db.rollback()


def purge_expired_sessions(db: Session):
    """
    ✅ Supprime les sessions expirées (> 7 jours).
    À appeler au startup dans main.py.
    """
    try:
        db.execute(text(
            "DELETE FROM admin_sessions "
            "WHERE created_at < NOW() - INTERVAL ':days days'"
        ).bindparams(days=SESSION_TTL_DAYS))
        db.commit()
    except Exception:
        try:
            # Fallback syntaxe alternative PostgreSQL
            db.execute(text(
                f"DELETE FROM admin_sessions "
                f"WHERE created_at < NOW() - INTERVAL '{SESSION_TTL_DAYS} days'"
            ))
            db.commit()
        except Exception:
            db.rollback()


# ── CRUD sessions ─────────────────────────────────────────────

def session_get(db: Session, token: str) -> str | None:
    """Retourne le rôle associé au token, ou None si expiré/inexistant."""
    if not token:
        return None
    try:
        row = db.execute(
            text(
                "SELECT role FROM admin_sessions "
                "WHERE token = :t "
                f"AND created_at > NOW() - INTERVAL '{SESSION_TTL_DAYS} days'"
            ),
            {"t": token}
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def session_set(db: Session, token: str, role: str):
    try:
        db.execute(
            text("""
                INSERT INTO admin_sessions (token, role)
                VALUES (:t, :r)
                ON CONFLICT (token) DO UPDATE SET role = :r, created_at = NOW()
            """),
            {"t": token, "r": role}
        )
        db.commit()
    except Exception:
        db.rollback()


def session_delete(db: Session, token: str):
    if not token:
        return
    try:
        db.execute(
            text("DELETE FROM admin_sessions WHERE token = :t"),
            {"t": token}
        )
        db.commit()
    except Exception:
        db.rollback()


# ── Helpers ───────────────────────────────────────────────────

def get_config(db: Session):
    cfg = db.query(Config).first()
    if not cfg:
        cfg = Config()
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        key      = COOKIE_NAME,
        value    = token,
        httponly = True,
        secure   = True,
        samesite = "strict",
        max_age  = COOKIE_MAX_AGE,
        path     = "/",
    )


def _clear_session_cookie(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")


def _get_token(request: Request) -> str:
    return (
        request.cookies.get(COOKIE_NAME)
        or request.headers.get("X-Admin-Token")
        or ""
    )


# ── Routes ────────────────────────────────────────────────────

@router.post("/login")
def login(body: Dict[str, Any], response: Response, request: Request, db: Session = Depends(get_db)):
    import time as _time
    password = str(body.get("password", "")).strip()
    if not password:
        raise HTTPException(401, "Mot de passe requis")

    cfg  = get_config(db)
    role = None

    # Vérifier patron
    if cfg.admin_pwd and password == cfg.admin_pwd:
        role = "patron"
    else:
        # Vérifier employé
        emp = db.query(Employe).filter(
            Employe.pwd == password,
            Employe.actif == True
        ).first()
        if emp:
            role = getattr(emp, "role", None) or "employe"
            if role not in ("employe", "logisticien"):
                role = "employe"

    if not role:
        # ✅ Log la tentative échouée
        ip = (request.headers.get("CF-Connecting-IP")
              or request.headers.get("X-Forwarded-For", "").split(",")[0]
              or (request.client.host if request.client else "?"))
        print(f"🚨 Tentative login échouée — IP: {ip} — pwd: {'*'*len(password)}")
        # ✅ Délai anti timing-attack (empêche de deviner si le mdp est proche)
        _time.sleep(0.5)
        raise HTTPException(401, "Mot de passe incorrect")

    token = secrets.token_hex(32)
    session_set(db, token, role)
    _set_session_cookie(response, token)
    return {"ok": True, "role": role, "token": token}


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = _get_token(request)
    session_delete(db, token)
    _clear_session_cookie(response)
    return {"ok": True}


@router.get("/check")
def check(request: Request, db: Session = Depends(get_db)):
    token = _get_token(request)
    role  = session_get(db, token)
    if role:
        return {"authenticated": True, "role": role}
    return {"authenticated": False}


@router.post("/reset")
def reset_password(body: Dict[str, Any], db: Session = Depends(get_db)):
    secret       = str(body.get("secret", "")).strip()
    new_password = str(body.get("new_password", "")).strip()

    if not secret:
        raise HTTPException(400, "Code secret requis")
    if not new_password or len(new_password) < 4:
        raise HTTPException(400, "Mot de passe trop court (minimum 4 caractères)")

    cfg = get_config(db)
    if not cfg.secret_reset or secret != cfg.secret_reset:
        raise HTTPException(403, "Code secret incorrect")

    cfg.admin_pwd = new_password

    # Révoquer toutes les sessions patron — ✅ un seul commit
    try:
        db.execute(text("DELETE FROM admin_sessions WHERE role = 'patron'"))
    except Exception:
        pass

    db.commit()
    return {"ok": True, "message": "Mot de passe mis à jour"}


# ── Dépendances FastAPI ───────────────────────────────────────
# ✅ ensure_sessions_table() RETIRÉ d'ici — appelé au startup dans main.py
# Chaque requête admin fait maintenant 1 seul appel DB au lieu de 2

def require_auth(request: Request, db: Session = Depends(get_db)) -> str:
    token = _get_token(request)
    role  = session_get(db, token)
    if not role:
        raise HTTPException(401, "Non authentifié")
    return role


def require_patron(request: Request, db: Session = Depends(get_db)) -> str:
    role = require_auth(request, db)
    if role != "patron":
        raise HTTPException(403, "Accès réservé au patron")
    return role
