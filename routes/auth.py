from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, Any
import secrets
from database import get_db
from models import Config, Employe

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_MAX_AGE = 7 * 24 * 3600
COOKIE_NAME    = "fg_admin_session"

# ── Migration : table sessions en base ───────────────────────
def ensure_sessions_table(db):
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS admin_sessions (
                token   VARCHAR PRIMARY KEY,
                role    VARCHAR NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        db.commit()
    except Exception:
        db.rollback()

# ── CRUD sessions (base de données, survivent aux restarts) ──
def session_get(db, token: str) -> str | None:
    """Retourne le rôle associé au token, ou None."""
    try:
        row = db.execute(
            text("SELECT role FROM admin_sessions WHERE token = :t"),
            {"t": token}
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None

def session_set(db, token: str, role: str):
    try:
        db.execute(
            text("""
                INSERT INTO admin_sessions (token, role)
                VALUES (:t, :r)
                ON CONFLICT (token) DO UPDATE SET role = :r
            """),
            {"t": token, "r": role}
        )
        db.commit()
    except Exception:
        db.rollback()

def session_delete(db, token: str):
    try:
        db.execute(
            text("DELETE FROM admin_sessions WHERE token = :t"),
            {"t": token}
        )
        db.commit()
    except Exception:
        db.rollback()

# ── Helpers config & cookie ───────────────────────────────────
def get_config(db):
    cfg = db.query(Config).first()
    if not cfg:
        cfg = Config(); db.add(cfg); db.commit(); db.refresh(cfg)
    return cfg

def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        key=COOKIE_NAME, value=token,
        httponly=True, secure=True, samesite="strict",
        max_age=COOKIE_MAX_AGE, path="/",
    )

def _clear_session_cookie(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")

# ── Routes ────────────────────────────────────────────────────
@router.post("/login")
def login(body: Dict[str, Any], response: Response, db: Session = Depends(get_db)):
    ensure_sessions_table(db)
    password = str(body.get("password", ""))
    if not password:
        raise HTTPException(401, "Mot de passe requis")

    cfg = get_config(db)
    role = None

    if cfg.admin_pwd and password == cfg.admin_pwd:
        role = "patron"
    else:
        emp = db.query(Employe).filter(
            Employe.pwd == password, Employe.actif == True
        ).first()
        if emp:
            role = getattr(emp, "role", None) or "employe"
            if role not in ("employe", "logisticien"):
                role = "employe"

    if not role:
        raise HTTPException(401, "Mot de passe incorrect")

    token = secrets.token_hex(32)
    session_set(db, token, role)

    _set_session_cookie(response, token)
    return {"ok": True, "role": role, "token": token}

@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    ensure_sessions_table(db)
    token = (
        request.cookies.get(COOKIE_NAME)
        or request.headers.get("X-Admin-Token")
        or ""
    )
    session_delete(db, token)
    _clear_session_cookie(response)
    return {"ok": True}

@router.get("/check")
def check(request: Request, db: Session = Depends(get_db)):
    ensure_sessions_table(db)
    token = (
        request.cookies.get(COOKIE_NAME)
        or request.headers.get("X-Admin-Token")
        or ""
    )
    role = session_get(db, token)
    if role:
        return {"authenticated": True, "role": role}
    return {"authenticated": False}

@router.post("/reset")
def reset_password(body: Dict[str, Any], db: Session = Depends(get_db)):
    ensure_sessions_table(db)
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
    # Révoquer toutes les sessions patron
    try:
        db.execute(text("DELETE FROM admin_sessions WHERE role = 'patron'"))
        db.commit()
    except Exception:
        db.rollback()

    db.commit()
    return {"ok": True, "message": "Mot de passe mis à jour"}

# ── Dépendances ───────────────────────────────────────────────
def _get_token(request: Request) -> str:
    return (
        request.cookies.get(COOKIE_NAME)
        or request.headers.get("X-Admin-Token")
        or ""
    )

def require_auth(request: Request, db: Session = Depends(get_db)):
    ensure_sessions_table(db)
    token = _get_token(request)
    role = session_get(db, token)
    if not role:
        raise HTTPException(401, "Non authentifié")
    return role

def require_patron(request: Request, db: Session = Depends(get_db)):
    role = require_auth(request, db)
    if role != "patron":
        raise HTTPException(403, "Accès réservé au patron")
    return role
