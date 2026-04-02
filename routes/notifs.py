from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, Any, Optional
from database import get_db
from routes.auth import require_auth

router = APIRouter(prefix="/api/notifs", tags=["notifs"])

def ensure_tokens_table(db):
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS fcm_tokens (
                id SERIAL PRIMARY KEY,
                token VARCHAR UNIQUE NOT NULL,
                role VARCHAR DEFAULT 'client',
                ref VARCHAR,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """))
        db.commit()
    except Exception:
        db.rollback()

@router.post("/register")
def register_token(body: Dict[str, Any], db: Session = Depends(get_db)):
    ensure_tokens_table(db)
    token = str(body.get("token", "")).strip()
    role = str(body.get("role", "client"))
    ref = body.get("ref")
    if not token:
        raise HTTPException(400, "Token manquant")
    try:
        db.execute(text("""
            INSERT INTO fcm_tokens (token, role, ref, updated_at)
            VALUES (:token, :role, :ref, NOW())
            ON CONFLICT (token) DO UPDATE SET role=:role, ref=:ref, updated_at=NOW()
        """), {"token": token, "role": role, "ref": ref})
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Erreur enregistrement: {e}")
    return {"ok": True}

@router.post("/send")
def send_notification(
    body: Dict[str, Any],
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_auth)
):
    return {"ok": False, "message": "Notifications push désactivées"}

def _send_fcm(token: str, title: str, body: str, ref: Optional[str] = None) -> bool:
    return False

def notifier_patron(db, title: str, body: str, ref: Optional[str] = None):
    pass

def notifier_client(db, ref: str, title: str, body: str):
    pass
