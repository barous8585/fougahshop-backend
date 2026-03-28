from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
from typing import Optional
import json, csv, io
from fastapi.responses import StreamingResponse
from database import get_db
from models import Commande, Config
from routes.auth import require_auth, require_patron

router = APIRouter(prefix="/api/admin", tags=["admin"])

STATUTS = ["en_attente_paiement","paye","achete","expedie","arrive","paiement_refuse"]
STATUT_LABELS = {
    "en_attente_paiement": "En attente de paiement",
    "paye":     "Payé",
    "achete":   "Acheté",
    "expedie":  "Expédié",
    "arrive":   "Arrivé",
    "paiement_refuse": "Paiement refusé",
}

def serialize_cmd(c):
    return {
        "id": c.id, "ref": c.ref, "statut": c.statut,
        "statut_label": STATUT_LABELS.get(c.statut, c.statut),
        "client_nom": c.client_nom, "client_tel": c.client_tel,
        "client_pays": c.client_pays, "client_adresse": c.client_adresse,
        "client_instructions": c.client_instructions,
        "operateur": c.operateur, "monnaie": c.monnaie,
        "total_euro": c.total_euro, "total_local": c.total_local,
        "poids_estime": c.poids_estime, "poids_reel": c.poids_reel,
        "nb_articles": c.nb_articles,
        "articles": json.loads(c.articles) if c.articles else [],
        "note_admin": c.note_admin,
        "delai_livraison": c.delai_livraison,
        "paiement_ref": c.paiement_ref,
        "created_at": c.created_at,
    }

@router.get("/stats")
def stats(request: Request, db: Session = Depends(get_db),
          role: str = Depends(require_auth)):
    total = db.query(Commande).count()
    by_statut = {s: db.query(Commande).filter(Commande.statut == s).count()
                 for s in STATUTS}
    encaisse = db.query(func.sum(Commande.total_local)).filter(
        Commande.statut.in_(["paye","achete","expedie","arrive"])
    ).scalar() or 0

    base = {"total": total, "by_statut": by_statut}

    # Finances uniquement pour le patron
    if role == "patron":
        base["encaisse"] = round(encaisse)
        # Marge estimée (commission × nb articles payés)
        cfg = db.query(Config).first()
        nb_articles_payes = db.query(func.sum(Commande.nb_articles)).filter(
            Commande.statut.in_(["paye","achete","expedie","arrive"])
        ).scalar() or 0
        base["marge_estimee"] = round((cfg.commission if cfg else 3500) * nb_articles_payes)

    return base

@router.get("/commandes")
def liste_commandes(
    request: Request,
    statut: Optional[str] = None,
    search: Optional[str] = None,
    date_debut: Optional[str] = None,
    date_fin: Optional[str] = None,
    db: Session = Depends(get_db),
    role: str = Depends(require_auth),
):
    q = db.query(Commande)

    # Employé : uniquement paye et achete
    if role == "employe":
        q = q.filter(Commande.statut.in_(["paye", "achete"]))
    elif statut:
        q = q.filter(Commande.statut == statut)

    if search:
        q = q.filter(
            Commande.ref.ilike(f"%{search}%") |
            Commande.client_nom.ilike(f"%{search}%") |
            Commande.client_tel.ilike(f"%{search}%")
        )
    if date_debut:
        q = q.filter(Commande.created_at >= date_debut)
    if date_fin:
        q = q.filter(Commande.created_at <= date_fin + " 23:59:59")

    cmds = q.order_by(Commande.created_at.desc()).all()

    # Employé ne voit pas les montants
    result = []
    for c in cmds:
        d = serialize_cmd(c)
        if role == "employe":
            d.pop("total_local", None)
            d.pop("total_euro", None)
            d.pop("monnaie", None)
        result.append(d)
    return result

class StatutUpdate(BaseModel):
    statut:      str
    note_admin:  Optional[str] = None
    poids_reel:  Optional[float] = None

@router.patch("/commandes/{ref}/statut")
def update_statut(
    ref: str, body: StatutUpdate,
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_auth),
):
    if body.statut not in STATUTS:
        raise HTTPException(400, "Statut invalide")
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")

    cmd.statut = body.statut
    if body.note_admin:
        cmd.note_admin = (cmd.note_admin or "") + " | " + body.note_admin

    # Ajustement poids réel (patron uniquement)
    if body.poids_reel and role == "patron":
        cmd.poids_reel = body.poids_reel
        # Recalculer le total si poids différent
        cfg = db.query(Config).first()
        from models import PortKg
        port = db.query(PortKg).filter(PortKg.pays == cmd.client_pays).first()
        port_kg = port.prix if port else 7000
        articles = json.loads(cmd.articles) if cmd.articles else []
        nouveau_total = 0
        poids_par_art = body.poids_reel / max(cmd.nb_articles, 1)
        for a in articles:
            base = round(a["prix_eu"] * cfg.taux_change)
            port_art = round(port_kg * poids_par_art)
            total_fcfa = (base + cfg.commission + port_art) * a["qty"]
            taux_local = cfg.taux_gnf if cmd.monnaie == "GNF" else 656
            nouveau_total += round(total_fcfa * (taux_local / 656))
        diff = nouveau_total - cmd.total_local
        cmd.total_local = nouveau_total
        if diff != 0:
            sign = "+" if diff > 0 else ""
            note = f"Poids réel: {body.poids_reel}kg | Ajustement: {sign}{round(diff):,} {cmd.monnaie}"
            cmd.note_admin = (cmd.note_admin or "") + " | " + note

    db.commit()
    return {"ref": cmd.ref, "statut": cmd.statut}

@router.get("/export/csv")
def export_csv(request: Request, db: Session = Depends(get_db),
               role: str = Depends(require_patron)):
    cmds = db.query(Commande).order_by(Commande.created_at.desc()).all()
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow([
        "Référence","Date","Client","Téléphone","Pays","Adresse",
        "Opérateur","Monnaie","Total €","Total local","Poids estimé",
        "Poids réel","Nb articles","Statut","Délai","Notes","Détail articles"
    ])
    for c in cmds:
        arts = json.loads(c.articles) if c.articles else []
        detail = " | ".join([
            f"{a['nom']} x{a.get('qty',1)} ({a.get('poids',0.5)}kg)"
            for a in arts
        ])
        w.writerow([
            c.ref,
            c.created_at.strftime("%d/%m/%Y %H:%M") if c.created_at else "",
            c.client_nom, c.client_tel, c.client_pays, c.client_adresse or "",
            c.operateur, c.monnaie, c.total_euro, c.total_local,
            c.poids_estime or "", c.poids_reel or "",
            c.nb_articles, STATUT_LABELS.get(c.statut, c.statut),
            c.delai_livraison or "", c.note_admin or "", detail
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=commandes_proxyshop.csv"}
    )
