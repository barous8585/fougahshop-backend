from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, Any, Optional
from database import get_db
from routes.auth import require_patron

router = APIRouter(prefix="/api/annonces", tags=["annonces"])


def ensure_annonces_table(db: Session):
    """Migration — à appeler au startup dans main.py."""
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS annonces (
                id         SERIAL PRIMARY KEY,
                message    TEXT NOT NULL,
                type       VARCHAR DEFAULT 'info',
                actif      BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[annonces] ensure_table error: {e}")


# ── Endpoint public — lu par tous les clients ──────────────────
@router.get("/active")
def get_annonce_active(db: Session = Depends(get_db)):
    """
    Retourne l'annonce active la plus récente.
    Appelé par le frontend client au chargement de l'app.
    """
    try:
        row = db.execute(text("""
            SELECT id, message, type, created_at
            FROM annonces
            WHERE actif = TRUE
            ORDER BY created_at DESC
            LIMIT 1
        """)).mappings().first()
        if not row:
            return {"annonce": None}
        return {
            "annonce": {
                "id":         row["id"],
                "message":    row["message"],
                "type":       row["type"] or "info",
                "created_at": str(row["created_at"]),
            }
        }
    except Exception as e:
        print(f"[annonces] get_active error: {e}")
        return {"annonce": None}


# ── Endpoints admin ────────────────────────────────────────────
@router.get("/")
def list_annonces(
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    """Liste toutes les annonces (actives + archivées)."""
    try:
        rows = db.execute(text("""
            SELECT id, message, type, actif, created_at
            FROM annonces
            ORDER BY created_at DESC
            LIMIT 20
        """)).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []


@router.post("/", status_code=201)
def create_annonce(
    body: Dict[str, Any],
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    """Crée une nouvelle annonce et désactive les précédentes."""
    message = str(body.get("message", "")).strip()
    if not message:
        raise HTTPException(400, "Message vide")
    if len(message) > 500:
        raise HTTPException(400, "Message trop long (max 500 caractères)")

    type_ = str(body.get("type", "info"))
    if type_ not in ("info", "promo", "alerte"):
        type_ = "info"

    # Désactiver toutes les annonces actives avant d'en créer une nouvelle
    db.execute(text("UPDATE annonces SET actif = FALSE WHERE actif = TRUE"))

    row = db.execute(text("""
        INSERT INTO annonces (message, type, actif)
        VALUES (:message, :type, TRUE)
        RETURNING id
    """), {"message": message, "type": type_}).fetchone()

    db.commit()
    return {"ok": True, "id": row.id}


@router.patch("/{annonce_id}/toggle")
def toggle_annonce(
    annonce_id: int,
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    """Active ou désactive une annonce."""
    row = db.execute(
        text("SELECT id, actif FROM annonces WHERE id = :id"),
        {"id": annonce_id}
    ).fetchone()
    if not row:
        raise HTTPException(404, "Annonce introuvable")

    new_actif = not bool(row.actif)
    # Si on active, désactiver les autres d'abord
    if new_actif:
        db.execute(text("UPDATE annonces SET actif = FALSE WHERE actif = TRUE"))

    db.execute(
        text("UPDATE annonces SET actif = :actif WHERE id = :id"),
        {"actif": new_actif, "id": annonce_id}
    )
    db.commit()
    return {"ok": True, "actif": new_actif}


@router.delete("/{annonce_id}")
def delete_annonce(
    annonce_id: int,
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    """Supprime une annonce."""
    db.execute(text("DELETE FROM annonces WHERE id = :id"), {"id": annonce_id})
    db.commit()
    return {"ok": True}
