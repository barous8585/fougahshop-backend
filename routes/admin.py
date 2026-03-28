from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional, Dict, Any
import json, csv, io
from fastapi.responses import StreamingResponse
from database import get_db
from models import Commande, Config, PortKg
from routes.auth import require_auth, require_patron

router = APIRouter(prefix="/api/admin", tags=["admin"])

STATUTS = ["en_attente_paiement","paye","achete","expedie","arrive","paiement_refuse"]
STATUT_LABELS = {
    "en_attente_paiement": "En attente de paiement",
    "paye": "Payé", "achete": "Acheté",
    "expedie": "Expédié", "arrive": "Arrivé",
    "paiement_refuse": "Paiement refusé",
}

def serialize_cmd(c, role="patron"):
    d = {
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
        "created_at": str(c.created_at),
    }
    if role == "employe":
        d.pop("total_local", None)
        d.pop("total_euro", None)
    return d

@router.get("/stats")
def stats(request: Request, db: Session = Depends(get_db), role: str = Depends(require_auth)):
    total = db.query(Commande).count()
    by_statut = {s: db.query(Commande).filter(Commande.statut == s).count() for s in STATUTS}
    base = {"total": total, "by_statut": by_statut}
    if role == "patron":
        encaisse = db.query(func.sum(Commande.total_local)).filter(
            Commande.statut.in_(["paye","achete","expedie","arrive"])
        ).scalar() or 0
        cfg = db.query(Config).first()
        nb_arts = db.query(func.sum(Commande.nb_articles)).filter(
            Commande.statut.in_(["paye","achete","expedie","arrive"])
        ).scalar() or 0
        base["encaisse"] = round(encaisse)
        base["marge_estimee"] = round((cfg.commission if cfg else 3500) * nb_arts)
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
    return [serialize_cmd(c, role) for c in q.order_by(Commande.created_at.desc()).all()]

@router.patch("/commandes/{ref}/statut")
def update_statut(ref: str, body: Dict[str, Any], request: Request,
                  db: Session = Depends(get_db), role: str = Depends(require_auth)):
    new_statut = str(body.get("statut", ""))
    if new_statut not in STATUTS:
        raise HTTPException(400, "Statut invalide")
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    cmd.statut = new_statut
    note = body.get("note_admin", "")
    if note:
        cmd.note_admin = (cmd.note_admin or "") + " | " + str(note)
    if body.get("poids_reel") and role == "patron":
        poids_reel = float(body["poids_reel"])
        cmd.poids_reel = poids_reel
        cfg = db.query(Config).first()
        port = db.query(PortKg).filter(PortKg.pays == cmd.client_pays).first()
        port_kg = port.prix if port else 7000
        articles = json.loads(cmd.articles) if cmd.articles else []
        nouveau_total = 0
        poids_par_art = poids_reel / max(cmd.nb_articles, 1)
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
            cmd.note_admin = (cmd.note_admin or "") + f" | Poids réel: {poids_reel}kg | Ajustement: {sign}{round(diff):,} {cmd.monnaie}"
    db.commit()
    return {"ref": cmd.ref, "statut": cmd.statut}

@router.get("/export/csv")
def export_csv(request: Request, db: Session = Depends(get_db), role: str = Depends(require_patron)):
    cmds = db.query(Commande).order_by(Commande.created_at.desc()).all()
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["Référence","Date","Client","Téléphone","Pays","Adresse",
                "Opérateur","Monnaie","Total €","Total local","Poids estimé",
                "Poids réel","Nb articles","Statut","Délai","Notes","Détail articles"])
    for c in cmds:
        arts = json.loads(c.articles) if c.articles else []
        detail = " | ".join([f"{a['nom']} x{a.get('qty',1)} ({a.get('poids',0.5)}kg)" for a in arts])
        w.writerow([c.ref, str(c.created_at)[:16] if c.created_at else "",
                    c.client_nom, c.client_tel, c.client_pays, c.client_adresse or "",
                    c.operateur, c.monnaie, c.total_euro, c.total_local,
                    c.poids_estime or "", c.poids_reel or "", c.nb_articles,
                    STATUT_LABELS.get(c.statut, c.statut), c.delai_livraison or "",
                    c.note_admin or "", detail])
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=commandes_fougahshop.csv"})
