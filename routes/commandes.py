from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import json
from database import get_db
from models import Commande, Config, PortKg

router = APIRouter(prefix="/api/commandes", tags=["commandes"])

try:
    from routes.notifs import notifier_patron
except Exception:
    def notifier_patron(*a, **kw): pass

MONNAIES = {
    "Burkina Faso": {"symbole": "FCFA", "taux_base": 656},
    "Guinée":       {"symbole": "GNF",  "taux_base": None},  # flottant
    "Cameroun":     {"symbole": "FCFA", "taux_base": 656},
    "Bénin":        {"symbole": "FCFA", "taux_base": 656},
    "Togo":         {"symbole": "FCFA", "taux_base": 656},
    "Niger":        {"symbole": "FCFA", "taux_base": 656},
    "Congo":        {"symbole": "FCFA", "taux_base": 656},
    "Gabon":        {"symbole": "FCFA", "taux_base": 656},
}

def get_config(db):
    cfg = db.query(Config).first()
    if not cfg:
        cfg = Config(); db.add(cfg); db.commit(); db.refresh(cfg)
    return cfg

def get_port(db, pays):
    p = db.query(PortKg).filter(PortKg.pays == pays).first()
    return p.prix if p else 7000.0

def generate_ref(db):
    count = db.query(Commande).count() + 1
    return f"CMD-{datetime.now().year}-{count:04d}"

def calc_article(prix_eu, poids, pays, qty, cfg, db):
    taux = cfg.taux_change
    port_fcfa_kg = get_port(db, pays)
    base_fcfa = round(prix_eu * taux)
    port_fcfa = round(port_fcfa_kg * poids)
    total_fcfa_unit = base_fcfa + cfg.commission + port_fcfa
    # Convertir en monnaie locale
    m = MONNAIES.get(pays, {"symbole": "FCFA", "taux_base": 656})
    taux_local = cfg.taux_gnf if m["symbole"] == "GNF" else 656
    taux_conv = taux_local / 656
    total_local = round(total_fcfa_unit * taux_conv * qty)
    return {
        "base_fcfa": base_fcfa,
        "port_fcfa": port_fcfa,
        "commission": cfg.commission,
        "total_local": total_local,
        "monnaie": m["symbole"],
    }

# ── Schemas ───────────────────────────────────────────────────
class ArticleIn(BaseModel):
    lien:      str
    nom:       str
    img:       Optional[str] = None
    categorie: Optional[str] = None
    taille:    Optional[str] = None
    couleur:   Optional[str] = None
    specs:     Optional[str] = None
    prix_eu:   float
    poids:     float = 0.5
    qty:       int = 1

class CommandeCreate(BaseModel):
    client_nom:          str
    client_tel:          str
    client_pays:         str
    client_adresse:      Optional[str] = None
    client_instructions: Optional[str] = None
    operateur:           str
    articles:            List[ArticleIn]

class CalculRequest(BaseModel):
    prix_eu: float
    poids:   float
    pays:    str
    qty:     int = 1

# ── Routes ────────────────────────────────────────────────────
@router.post("/calculer")
def calculer(body: CalculRequest, db: Session = Depends(get_db)):
    cfg = get_config(db)
    detail = calc_article(body.prix_eu, body.poids, body.pays, body.qty, cfg, db)
    return detail

@router.post("/", status_code=201)
def creer_commande(body: CommandeCreate, db: Session = Depends(get_db)):
    if not body.articles:
        raise HTTPException(400, "Panier vide")
    cfg = get_config(db)
    m = MONNAIES.get(body.client_pays, {"symbole": "FCFA"})
    port_info = db.query(PortKg).filter(PortKg.pays == body.client_pays).first()

    articles_detail = []
    total_eu = 0.0
    total_local = 0.0
    poids_total = 0.0

    for a in body.articles:
        detail = calc_article(a.prix_eu, a.poids, body.client_pays, a.qty, cfg, db)
        articles_detail.append({
            "lien": a.lien, "nom": a.nom, "img": a.img,
            "categorie": a.categorie, "taille": a.taille,
            "couleur": a.couleur, "specs": a.specs,
            "prix_eu": a.prix_eu, "poids": a.poids, "qty": a.qty,
            "total_local": detail["total_local"], "monnaie": detail["monnaie"],
        })
        total_eu += a.prix_eu * a.qty
        total_local += detail["total_local"]
        poids_total += a.poids * a.qty

    commande = Commande(
        ref=generate_ref(db),
        client_nom=body.client_nom,
        client_tel=body.client_tel,
        client_pays=body.client_pays,
        client_adresse=body.client_adresse,
        client_instructions=body.client_instructions,
        operateur=body.operateur,
        monnaie=m["symbole"],
        total_euro=round(total_eu, 2),
        total_local=round(total_local),
        poids_estime=round(poids_total, 2),
        articles=json.dumps(articles_detail, ensure_ascii=False),
        nb_articles=len(body.articles),
        statut="en_attente_paiement",
        delai_livraison=port_info.delai if port_info else "—",
    )
    db.add(commande); db.commit(); db.refresh(commande)

    # Notifier le patron
    notifier_patron(db, "🛍️ Nouvelle commande reçue !",
        f"{commande.client_nom} · {commande.ref} · {round(commande.total_local or 0):,} {commande.monnaie or 'FCFA'}",
        commande.ref)

    return {
        "ref": commande.ref,
        "total_local": commande.total_local,
        "monnaie": commande.monnaie,
        "nb_articles": commande.nb_articles,
        "statut": commande.statut,
    }

@router.get("/suivi/{ref}")
def suivi(ref: str, db: Session = Depends(get_db)):
    cmd = db.query(Commande).filter(Commande.ref == ref.upper()).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    return {
        "ref": cmd.ref, "statut": cmd.statut,
        "nb_articles": cmd.nb_articles,
        "total_local": cmd.total_local, "monnaie": cmd.monnaie,
        "poids_estime": cmd.poids_estime, "poids_reel": cmd.poids_reel,
        "delai_livraison": cmd.delai_livraison,
        "articles": json.loads(cmd.articles) if cmd.articles else [],
        "note_admin": cmd.note_admin,
        "created_at": cmd.created_at,
    }

@router.get("/historique/{tel}")
def historique(tel: str, db: Session = Depends(get_db)):
    tel_clean = tel.replace(" ", "").replace("+", "")
    cmds = db.query(Commande).filter(
        Commande.client_tel.contains(tel_clean[-8:])  # chercher sur les 8 derniers chiffres
    ).order_by(Commande.created_at.desc()).all()
    if not cmds:
        raise HTTPException(404, "Aucune commande trouvée")
    return [
        {
            "ref": c.ref, "statut": c.statut,
            "nb_articles": c.nb_articles,
            "total_local": c.total_local, "monnaie": c.monnaie,
            "delai_livraison": c.delai_livraison,
            "note_admin": c.note_admin,
            "created_at": c.created_at,
        }
        for c in cmds
    ]
