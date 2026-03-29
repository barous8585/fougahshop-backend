from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional, Any, Dict
from datetime import datetime
import json
from database import get_db
from models import Commande, Config, PortKg

router = APIRouter(prefix="/api/commandes", tags=["commandes"])

PALIERS_COMMISSION = [
    {"max": 50,    "comm": 3500},
    {"max": 100,   "comm": 5000},
    {"max": 200,   "comm": 7000},
    {"max": 500,   "comm": 12000},
    {"max": 99999, "comm": 20000},
]

def get_commission(total_euros: float) -> float:
    for palier in PALIERS_COMMISSION:
        if total_euros <= palier["max"]:
            return palier["comm"]
    return 20000

MONNAIES = {
    "Burkina Faso": {"symbole": "FCFA", "taux_base": 656},
    "Guinée":       {"symbole": "GNF",  "taux_base": None},
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

def calc_article(prix_eu, poids, pays, qty, cfg, db, commission_par_article=0):
    """commission_par_article = commission totale / nb articles du panier"""
    port_fcfa_kg = get_port(db, pays)
    port_fcfa = round(port_fcfa_kg * poids)
    m = MONNAIES.get(pays, {"symbole": "FCFA", "taux_base": 656})

    if m["symbole"] == "GNF":
        # Calcul direct en GNF sans passer par FCFA
        taux_gnf = cfg.taux_gnf or 9500
        base_gnf = round(prix_eu * taux_gnf)
        port_gnf = round(port_fcfa * (taux_gnf / 656))
        comm_gnf = round(commission_par_article * (taux_gnf / 656))
        total_local = round((base_gnf + port_gnf) * qty + comm_gnf)
        base_fcfa = round(prix_eu * cfg.taux_change)
    else:
        # FCFA standard
        taux = cfg.taux_change
        base_fcfa = round(prix_eu * taux)
        total_fcfa_unit = base_fcfa + commission_par_article + port_fcfa
        total_local = round(total_fcfa_unit * qty)

    return {
        "base_fcfa": base_fcfa,
        "port_fcfa": port_fcfa,
        "commission": commission_par_article,
        "total_local": total_local,
        "monnaie": m["symbole"],
    }

@router.post("/calculer")
def calculer(body: Dict[str, Any], db: Session = Depends(get_db)):
    cfg = get_config(db)
    detail = calc_article(
        float(body.get("prix_eu", 0)),
        float(body.get("poids", 0.5)),
        str(body.get("pays", "")),
        int(body.get("qty", 1)),
        cfg, db
    )
    return detail

@router.post("/", status_code=201)
def creer_commande(body: Dict[str, Any], db: Session = Depends(get_db)):
    articles_in = body.get("articles", [])
    if not articles_in:
        raise HTTPException(400, "Panier vide")
    cfg = get_config(db)
    client_pays = str(body.get("client_pays", ""))
    m = MONNAIES.get(client_pays, {"symbole": "FCFA"})
    port_info = db.query(PortKg).filter(PortKg.pays == client_pays).first()

    articles_detail = []
    total_eu = 0.0
    total_local = 0.0
    poids_total = 0.0

    # Commission progressive selon total panier
    total_euros_panier = sum(float(a.get("prix_eu", 0)) * int(a.get("qty", 1)) for a in articles_in)
    commission_totale = get_commission(total_euros_panier)
    nb_articles_total = len(articles_in)
    commission_par_article = round(commission_totale / nb_articles_total) if nb_articles_total > 0 else commission_totale

    for a in articles_in:
        prix_eu = float(a.get("prix_eu", 0))
        poids = float(a.get("poids", 0.5))
        qty = int(a.get("qty", 1))
        detail = calc_article(prix_eu, poids, client_pays, qty, cfg, db, commission_par_article)
        articles_detail.append({
            "lien": a.get("lien", ""), "nom": a.get("nom", ""),
            "img": a.get("img"), "categorie": a.get("categorie"),
            "taille": a.get("taille"), "couleur": a.get("couleur"),
            "specs": a.get("specs"), "prix_eu": prix_eu,
            "poids": poids, "qty": qty,
            "total_local": detail["total_local"], "monnaie": detail["monnaie"],
        })
        total_eu += prix_eu * qty
        total_local += detail["total_local"]
        poids_total += poids * qty

    commande = Commande(
        ref=generate_ref(db),
        client_nom=str(body.get("client_nom", "")),
        client_tel=str(body.get("client_tel", "")),
        client_pays=client_pays,
        client_adresse=body.get("client_adresse"),
        client_instructions=body.get("client_instructions"),
        operateur=str(body.get("operateur", "")),
        monnaie=m["symbole"],
        total_euro=round(total_eu, 2),
        total_local=round(total_local),
        poids_estime=round(poids_total, 2),
        articles=json.dumps(articles_detail, ensure_ascii=False),
        nb_articles=len(articles_in),
        statut="en_attente_paiement",
        delai_livraison=port_info.delai if port_info else "—",
    )
    db.add(commande); db.commit(); db.refresh(commande)
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
        "created_at": str(cmd.created_at),
    }

@router.get("/historique/{tel}")
def historique(tel: str, db: Session = Depends(get_db)):
    tel_clean = tel.replace(" ", "").replace("+", "")
    cmds = db.query(Commande).filter(
        Commande.client_tel.contains(tel_clean[-8:])
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
            "created_at": str(c.created_at),
        }
        for c in cmds
    ]
