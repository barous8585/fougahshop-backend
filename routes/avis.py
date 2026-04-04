from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel
from typing import Optional
from database import get_db
from routes.auth import require_patron

router = APIRouter(prefix="/api/avis", tags=["avis"])

# ── Migration auto ────────────────────────────────────────────
def ensure_avis_table(db: Session):
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS avis (
            id         SERIAL PRIMARY KEY,
            nom        VARCHAR NOT NULL,
            pays       VARCHAR DEFAULT '',
            drapeau    VARCHAR DEFAULT '',
            note       INTEGER NOT NULL CHECK (note BETWEEN 1 AND 5),
            texte      TEXT NOT NULL,
            reponse    TEXT,
            visible    BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.commit()

# ── Schémas ───────────────────────────────────────────────────
class AvisCreate(BaseModel):
    nom:    str
    pays:   Optional[str] = ''
    drapeau:Optional[str] = ''
    note:   int
    texte:  str

class ReponseBody(BaseModel):
    reponse: Optional[str] = None

# ── Routes publiques ──────────────────────────────────────────
@router.get("/")
def list_avis(db: Session = Depends(get_db)):
    """Retourne les avis visibles pour la page d'accueil"""
    ensure_avis_table(db)
    rows = db.execute(text(
        "SELECT id, nom, pays, drapeau, note, texte, reponse, created_at "
        "FROM avis WHERE visible = TRUE ORDER BY created_at DESC LIMIT 20"
    )).fetchall()
    return [dict(r._mapping) for r in rows]

@router.post("/", status_code=201)
def create_avis(body: AvisCreate, db: Session = Depends(get_db)):
    """Soumettre un nouvel avis (public)"""
    ensure_avis_table(db)
    if len(body.texte.strip()) < 10:
        raise HTTPException(400, "Avis trop court")
    if body.note < 1 or body.note > 5:
        raise HTTPException(400, "Note invalide")

    result = db.execute(text(
        "INSERT INTO avis (nom, pays, drapeau, note, texte) "
        "VALUES (:nom, :pays, :drapeau, :note, :texte) RETURNING id, created_at"
    ), {
        "nom": body.nom.strip()[:80],
        "pays": (body.pays or '').strip()[:50],
        "drapeau": (body.drapeau or '').strip()[:10],
        "note": body.note,
        "texte": body.texte.strip()[:1000],
    })
    db.commit()
    row = result.fetchone()
    return {"ok": True, "id": row.id, "created_at": str(row.created_at)}

# ── Routes admin (patron uniquement) ─────────────────────────
@router.get("/admin")
def list_avis_admin(db: Session = Depends(get_db),
                    role: str = Depends(require_patron)):
    """Tous les avis pour l'admin (y compris masqués)"""
    ensure_avis_table(db)
    rows = db.execute(text(
        "SELECT id, nom, pays, drapeau, note, texte, reponse, visible, created_at "
        "FROM avis ORDER BY created_at DESC"
    )).fetchall()
    return [dict(r._mapping) for r in rows]

@router.patch("/admin/{avis_id}/reponse")
def repondre_avis(avis_id: int, body: ReponseBody,
                  db: Session = Depends(get_db),
                  role: str = Depends(require_patron)):
    """Ajouter ou supprimer une réponse"""
    ensure_avis_table(db)
    db.execute(text(
        "UPDATE avis SET reponse = :reponse WHERE id = :id"
    ), {"reponse": body.reponse, "id": avis_id})
    db.commit()
    return {"ok": True}

class BodyVide(BaseModel):
    pass  # Body optionnel — FastAPI exige un model même pour les PATCH sans body

@router.patch("/admin/{avis_id}/visibilite")
def toggle_visibilite(avis_id: int,
                      body: BodyVide = BodyVide(),
                      db: Session = Depends(get_db),
                      role: str = Depends(require_patron)):
    """Basculer visible/masqué"""
    ensure_avis_table(db)
    db.execute(text(
        "UPDATE avis SET visible = NOT visible WHERE id = :id"
    ), {"id": avis_id})
    db.commit()
    return {"ok": True}

@router.delete("/admin/{avis_id}")
def supprimer_avis(avis_id: int,
                   db: Session = Depends(get_db),
                   role: str = Depends(require_patron)):
    """Supprimer définitivement un avis"""
    ensure_avis_table(db)
    db.execute(text("DELETE FROM avis WHERE id = :id"), {"id": avis_id})
    db.commit()
    return {"ok": True}
