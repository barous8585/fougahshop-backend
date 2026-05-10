from fastapi          import APIRouter, Depends, HTTPException
from sqlalchemy.orm   import Session
from sqlalchemy       import text
from pydantic         import BaseModel
from typing           import Optional
from database         import get_db
from routes.auth      import require_auth

router = APIRouter(tags=["avis"])

# ── Modèles Pydantic ──────────────────────────────────────────

class AvisCreate(BaseModel):
    note:          int
    commentaire:   Optional[str]  = None
    client_tel:    Optional[str]  = None
    taille_retour: Optional[str]  = None   # "Trop petit" / "Taille correcte" / "Trop grand"
    photo_url:     Optional[str]  = None   # URL Cloudinary
    client_nom:    Optional[str]  = None

class AvisReponse(BaseModel):
    reponse: str

class AvisVerifie(BaseModel):
    verifie: bool

# ── Valider les données ───────────────────────────────────────

def _valider_avis(body: AvisCreate):
    if not (1 <= body.note <= 5):
        raise HTTPException(status_code=400, detail="Note invalide (1-5)")
    if body.taille_retour and body.taille_retour not in ["Trop petit", "Taille correcte", "Trop grand"]:
        raise HTTPException(status_code=400, detail="Taille invalide")
    if body.photo_url and not body.photo_url.startswith("https://res.cloudinary.com/"):
        raise HTTPException(status_code=400, detail="URL photo invalide")

# ── Endpoints publics ─────────────────────────────────────────

@router.get("/api/avis")
def get_avis_public(db: Session = Depends(get_db)):
    """Avis visibles pour la landing page."""
    rows = db.execute(text("""
        SELECT id, nom, client_nom, pays, drapeau, note,
               texte, commentaire, reponse, created_at,
               taille_retour, photo_url, verifie, utile_count
        FROM avis
        WHERE visible = TRUE
        ORDER BY created_at DESC
        LIMIT 50
    """)).fetchall()

    result = []
    for r in rows:
        r = dict(r._mapping)
        result.append({
            "id":            r.get("id"),
            "client_nom":    r.get("client_nom") or r.get("nom") or "Client",
            "pays":          r.get("pays"),
            "note":          r.get("note"),
            "commentaire":   r.get("commentaire") or r.get("texte") or "",
            "reponse":       r.get("reponse"),
            "created_at":    str(r.get("created_at", ""))[:10],
            "taille_retour": r.get("taille_retour"),
            "photo_url":     r.get("photo_url"),
            "verifie":       r.get("verifie") or False,
            "utile_count":   r.get("utile_count") or 0,
        })
    return result


@router.post("/api/avis")
def creer_avis(body: AvisCreate, db: Session = Depends(get_db)):
    """Soumettre un nouvel avis depuis l'espace client."""
    _valider_avis(body)

    # ✅ Récupérer le nom du client depuis ses commandes
    nom_client = body.client_nom or "Client FougahShop"
    if body.client_tel:
        row = db.execute(text("""
            SELECT client_nom, client_pays FROM commandes
            WHERE client_tel = :tel
            ORDER BY created_at DESC LIMIT 1
        """), {"tel": body.client_tel}).fetchone()
        if row:
            nom_client = row.client_nom or nom_client

    db.execute(text("""
        INSERT INTO avis
            (nom, client_nom, client_tel, note, texte, commentaire,
             taille_retour, photo_url, visible, verifie, utile_count)
        VALUES
            (:nom, :nom, :tel, :note, :commentaire, :commentaire,
             :taille_retour, :photo_url, FALSE, FALSE, 0)
    """), {
        "nom":           nom_client,
        "tel":           body.client_tel or "",
        "note":          body.note,
        "commentaire":   (body.commentaire or "").strip(),
        "taille_retour": body.taille_retour,
        "photo_url":     body.photo_url,
    })
    db.commit()
    return {"ok": True, "message": "Avis enregistré — merci !"}


@router.post("/api/avis/{avis_id}/utile")
def marquer_utile(avis_id: int, db: Session = Depends(get_db)):
    """Incrémenter le compteur 'Utile'."""
    db.execute(text("""
        UPDATE avis SET utile_count = COALESCE(utile_count, 0) + 1
        WHERE id = :id
    """), {"id": avis_id})
    db.commit()
    return {"ok": True}


# ── Endpoints admin ───────────────────────────────────────────

@router.get("/api/avis/admin")
def get_avis_admin(db: Session = Depends(get_db),
                   token: str = Depends(require_auth)):
    """Tous les avis pour l'admin."""
    rows = db.execute(text("""
        SELECT id, nom, client_nom, client_tel, pays, note,
               texte, commentaire, reponse, visible, created_at,
               taille_retour, photo_url, verifie, utile_count
        FROM avis
        ORDER BY created_at DESC
    """)).fetchall()

    return [dict(r._mapping) for r in rows]


@router.put("/api/avis/{avis_id}/visible")
def toggle_visible(avis_id: int, db: Session = Depends(get_db),
                   token: str = Depends(require_auth)):
    """Basculer la visibilité d'un avis."""
    db.execute(text("""
        UPDATE avis SET visible = NOT COALESCE(visible, FALSE)
        WHERE id = :id
    """), {"id": avis_id})
    db.commit()
    return {"ok": True}


@router.put("/api/avis/{avis_id}/verifie")
def toggle_verifie(avis_id: int, db: Session = Depends(get_db),
                   token: str = Depends(require_auth)):
    """Marquer un avis comme vérifié."""
    db.execute(text("""
        UPDATE avis SET verifie = NOT COALESCE(verifie, FALSE)
        WHERE id = :id
    """), {"id": avis_id})
    db.commit()
    return {"ok": True}


@router.post("/api/avis/{avis_id}/reponse")
def repondre_avis(avis_id: int, body: AvisReponse,
                  db: Session = Depends(get_db),
                  token: str = Depends(require_auth)):
    """Ajouter ou modifier la réponse à un avis."""
    db.execute(text("""
        UPDATE avis SET reponse = :reponse WHERE id = :id
    """), {"reponse": body.reponse.strip(), "id": avis_id})
    db.commit()
    return {"ok": True}


@router.delete("/api/avis/{avis_id}")
def supprimer_avis(avis_id: int, db: Session = Depends(get_db),
                   token: str = Depends(require_auth)):
    """Supprimer un avis."""
    db.execute(text("DELETE FROM avis WHERE id = :id"), {"id": avis_id})
    db.commit()
    return {"ok": True}
