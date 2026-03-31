from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, Any, Optional
from database import get_db
from routes.auth import require_auth
import json
import os

router = APIRouter(prefix="/api/notifs", tags=["notifs"])

# ── Initialiser Firebase Admin ────────────────────────────────
_firebase_initialized = False

def init_firebase():
    global _firebase_initialized
    if _firebase_initialized:
        return True
    try:
        import firebase_admin
        from firebase_admin import credentials
        # Chercher la clé dans les variables d'environnement ou le fichier
        service_account_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
        if service_account_json:
            sa_dict = json.loads(service_account_json)
            cred = credentials.Certificate(sa_dict)
        else:
            cred = credentials.Certificate("firebase-service-account.json")
        firebase_admin.initialize_app(cred)
        _firebase_initialized = True
        print("✅ Firebase Admin initialisé")
        return True
    except Exception as e:
        print(f"⚠️ Firebase Admin non initialisé: {e}")
        return False

def ensure_tokens_table(db):
    """Créer la table FCM tokens si elle n'existe pas"""
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

# ── Enregistrer un token FCM ──────────────────────────────────
@router.post("/register")
def register_token(body: Dict[str, Any], db: Session = Depends(get_db)):
    ensure_tokens_table(db)
    token = str(body.get("token", "")).strip()
    role = str(body.get("role", "client"))  # "client" ou "patron"
    ref = body.get("ref")  # référence commande pour le client
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

# ── Envoyer une notification (utilisé par admin) ──────────────
@router.post("/send")
def send_notification(
    body: Dict[str, Any],
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_auth)
):
    token = str(body.get("token", ""))
    title = str(body.get("title", "FougahShop"))
    message = str(body.get("message", ""))
    ref = body.get("ref")
    if not token or not message:
        raise HTTPException(400, "Token et message requis")
    ok = _send_fcm(token, title, message, ref)
    return {"ok": ok}

# ── Fonction interne : envoyer une notif FCM ──────────────────
def _send_fcm(token: str, title: str, body: str, ref: Optional[str] = None) -> bool:
    if not init_firebase():
        return False
    try:
        from firebase_admin import messaging
        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={"ref": ref or ""},
            token=token,
            android=messaging.AndroidConfig(priority="high"),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound="default", badge=1)
                )
            ),
            webpush=messaging.WebpushConfig(
                notification=messaging.WebpushNotification(
                    title=title, body=body,
                    icon="https://fougahshop.netlify.app/icon-192.png"
                )
            )
        )
        messaging.send(msg)
        return True
    except Exception as e:
        print(f"⚠️ Erreur FCM: {e}")
        return False

def notifier_patron(db, title: str, body: str, ref: Optional[str] = None):
    """Envoyer une notification à tous les tokens patron"""
    ensure_tokens_table(db)
    try:
        tokens = db.execute(text(
            "SELECT token FROM fcm_tokens WHERE role='patron'"
        )).fetchall()
        for t in tokens:
            _send_fcm(t.token, title, body, ref)
    except Exception as e:
        print(f"⚠️ Erreur notif patron: {e}")

def notifier_client(db, ref: str, title: str, body: str):
    """Envoyer une notification au client d'une commande"""
    ensure_tokens_table(db)
    try:
        tokens = db.execute(text(
            "SELECT token FROM fcm_tokens WHERE ref=:ref AND role='client'"
        ), {"ref": ref}).fetchall()
        for t in tokens:
            _send_fcm(t.token, title, body, ref)
    except Exception as e:
        print(f"⚠️ Erreur notif client: {e}")
