from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from typing import Dict, Any
import secrets
from database import get_db
from models import Config, Employe

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Sessions en mémoire (tokens valides)
# Note: survivent aux requêtes mais pas aux restarts Render.
sessions: dict = {}

COOKIE_MAX_AGE = 7 * 24 * 3600
COOKIE_NAME = "fg_admin_session"

def get_config(db):
    cfg = db.query(Config).first()
    if not cfg:
        cfg = Config()
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg

def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=COOKIE_MAX_AGE,
        path="/",
    )

def _clear_session_cookie(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")

@router.post("/login")
def login(body: Dict[str, Any], response: Response, db: Session = Depends(get_db)):
    password = str(body.get("password", ""))

    if not password:
        raise HTTPException(status_code=401, detail="Mot de passe requis")

    cfg = get_config(db)
    role = None

    if cfg.admin_pwd and password == cfg.admin_pwd:
        role = "patron"
    else:
        emp = db.query(Employe).filter(
            Employe.pwd == password,
            Employe.actif == True
        ).first()
        if emp:
            role = getattr(emp, "role", None) or "employe"
            if role not in ("employe", "logisticien"):
                role = "employe"

    if not role:
        raise HTTPException(status_code=401, detail="Mot de passe incorrect")

    token = secrets.token_hex(32)
    sessions[token] = role

    # Cookie HttpOnly pour les navigateurs
    _set_session_cookie(response, token)

    # ✅ Token retourné aussi dans le JSON pour le frontend localStorage
    return {"ok": True, "role": role, "token": token}

@router.post("/logout")
def logout(request: Request, response: Response):
    token = (
        request.cookies.get(COOKIE_NAME)
        or request.headers.get("X-Admin-Token")
        or ""
    )
    sessions.pop(token, None)
    _clear_session_cookie(response)
    return {"ok": True}

@router.get("/check")
def check(request: Request):
    token = (
        request.cookies.get(COOKIE_NAME)
        or request.headers.get("X-Admin-Token")
        or ""
    )
    role = sessions.get(token)
    if role:
        return {"authenticated": True, "role": role}
    return {"authenticated": False}

@router.post("/reset")
def reset_password(body: Dict[str, Any], db: Session = Depends(get_db)):
    secret       = str(body.get("secret", "")).strip()
    new_password = str(body.get("new_password", "")).strip()

    if not secret:
        raise HTTPException(status_code=400, detail="Code secret requis")
    if not new_password or len(new_password) < 4:
        raise HTTPException(status_code=400, detail="Mot de passe trop court (minimum 4 caractères)")

    cfg = get_config(db)

    if not cfg.secret_reset or secret != cfg.secret_reset:
        raise HTTPException(status_code=403, detail="Code secret incorrect")

    cfg.admin_pwd = new_password
    tokens_a_supprimer = [t for t, r in sessions.items() if r == "patron"]
    for t in tokens_a_supprimer:
        sessions.pop(t, None)

    db.commit()
    return {"ok": True, "message": "Mot de passe mis à jour"}

# ── Dépendances ───────────────────────────────────────────────

def _get_token(request: Request) -> str:
    return (
        request.cookies.get(COOKIE_NAME)
        or request.headers.get("X-Admin-Token")
        or ""
    )

def require_auth(request: Request):
    token = _get_token(request)
    role = sessions.get(token)
    if not role:
        raise HTTPException(status_code=401, detail="Non authentifié")
    return role

def require_patron(request: Request):
    role = require_auth(request)
    if role != "patron":
        raise HTTPException(status_code=403, detail="Accès réservé au patron")
    return role
