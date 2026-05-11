from fastapi          import APIRouter, Depends, HTTPException
from sqlalchemy.orm   import Session
from sqlalchemy       import text
from pydantic         import BaseModel
from typing           import Optional, List
from database         import get_db
from routes.auth      import require_auth
import json as _json

router = APIRouter(tags=["avis"])

# ══════════════════════════════════════════════════════════════
# MIGRATION — colonnes manquantes
# ══════════════════════════════════════════════════════════════

def ensure_avis_columns(db: Session):
    """
    Ajoute toutes les colonnes manquantes de la table avis.
    Appelée au startup depuis main.py ET à chaque POST /api/avis (sécurité).
    """
    colonnes = [
        "ALTER TABLE avis ADD COLUMN IF NOT EXISTS client_tel    VARCHAR",
        "ALTER TABLE avis ADD COLUMN IF NOT EXISTS taille_retour VARCHAR",
        "ALTER TABLE avis ADD COLUMN IF NOT EXISTS photo_url     VARCHAR",
        "ALTER TABLE avis ADD COLUMN IF NOT EXISTS photos_urls   TEXT",
        "ALTER TABLE avis ADD COLUMN IF NOT EXISTS verifie       BOOLEAN DEFAULT FALSE",
        "ALTER TABLE avis ADD COLUMN IF NOT EXISTS utile_count   INTEGER DEFAULT 0",
        "ALTER TABLE avis ADD COLUMN IF NOT EXISTS commande_ref  VARCHAR",
        "ALTER TABLE avis ADD COLUMN IF NOT EXISTS commentaire   TEXT",
        "ALTER TABLE avis ADD COLUMN IF NOT EXISTS client_nom    VARCHAR",
    ]
    for sql in colonnes:
        try:
            db.execute(text(sql))
            db.commit()
        except Exception:
            db.rollback()


# ── Modèles Pydantic ──────────────────────────────────────────

class AvisCreate(BaseModel):
    note:          int
    commentaire:   Optional[str]       = None
    client_tel:    Optional[str]       = None
    taille_retour: Optional[str]       = None
    photo_url:     Optional[str]       = None   # compat 1 photo
    photos_urls:   Optional[List[str]] = None   # jusqu'à 5 photos
    client_nom:    Optional[str]       = None
    commande_ref:  Optional[str]       = None


class AvisReponse(BaseModel):
    reponse: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────

def _get_all_photos(body: AvisCreate) -> List[str]:
    if body.photos_urls:
        urls = [u.strip() for u in body.photos_urls if u and u.strip()]
    elif body.photo_url:
        urls = [body.photo_url.strip()]
    else:
        urls = []
    return urls[:5]


def _parse_photos(row_dict: dict) -> List[str]:
    raw = row_dict.get("photos_urls")
    if raw:
        try:
            photos = _json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(photos, list) and photos:
                return photos
        except Exception:
            pass
    if row_dict.get("photo_url"):
        return [row_dict["photo_url"]]
    return []


# ── Valider les données ───────────────────────────────────────

def _valider_avis(body: AvisCreate):
    if not (1 <= body.note <= 5):
        raise HTTPException(400, "Note invalide (1-5)")
    if body.taille_retour and body.taille_retour not in ["Trop petit", "Taille correcte", "Trop grand"]:
        raise HTTPException(400, "Taille invalide")
    all_photos = _get_all_photos(body)
    if len(all_photos) > 5:
        raise HTTPException(400, "Maximum 5 photos autorisées")
    for url in all_photos:
        if not url.startswith("https://"):
            raise HTTPException(400, "URL photo invalide")


# ── Endpoints publics ─────────────────────────────────────────

@router.get("/api/avis")
def get_avis_public(db: Session = Depends(get_db)):
    try:
        rows = db.execute(text("""
            SELECT id, nom, client_nom, pays, drapeau, note,
                   texte, commentaire, reponse, created_at,
                   taille_retour, photo_url, photos_urls,
                   verifie, utile_count, commande_ref
            FROM avis
            WHERE visible = TRUE
            ORDER BY created_at DESC
            LIMIT 50
        """)).fetchall()
    except Exception:
        # Si une colonne manque, retourner liste vide plutôt que crasher
        return []

    result = []
    for r in rows:
        rd = dict(r._mapping)
        result.append({
            "id":            rd.get("id"),
            "client_nom":    rd.get("client_nom") or rd.get("nom") or "Client",
            "pays":          rd.get("pays"),
            "note":          rd.get("note"),
            "commentaire":   rd.get("commentaire") or rd.get("texte") or "",
            "reponse":       rd.get("reponse"),
            "created_at":    str(rd.get("created_at", ""))[:10],
            "taille_retour": rd.get("taille_retour"),
            "photo_url":     rd.get("photo_url"),
            "photos_urls":   _parse_photos(rd),
            "verifie":       rd.get("verifie") or False,
            "utile_count":   rd.get("utile_count") or 0,
            "commande_ref":  rd.get("commande_ref") or None,
        })
    return result


@router.post("/api/avis")
def creer_avis(body: AvisCreate, db: Session = Depends(get_db)):
    _valider_avis(body)

    # ✅ Migration à chaque appel — garantit que les colonnes existent
    ensure_avis_columns(db)

    nom_client   = body.client_nom or "Client FougahShop"
    commande_ref = body.commande_ref or None

    if body.client_tel:
        try:
            row = db.execute(text("""
                SELECT client_nom, ref FROM commandes
                WHERE client_tel = :tel
                ORDER BY created_at DESC LIMIT 1
            """), {"tel": body.client_tel}).fetchone()
            if row:
                nom_client = row.client_nom or nom_client
                if not commande_ref:
                    commande_ref = row.ref
        except Exception:
            pass

    if commande_ref and body.client_tel:
        try:
            check = db.execute(text("""
                SELECT 1 FROM commandes WHERE ref = :ref AND client_tel = :tel
            """), {"ref": commande_ref, "tel": body.client_tel}).fetchone()
            if not check:
                commande_ref = None
        except Exception:
            commande_ref = None

    all_photos       = _get_all_photos(body)
    photos_json      = _json.dumps(all_photos) if all_photos else None
    photo_url_legacy = all_photos[0] if all_photos else None

    try:
        db.execute(text("""
            INSERT INTO avis
                (nom, client_nom, client_tel, note, texte, commentaire,
                 taille_retour, photo_url, photos_urls, commande_ref,
                 visible, verifie, utile_count)
            VALUES
                (:nom, :nom, :tel, :note, :commentaire, :commentaire,
                 :taille_retour, :photo_url, :photos_urls, :commande_ref,
                 FALSE, FALSE, 0)
        """), {
            "nom":           nom_client,
            "tel":           body.client_tel or "",
            "note":          body.note,
            "commentaire":   (body.commentaire or "").strip(),
            "taille_retour": body.taille_retour,
            "photo_url":     photo_url_legacy,
            "photos_urls":   photos_json,
            "commande_ref":  commande_ref,
        })
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[avis] Erreur INSERT: {e}")
        raise HTTPException(500, f"Erreur enregistrement avis: {str(e)}")

    return {"ok": True, "message": "Avis enregistré — merci !"}


@router.post("/api/avis/{avis_id}/utile")
def marquer_utile(avis_id: int, db: Session = Depends(get_db)):
    db.execute(text(
        "UPDATE avis SET utile_count = COALESCE(utile_count, 0) + 1 WHERE id = :id"
    ), {"id": avis_id})
    db.commit()
    return {"ok": True}


# ── Endpoints admin ───────────────────────────────────────────

@router.get("/api/avis/admin")
def get_avis_admin(db: Session = Depends(get_db), token: str = Depends(require_auth)):
    try:
        rows = db.execute(text("""
            SELECT id, nom, client_nom, client_tel, pays, note,
                   texte, commentaire, reponse, visible, created_at,
                   taille_retour, photo_url, photos_urls,
                   verifie, utile_count, commande_ref
            FROM avis ORDER BY created_at DESC
        """)).fetchall()
    except Exception:
        rows = []
    result = []
    for r in rows:
        rd = dict(r._mapping)
        rd["photos_urls"] = _parse_photos(rd)
        result.append(rd)
    return result


@router.patch("/api/avis/admin/{avis_id}/visibilite")
def toggle_visible(avis_id: int, db: Session = Depends(get_db),
                   token: str = Depends(require_auth)):
    db.execute(text(
        "UPDATE avis SET visible = NOT COALESCE(visible, FALSE) WHERE id = :id"
    ), {"id": avis_id})
    db.commit()
    return {"ok": True}


@router.put("/api/avis/{avis_id}/visible")
def toggle_visible_legacy(avis_id: int, db: Session = Depends(get_db),
                           token: str = Depends(require_auth)):
    db.execute(text(
        "UPDATE avis SET visible = NOT COALESCE(visible, FALSE) WHERE id = :id"
    ), {"id": avis_id})
    db.commit()
    return {"ok": True}


@router.put("/api/avis/{avis_id}/verifie")
def toggle_verifie(avis_id: int, db: Session = Depends(get_db),
                   token: str = Depends(require_auth)):
    db.execute(text(
        "UPDATE avis SET verifie = NOT COALESCE(verifie, FALSE) WHERE id = :id"
    ), {"id": avis_id})
    db.commit()
    return {"ok": True}


@router.patch("/api/avis/admin/{avis_id}/reponse")
def repondre_avis_patch(avis_id: int, body: AvisReponse,
                        db: Session = Depends(get_db),
                        token: str = Depends(require_auth)):
    reponse_val = body.reponse.strip() if body.reponse else None
    db.execute(text("UPDATE avis SET reponse = :reponse WHERE id = :id"),
               {"reponse": reponse_val, "id": avis_id})
    db.commit()
    return {"ok": True}


@router.post("/api/avis/{avis_id}/reponse")
def repondre_avis_post(avis_id: int, body: AvisReponse,
                       db: Session = Depends(get_db),
                       token: str = Depends(require_auth)):
    reponse_val = body.reponse.strip() if body.reponse else None
    db.execute(text("UPDATE avis SET reponse = :reponse WHERE id = :id"),
               {"reponse": reponse_val, "id": avis_id})
    db.commit()
    return {"ok": True}


@router.delete("/api/avis/admin/{avis_id}")
def supprimer_avis_admin(avis_id: int, db: Session = Depends(get_db),
                         token: str = Depends(require_auth)):
    db.execute(text("DELETE FROM avis WHERE id = :id"), {"id": avis_id})
    db.commit()
    return {"ok": True}


@router.delete("/api/avis/{avis_id}")
def supprimer_avis(avis_id: int, db: Session = Depends(get_db),
                   token: str = Depends(require_auth)):
    db.execute(text("DELETE FROM avis WHERE id = :id"), {"id": avis_id})
    db.commit()
    return {"ok": True}
