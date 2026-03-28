from fastapi import APIRouter, Depends, HTTPException, Response, Request
from sqlalchemy.orm import Session
from typing import Dict, Any
import secrets
from database import get_db
from models import Config, Employe

router = APIRouter(prefix="/api/auth", tags=["auth"])
sessions: dict = {}

def get_config(db):
    cfg = db.query(Config).first()
    if not cfg:
        cfg = Config(); db.add(cfg); db.commit(); db.refresh(cfg)
    return cfg

@router.post("/login")
def login(body: Dict[str, Any], response: Response, db: Session = Depends(get_db)):
    password = str(body.get("password", ""))
    cfg = get_config(db)
    role = None
    if password == cfg.admin_pwd:
        role = "patron"
    else:
        emp = db.query(Employe).filter(Employe.pwd == password, Employe.actif == True).first()
        if emp:
            role = "employe"
    if not role:
        raise HTTPException(status_code=401, detail="Mot de passe incorrect")
    token = secrets.token_hex(32)
    sessions[token] = role
    response.set_cookie("admin_token", token, httponly=True, max_age=86400*7, samesite="lax")
    return {"ok": True, "role": role}

@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("admin_token")
    sessions.pop(token, None)
    response.delete_cookie("admin_token")
    return {"ok": True}

@router.get("/check")
def check(request: Request):
    token = request.cookies.get("admin_token")
    role = sessions.get(token)
    if role:
        return {"authenticated": True, "role": role}
    return {"authenticated": False}

def require_auth(request: Request):
    token = request.cookies.get("admin_token")
    role = sessions.get(token)
    if not role:
        raise HTTPException(status_code=401, detail="Non authentifié")
    return role

def require_patron(request: Request):
    role = require_auth(request)
    if role != "patron":
        raise HTTPException(status_code=403, detail="Accès réservé au patron")
    return role
