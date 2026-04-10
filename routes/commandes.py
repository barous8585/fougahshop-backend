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
    "Guinée":       {"symbole": "GNF",  "taux_base": None},
    "Cameroun":     {"symbole": "FCFA", "taux_base": 656},
    "Bénin":        {"symbole": "FCFA", "taux_base": 656},
    "Togo":         {"symbole": "FCFA", "taux_base": 656},
    "Niger":        {"symbole": "FCFA", "taux_base": 656},
    "Congo":        {"symbole": "FCFA", "taux_base": 656},
    "Gabon":        {"symbole": "FCFA", "taux_base": 656},
}

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

def calc_article_sans_port_ni_commission(prix_eu, qty, pays, cfg):
    taux = cfg.taux_change
    base_fcfa = round(prix_eu * taux)
    m = MONNAIES.get(pays, {"symbole": "FCFA", "taux_base": 656})
    taux_local = cfg.taux_gnf if m["symbole"] == "GNF" else 656
    taux_conv = taux_local / 656
    total_local = round(base_fcfa * taux_conv * qty)
    return {
        "base_fcfa":   base_fcfa,
        "total_local": total_local,
        "monnaie":     m["symbole"],
        "taux_conv":   taux_conv,
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
    promo_code:          Optional[str] = None
    articles:            List[ArticleIn]

class CalculRequest(BaseModel):
    prix_eu: float
    poids:   float
    pays:    str
    qty:     int = 1

class AnnulationBody(BaseModel):
    ref:        str
    client_tel: str
    motif:      Optional[str] = None

# ── Routes ────────────────────────────────────────────────────
@router.post("/calculer")
def calculer(body: CalculRequest, db: Session = Depends(get_db)):
    cfg = get_config(db)
    detail = calc_article_sans_port_ni_commission(body.prix_eu, body.qty, body.pays, cfg)
    commission = get_commission(body.prix_eu * body.qty)
    m = MONNAIES.get(body.pays, {"symbole": "FCFA", "taux_base": 656})
    taux_local = cfg.taux_gnf if m["symbole"] == "GNF" else 656
    taux_conv = taux_local / 656
    comm_local = round(commission * taux_conv)
    port_fcfa = get_port(db, body.pays)
    port_local = round(port_fcfa * body.poids * taux_conv)
    return {
        "base_fcfa":        detail["base_fcfa"],
        "commission":       commission,
        "port_estime":      port_local,
        "total_local":      detail["total_local"] + comm_local,
        "total_avec_port":  detail["total_local"] + comm_local + port_local,
        "monnaie":          detail["monnaie"],
    }

@router.post("/", status_code=201)
def creer_commande(body: CommandeCreate, db: Session = Depends(get_db)):
    if not body.articles:
        raise HTTPException(400, "Panier vide")
    cfg = get_config(db)
    m = MONNAIES.get(body.client_pays, {"symbole": "FCFA"})
    port_info = db.query(PortKg).filter(PortKg.pays == body.client_pays).first()

    articles_detail = []
    total_eu = 0.0
    total_local_sans_comm = 0.0
    poids_total = 0.0
    taux_conv = 1.0

    for a in body.articles:
        detail = calc_article_sans_port_ni_commission(a.prix_eu, a.qty, body.client_pays, cfg)
        articles_detail.append({
            "lien":        a.lien,
            "nom":         a.nom,
            "img":         None,
            "categorie":   a.categorie,
            "taille":      a.taille,
            "couleur":     a.couleur,
            "specs":       a.specs,
            "prix_eu":     a.prix_eu,
            "poids":       a.poids,
            "qty":         a.qty,
            "total_local": detail["total_local"],
            "monnaie":     detail["monnaie"],
        })
        total_eu += a.prix_eu * a.qty
        total_local_sans_comm += detail["total_local"]
        poids_total += a.poids * a.qty
        taux_conv = detail["taux_conv"]

    commission_fcfa = get_commission(total_eu)
    commission_locale = round(commission_fcfa * taux_conv)
    total_local = total_local_sans_comm + commission_locale

    if body.promo_code:
        try:
            from sqlalchemy import text
            promo = db.execute(
                text("SELECT * FROM promo_codes WHERE code=:code AND actif=TRUE LIMIT 1"),
                {"code": body.promo_code.upper()}
            ).fetchone()
            if promo and promo.utilisations < promo.quota:
                reduction = round(promo.reduction_fcfa * taux_conv)
                total_local = max(0, total_local - reduction)
                db.execute(
                    text("UPDATE promo_codes SET utilisations = utilisations + 1 WHERE code=:code"),
                    {"code": body.promo_code.upper()}
                )
        except Exception:
            pass

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
    db.add(commande)
    db.commit()
    db.refresh(commande)

    notifier_patron(db, "🛍️ Nouvelle commande reçue !",
        f"{commande.client_nom} · {commande.ref} · {round(commande.total_local or 0):,} {commande.monnaie or 'FCFA'}",
        commande.ref)

    return {
        "ref":         commande.ref,
        "total_local": commande.total_local,
        "monnaie":     commande.monnaie,
        "nb_articles": commande.nb_articles,
        "statut":      commande.statut,
    }

@router.get("/suivi/{ref}")
def suivi(ref: str, db: Session = Depends(get_db)):
    cmd = db.query(Commande).filter(Commande.ref == ref.upper()).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    return {
        "ref":             cmd.ref,
        "statut":          cmd.statut,
        "client_nom":      cmd.client_nom,                          # ✅ AJOUTÉ
        "client_tel":      cmd.client_tel,
        "nb_articles":     cmd.nb_articles,
        "total_local":     cmd.total_local,
        "monnaie":         cmd.monnaie,
        "poids_estime":    cmd.poids_estime,
        "poids_reel":      cmd.poids_reel,
        "delai_livraison": cmd.delai_livraison,
        "articles":        json.loads(cmd.articles) if cmd.articles else [],
        "note_admin":      cmd.note_admin,
        "suivi_num":       getattr(cmd, "suivi_num", None),         # ✅ AJOUTÉ
        "motif_refus":     getattr(cmd, "motif_refus", None),       # ✅ AJOUTÉ
        "created_at":      cmd.created_at,
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
            "ref":             c.ref,
            "statut":          c.statut,
            "nb_articles":     c.nb_articles,
            "total_local":     c.total_local,
            "monnaie":         c.monnaie,
            "delai_livraison": c.delai_livraison,
            "note_admin":      c.note_admin,
            "client_nom":      c.client_nom,
            "client_tel":      c.client_tel,    # ✅ AJOUTÉ
            "created_at":      c.created_at,
        }
        for c in cmds
    ]

@router.post("/annuler")
def annuler_commande(body: AnnulationBody, db: Session = Depends(get_db)):
    ref = body.ref.strip().upper()
    cmd = db.query(Commande).filter(Commande.ref == ref).first()

    if not cmd:
        raise HTTPException(404, "Commande introuvable")

    tel_clean = body.client_tel.replace(" ", "").replace("+", "")
    cmd_tel_clean = (cmd.client_tel or "").replace(" ", "").replace("+", "")
    if tel_clean[-8:] not in cmd_tel_clean:
        raise HTTPException(403, "Numéro de téléphone incorrect")

    STATUTS_ANNULABLES = ["en_attente_paiement", "paye"]
    if cmd.statut not in STATUTS_ANNULABLES:
        raise HTTPException(400, f"Annulation impossible — statut actuel : {cmd.statut}")

    ancien_statut = cmd.statut
    cmd.statut = "annulee"
    note = f"[ANNULATION CLIENT] Tel: {body.client_tel}"
    if body.motif:
        note += f" | Motif: {body.motif}"
    cmd.note_admin = (cmd.note_admin or "") + " | " + note
    db.commit()

    try:
        notifier_patron(
            db,
            "❌ Demande d'annulation",
            f"{cmd.ref} · {cmd.client_nom} · {cmd.client_pays} · Ancien statut: {ancien_statut}",
            cmd.ref
        )
    except Exception:
        pass

    return {"ok": True, "ref": cmd.ref, "statut": "annulee"}
