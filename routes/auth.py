from fastapi import APIRouter, Depends, HTTPException, Response, Request
from sqlalchemy.orm import Session
from typing import Dict, Any
import secrets
from database import get_db
from models import Config, Employe

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Sessions en mémoire (tokens valides)
sessions: dict = {}

def get_config(db):
    cfg = db.query(Config).first()
    if not cfg:
        cfg = Config()
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg

@router.post("/login")
def login(body: Dict[str, Any], db: Session = Depends(get_db)):
    password = str(body.get("password", ""))
    cfg = get_config(db)
    role = None
    if password == cfg.admin_pwd:
        role = "patron"
    else:
        emp = db.query(Employe).filter(
            Employe.pwd == password,
            Employe.actif == True
        ).first()
        if emp:
            role = "employe"
    if not role:
        raise HTTPException(status_code=401, detail="Mot de passe incorrect")
    token = secrets.token_hex(32)
    sessions[token] = role
    return {"ok": True, "role": role, "token": token}

@router.post("/logout")
def logout(request: Request):
    token = request.headers.get("X-Admin-Token") or ""
    sessions.pop(token, None)
    return {"ok": True}

@router.get("/check")
def check(request: Request):
    token = request.headers.get("X-Admin-Token") or ""
    role = sessions.get(token)
    if role:
        return {"authenticated": True, "role": role}
    return {"authenticated": False}

@router.post("/reset")
def reset_password(body: Dict[str, Any], db: Session = Depends(get_db)):
    secret       = str(body.get("secret", ""))
    new_password = str(body.get("new_password", ""))
    cfg = get_config(db)

    # ✅ Le secret est lu depuis la base de données — plus jamais codé en dur
    if not secret or secret != cfg.secret_reset:
        raise HTTPException(status_code=403, detail="Code secret incorrect")

    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="Mot de passe trop court")

    cfg.admin_pwd = new_password
    db.commit()
    return {"ok": True, "message": "Mot de passe mis à jour"}

# ── Dépendances utilisées par les autres routes ───────────────
def require_auth(request: Request):
    token = request.headers.get("X-Admin-Token") or ""
    role = sessions.get(token)
    if not role:
        raise HTTPException(status_code=401, detail="Non authentifié")
    return role

def require_patron(request: Request):
    role = require_auth(request)
    if role != "patron":
        raise HTTPException(status_code=403, detail="Accès réservé au patron")
    return role
