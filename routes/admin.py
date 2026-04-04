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

try:
    from routes.notifs import notifier_client, notifier_patron
    NOTIFS_OK = True
except Exception:
    NOTIFS_OK = False
    def notifier_client(*a, **kw): pass
    def notifier_patron(*a, **kw): pass

router = APIRouter(prefix="/api/admin", tags=["admin"])

STATUTS = ["en_attente_paiement","paye","achete","expedie","arrive","recupere","paiement_refuse","annulee"]
STATUT_LABELS = {
    "en_attente_paiement": "En attente de paiement",
    "paye":     "Payé",
    "achete":   "Acheté",
    "expedie":  "Expédié",
    "arrive":   "Arrivé",
    "recupere": "Récupéré",
    "paiement_refuse": "Paiement refusé",
    "annulee":  "Annulée",
}

# Statuts accessibles par rôle
STATUTS_PAR_ROLE = {
    "patron":      STATUTS,
    "logisticien": ["paye","achete","expedie","arrive","recupere"],
    "employe":     ["paye","achete"],
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
        Commande.statut.in_(["paye","achete","expedie","arrive","recupere"])
    ).scalar() or 0

    base = {"total": total, "by_statut": by_statut}

    if role == "patron":
        base["encaisse"] = round(encaisse)
        cfg = db.query(Config).first()
        nb_articles_payes = db.query(func.sum(Commande.nb_articles)).filter(
            Commande.statut.in_(["paye","achete","expedie","arrive","recupere"])
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

    statuts_autorises = STATUTS_PAR_ROLE.get(role, ["paye", "achete"])

    if role in ("employe", "logisticien"):
        if statut and statut in statuts_autorises:
            q = q.filter(Commande.statut == statut)
        else:
            q = q.filter(Commande.statut.in_(statuts_autorises))
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

    result = []
    for c in cmds:
        d = serialize_cmd(c)
        if role in ("employe", "logisticien"):
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
    statuts_autorises = STATUTS_PAR_ROLE.get(role, ["paye", "achete"])
    if body.statut not in statuts_autorises:
        raise HTTPException(403, f"Statut '{body.statut}' non autorisé pour le rôle '{role}'")

    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")

    cmd.statut = body.statut
    if body.note_admin:
        cmd.note_admin = (cmd.note_admin or "") + " | " + body.note_admin

    # Calcul port réel — patron ET logisticien
    if body.poids_reel and role in ("patron", "logisticien"):
        cmd.poids_reel = body.poids_reel
        cfg = db.query(Config).first()
        from models import PortKg
        port = db.query(PortKg).filter(PortKg.pays == cmd.client_pays).first()
        port_kg = port.prix if port else 7000

        port_fcfa = round(port_kg * body.poids_reel)
        taux_local = (cfg.taux_gnf if cfg else 9500) if cmd.monnaie == "GNF" else 656
        port_local = round(port_fcfa * (taux_local / 656))

        cmd.total_local = (cmd.total_local or 0) + port_local
        note = f"Poids réel: {body.poids_reel}kg | Port: {port_local:,} {cmd.monnaie or 'FCFA'}"
        cmd.note_admin = (cmd.note_admin or "") + " | " + note

    db.commit()

    labels = {
        "paye":     "💰 Paiement confirmé ! On achète votre article.",
        "achete":   "🛍️ Article acheté ! Préparation en cours.",
        "expedie":  "✈️ Votre colis est en route depuis l'Europe !",
        "arrive":   "📦 Votre colis est arrivé ! Contactez-nous.",
        "paiement_refuse": "❌ Paiement non confirmé. Contactez-nous.",
    }
    if body.statut in labels:
        msg = labels[body.statut]
        notifier_client(db, cmd.ref, f"🛍️ Commande {cmd.ref}", msg)
        if body.statut == "paye":
            notifier_patron(db, "💰 Nouveau paiement reçu",
                f"{cmd.client_nom} · {cmd.ref} · {round(cmd.total_local or 0):,} {cmd.monnaie or 'FCFA'}", cmd.ref)
        # Patron notifié quand logisticien met à jour
        if role == "logisticien" and body.statut in ("achete", "expedie", "arrive"):
            notifier_patron(db, f"📦 Logistique — {STATUT_LABELS.get(body.statut, body.statut)}",
                f"{cmd.ref} · {cmd.client_nom} · {cmd.client_pays}", cmd.ref)

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
