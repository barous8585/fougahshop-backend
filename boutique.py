from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import Column, Integer, String, Float, Boolean, Text, DateTime
from sqlalchemy.ext.declarative import declarative_base
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import json

from database import get_db, Base
from auth import require_auth, require_patron

router = APIRouter(prefix="/api/boutique", tags=["boutique"])

# ══════════════════════════════════════════════════════════════════════
# MODÈLE PRODUIT
# ══════════════════════════════════════════════════════════════════════
class Produit(Base):
    __tablename__ = "produits"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    nom         = Column(String(200), nullable=False)
    description = Column(Text, default="")
    categorie   = Column(String(80), default="")
    image_url   = Column(String(500), default="")
    images      = Column(Text, default="[]")   # JSON list d'URLs
    prix_eur    = Column(Float, nullable=False)  # prix de VENTE fixé par le patron
    badge       = Column(String(50), default="")  # "Nouveau", "Populaire", "Promo"
    actif       = Column(Boolean, default=True)
    ordre       = Column(Integer, default=0)
    created_at  = Column(DateTime, default=datetime.utcnow)


# ══════════════════════════════════════════════════════════════════════
# SCHÉMAS PYDANTIC
# ══════════════════════════════════════════════════════════════════════
class ProduitCreate(BaseModel):
    nom:         str
    description: Optional[str] = ""
    categorie:   Optional[str] = ""
    image_url:   Optional[str] = ""
    images:      Optional[List[str]] = []
    prix_eur:    float
    badge:       Optional[str] = ""
    actif:       Optional[bool] = True
    ordre:       Optional[int] = 0


class ProduitUpdate(BaseModel):
    nom:         Optional[str] = None
    description: Optional[str] = None
    categorie:   Optional[str] = None
    image_url:   Optional[str] = None
    images:      Optional[List[str]] = None
    prix_eur:    Optional[float] = None
    badge:       Optional[str] = None
    actif:       Optional[bool] = None
    ordre:       Optional[int] = None


class BoutiqueCommandeBody(BaseModel):
    produit_id:   int
    client_nom:   str
    client_tel:   str
    client_pays:  str
    client_adresse: Optional[str] = ""
    quantite:     Optional[int] = 1


# ══════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════
def serialize_produit(p):
    try:
        imgs = json.loads(p.images) if p.images else []
    except Exception:
        imgs = []
    return {
        "id":          p.id,
        "nom":         p.nom,
        "description": p.description or "",
        "categorie":   p.categorie or "",
        "image_url":   p.image_url or "",
        "images":      imgs,
        "prix_eur":    p.prix_eur,
        "badge":       p.badge or "",
        "actif":       p.actif,
        "ordre":       p.ordre or 0,
        "created_at":  p.created_at,
    }


def get_taux(db: Session, pays: str) -> tuple[float, str]:
    """Retourne (taux_de_conversion, symbole_monnaie) selon le pays."""
    from models import Config
    cfg = db.query(Config).first()
    if pays == "Guinée":
        return (cfg.taux_gnf if cfg else 9500), "GNF"
    return 656.0, "FCFA"


# ══════════════════════════════════════════════════════════════════════
# ROUTES CLIENT — lecture seule
# ══════════════════════════════════════════════════════════════════════
@router.get("/produits")
def liste_produits(categorie: Optional[str] = None, db: Session = Depends(get_db)):
    """Liste tous les produits actifs, ordonnés."""
    q = db.query(Produit).filter(Produit.actif == True)
    if categorie:
        q = q.filter(Produit.categorie == categorie)
    produits = q.order_by(Produit.ordre.asc(), Produit.created_at.desc()).all()
    return [serialize_produit(p) for p in produits]


@router.get("/produits/categories")
def liste_categories(db: Session = Depends(get_db)):
    """Liste les catégories disponibles (avec au moins 1 produit actif)."""
    rows = (
        db.query(Produit.categorie)
        .filter(Produit.actif == True, Produit.categorie != "")
        .distinct()
        .all()
    )
    return [r[0] for r in rows if r[0]]


@router.get("/produits/{produit_id}")
def detail_produit(produit_id: int, db: Session = Depends(get_db)):
    p = db.query(Produit).filter(Produit.id == produit_id, Produit.actif == True).first()
    if not p:
        raise HTTPException(404, "Produit introuvable")
    return serialize_produit(p)


# ══════════════════════════════════════════════════════════════════════
# ROUTE COMMANDE BOUTIQUE
# ══════════════════════════════════════════════════════════════════════
@router.post("/commandes")
def creer_commande_boutique(body: BoutiqueCommandeBody, db: Session = Depends(get_db)):
    """Crée une commande depuis la boutique — même modèle Commande, source='boutique'."""
    from models import Commande, Config
    from commandes import get_commission
    import random, string

    produit = db.query(Produit).filter(
        Produit.id == body.produit_id, Produit.actif == True
    ).first()
    if not produit:
        raise HTTPException(404, "Produit introuvable ou indisponible")

    qty = max(1, body.quantite or 1)
    total_eur = round(produit.prix_eur * qty, 2)

    # Commission calculée sur le prix de vente
    commission_fcfa = get_commission(total_eur)
    taux, monnaie   = get_taux(db, body.client_pays)
    total_local     = round((total_eur * taux / 656) * 656 + commission_fcfa * (taux / 656))
    # Simplifié : total_eur * taux + commission
    if monnaie == "GNF":
        total_local = round(total_eur * taux + commission_fcfa * taux / 656)
    else:
        total_local = round(total_eur * 656 + commission_fcfa)

    # Générer une référence boutique (préfixe BTQ)
    suffix = ''.join(random.choices(string.digits, k=4))
    year   = datetime.utcnow().year
    ref    = f"BTQ-{year}-{suffix}"
    # Éviter les doublons
    while db.query(Commande).filter(Commande.ref == ref).first():
        suffix = ''.join(random.choices(string.digits, k=4))
        ref    = f"BTQ-{year}-{suffix}"

    article = [{
        "nom":      f"{produit.nom} × {qty}" if qty > 1 else produit.nom,
        "lien":     "",
        "prix_eu":  produit.prix_eur,
        "qty":      qty,
        "poids":    0.5,
        "categorie":"",
        "image":    produit.image_url or "",
    }]

    cmd = Commande(
        ref                  = ref,
        statut               = "en_attente_paiement",
        client_nom           = body.client_nom.strip(),
        client_tel           = body.client_tel.strip().replace(" ", ""),
        client_pays          = body.client_pays,
        client_adresse       = body.client_adresse or "",
        client_instructions  = f"[BOUTIQUE] Produit ID:{body.produit_id} — {produit.nom}",
        operateur            = "",
        monnaie              = monnaie,
        total_euro           = total_eur,
        total_local          = total_local,
        nb_articles          = qty,
        articles             = json.dumps(article, ensure_ascii=False),
        created_at           = datetime.utcnow(),
    )
    db.add(cmd)
    db.commit()
    db.refresh(cmd)

    return {
        "ref":         cmd.ref,
        "total_local": cmd.total_local,
        "monnaie":     cmd.monnaie,
        "total_eur":   cmd.total_euro,
        "produit":     produit.nom,
    }


# ══════════════════════════════════════════════════════════════════════
# ROUTES ADMIN — gestion du catalogue
# ══════════════════════════════════════════════════════════════════════
@router.get("/admin/produits")
def admin_liste_produits(
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_auth),
):
    produits = db.query(Produit).order_by(Produit.ordre.asc(), Produit.created_at.desc()).all()
    return [serialize_produit(p) for p in produits]


@router.post("/admin/produits")
def admin_creer_produit(
    body: ProduitCreate,
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron),
):
    if not body.nom or not body.nom.strip():
        raise HTTPException(400, "Nom du produit requis")
    if body.prix_eur <= 0:
        raise HTTPException(400, "Prix invalide")

    p = Produit(
        nom         = body.nom.strip(),
        description = body.description or "",
        categorie   = body.categorie or "",
        image_url   = body.image_url or "",
        images      = json.dumps(body.images or []),
        prix_eur    = round(body.prix_eur, 2),
        badge       = body.badge or "",
        actif       = body.actif if body.actif is not None else True,
        ordre       = body.ordre or 0,
        created_at  = datetime.utcnow(),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return serialize_produit(p)


@router.patch("/admin/produits/{produit_id}")
def admin_modifier_produit(
    produit_id: int,
    body: ProduitUpdate,
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron),
):
    p = db.query(Produit).filter(Produit.id == produit_id).first()
    if not p:
        raise HTTPException(404, "Produit introuvable")

    if body.nom         is not None: p.nom         = body.nom.strip()
    if body.description is not None: p.description = body.description
    if body.categorie   is not None: p.categorie   = body.categorie
    if body.image_url   is not None: p.image_url   = body.image_url
    if body.images      is not None: p.images      = json.dumps(body.images)
    if body.prix_eur    is not None: p.prix_eur    = round(body.prix_eur, 2)
    if body.badge       is not None: p.badge       = body.badge
    if body.actif       is not None: p.actif       = body.actif
    if body.ordre       is not None: p.ordre       = body.ordre

    db.commit()
    return serialize_produit(p)


@router.delete("/admin/produits/{produit_id}")
def admin_supprimer_produit(
    produit_id: int,
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron),
):
    p = db.query(Produit).filter(Produit.id == produit_id).first()
    if not p:
        raise HTTPException(404, "Produit introuvable")
    db.delete(p)
    db.commit()
    return {"ok": True, "id": produit_id}


@router.patch("/admin/produits/{produit_id}/toggle")
def admin_toggle_produit(
    produit_id: int,
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron),
):
    p = db.query(Produit).filter(Produit.id == produit_id).first()
    if not p:
        raise HTTPException(404, "Produit introuvable")
    p.actif = not p.actif
    db.commit()
    return {"id": produit_id, "actif": p.actif}
