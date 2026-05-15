from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, Any, Optional
import secrets
from database import get_db
from models import Config, Employe

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_MAX_AGE   = 7 * 24 * 3600
COOKIE_NAME      = "fg_admin_session"
SESSION_TTL_DAYS = 7

# ── Point 2 : longueur minimale des mots de passe ─────────────
PWD_MIN_LENGTH = 8  # appliqué à TOUS les mots de passe (patron + employés)


def ensure_sessions_table(db: Session):
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


def ensure_totp_columns(db: Session):
    """Ajoute totp_secret et totp_enabled sur configs si absent (migration auto)."""
    try:
        db.execute(text(
            "ALTER TABLE configs ADD COLUMN IF NOT EXISTS totp_secret VARCHAR"
        ))
        db.execute(text(
            "ALTER TABLE configs ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN DEFAULT FALSE"
        ))
        db.commit()
    except Exception:
        db.rollback()


def purge_expired_sessions(db: Session):
    try:
        db.execute(text(
            f"DELETE FROM admin_sessions "
            f"WHERE created_at < NOW() - INTERVAL '{SESSION_TTL_DAYS} days'"
        ))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[auth] purge_expired_sessions error: {e}")


def session_get(db: Session, token: str) -> str | None:
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


# ── Point 2 : helpers TOTP ────────────────────────────────────

def _totp_available() -> bool:
    try:
        import pyotp  # noqa
        return True
    except ImportError:
        return False


def _verify_totp(secret: str, code: str) -> bool:
    """Vérifie un code TOTP à 6 chiffres. Fenêtre ±1 période (30s)."""
    try:
        import pyotp
        totp = pyotp.TOTP(secret)
        return totp.verify(code, valid_window=1)
    except Exception:
        return False


def _generate_totp_secret() -> str:
    import pyotp
    return pyotp.random_base32()


def _totp_provisioning_uri(secret: str, account: str = "patron@fougahshop") -> str:
    import pyotp
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name="FougahShop")


# ── Login ─────────────────────────────────────────────────────

@router.post("/login")
def login(body: Dict[str, Any], response: Response, request: Request,
          db: Session = Depends(get_db)):
    import time as _time

    password  = str(body.get("password", "")).strip()
    totp_code = str(body.get("totp_code", "")).strip()  # optionnel à ce stade

    if not password:
        raise HTTPException(401, "Mot de passe requis")

    cfg  = get_config(db)
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
        ip = (request.headers.get("CF-Connecting-IP")
              or request.headers.get("X-Forwarded-For", "").split(",")[0]
              or (request.client.host if request.client else "?"))
        print(f"🚨 Tentative login échouée — IP: {ip} — pwd: {'*'*len(password)}")
        _time.sleep(0.5)
        raise HTTPException(401, "Mot de passe incorrect")

    # ── 2FA : uniquement si patron + totp activé ──────────────
    totp_enabled = getattr(cfg, "totp_enabled", False) or False
    totp_secret  = getattr(cfg, "totp_secret",  None)

    if role == "patron" and totp_enabled and totp_secret:
        if not totp_code:
            # Première étape OK, mot de passe correct → demander le code TOTP
            # On NE crée PAS de session encore
            return {"ok": False, "step": "totp_required"}

        if not _verify_totp(totp_secret, totp_code):
            _time.sleep(0.3)
            raise HTTPException(401, "Code 2FA incorrect ou expiré")

    # Mot de passe OK (+ TOTP OK si activé) → créer la session
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


# ── Middlewares d'auth — définis ICI avant les endpoints qui en dépendent ──

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


# ── Point 2a : reset mot de passe — min 8 chars ───────────────

@router.post("/reset")
def reset_password(body: Dict[str, Any], db: Session = Depends(get_db)):
    secret       = str(body.get("secret", "")).strip()
    new_password = str(body.get("new_password", "")).strip()

    if not secret:
        raise HTTPException(400, "Code secret requis")
    if not new_password or len(new_password) < PWD_MIN_LENGTH:
        raise HTTPException(
            400,
            f"Mot de passe trop court (minimum {PWD_MIN_LENGTH} caractères)"
        )

    cfg = get_config(db)
    if not cfg.secret_reset or secret != cfg.secret_reset:
        raise HTTPException(403, "Code secret incorrect")

    cfg.admin_pwd = new_password
    try:
        db.execute(text("DELETE FROM admin_sessions WHERE role = 'patron'"))
    except Exception:
        pass
    db.commit()
    return {"ok": True, "message": "Mot de passe mis à jour"}


# ── Point 2b : setup 2FA TOTP ─────────────────────────────────

@router.post("/totp/setup")
def totp_setup(request: Request, db: Session = Depends(get_db),
               role: str = Depends(require_patron)):
    """
    Génère un nouveau secret TOTP et retourne l'URI pour le QR code.
    Le secret N'EST PAS encore activé — il faut appeler /totp/confirm pour l'activer.
    """
    if not _totp_available():
        raise HTTPException(500, "pyotp non installé — ajoutez 'pyotp' dans requirements.txt")

    cfg    = get_config(db)
    secret = _generate_totp_secret()
    uri    = _totp_provisioning_uri(secret)

    # Stocker le secret en attente (totp_enabled reste False jusqu'à confirmation)
    try:
        cfg.totp_secret = secret
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "Erreur lors de la sauvegarde du secret")

    # Générer le QR code en base64 si qrcode est disponible
    qr_base64 = None
    try:
        import qrcode, io, base64
        img = qrcode.make(uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_base64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except ImportError:
        pass  # qrcode optionnel

    return {
        "secret":    secret,
        "uri":       uri,
        "qr_base64": qr_base64,
    }


@router.post("/totp/confirm")
def totp_confirm(body: Dict[str, Any], request: Request,
                 db: Session = Depends(get_db),
                 role: str = Depends(require_patron)):
    """
    Confirme l'activation de la 2FA en vérifiant le premier code TOTP.
    Après confirmation, totp_enabled = True.
    """
    code = str(body.get("code", "")).strip()
    if not code:
        raise HTTPException(400, "Code TOTP requis")

    cfg    = get_config(db)
    secret = getattr(cfg, "totp_secret", None)
    if not secret:
        raise HTTPException(400, "Aucun secret TOTP configuré — lancez d'abord /totp/setup")

    if not _verify_totp(secret, code):
        raise HTTPException(400, "Code TOTP invalide — réessayez")

    cfg.totp_enabled = True
    db.commit()
    return {"ok": True, "message": "2FA activée avec succès"}


@router.post("/totp/disable")
def totp_disable(body: Dict[str, Any], request: Request,
                 db: Session = Depends(get_db),
                 role: str = Depends(require_patron)):
    """
    Désactive la 2FA. Nécessite le mot de passe patron pour confirmer.
    """
    password = str(body.get("password", "")).strip()
    cfg      = get_config(db)

    if not password or password != cfg.admin_pwd:
        raise HTTPException(403, "Mot de passe incorrect")

    cfg.totp_enabled = False
    cfg.totp_secret  = None
    db.commit()
    return {"ok": True, "message": "2FA désactivée"}


@router.get("/totp/status")
def totp_status(request: Request, db: Session = Depends(get_db),
                role: str = Depends(require_patron)):
    """Retourne l'état actuel de la 2FA."""
    cfg = get_config(db)
    return {
        "totp_enabled":   bool(getattr(cfg, "totp_enabled", False)),
        "totp_configured": bool(getattr(cfg, "totp_secret", None)),
    }
