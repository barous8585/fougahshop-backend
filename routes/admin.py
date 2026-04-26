from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
from typing import Optional
import json, csv, io, re
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

try:
    from wa_sender import envoyer_whatsapp, message_statut
except Exception:
    def envoyer_whatsapp(*a, **kw): return False
    def message_statut(*a, **kw): return ""

try:
    from date_estimee import calculer_date_estimee
except Exception:
    def calculer_date_estimee(*a, **kw): return ""

router = APIRouter(prefix="/api/admin", tags=["admin"])

STATUTS = ["en_attente_paiement","paye","achete","expedie","arrive","recupere","paiement_refuse","annulee"]
STATUT_LABELS = {
    "en_attente_paiement": "En attente de paiement",
    "paye":               "Payé",
    "achete":             "Acheté",
    "expedie":            "Expédié",
    "arrive":             "Arrivé",
    "recupere":           "Récupéré",
    "paiement_refuse":    "Paiement refusé",
    "annulee":            "Annulée",
}

STATUTS_PAR_ROLE = {
    "patron":      STATUTS,
    "logisticien": ["paye","achete","expedie","arrive","recupere"],
    "employe":     ["paye","achete"],
}


def ensure_archived_column(db: Session):
    """✅ À appeler au startup — pas à chaque requête."""
    from sqlalchemy import text
    try:
        db.execute(text("ALTER TABLE commandes ADD COLUMN IF NOT EXISTS archived BOOLEAN DEFAULT FALSE"))
        db.commit()
    except Exception:
        db.rollback()


def parse_cadeau(instructions: str) -> dict:
    """
    ✅ Extrait les infos cadeau depuis client_instructions.
    Format : '🎁 CADEAU POUR: Mamadou | Tel:+224... | Payeur: ...'
    Retourne un dict avec dest_nom, dest_tel, payeur_nom, payeur_tel
    ou None si ce n'est pas une commande cadeau.
    """
    if not instructions or "CADEAU POUR" not in instructions:
        return {}
    try:
        cadeau = {}
        # Extraire nom destinataire
        m = re.search(r"CADEAU POUR:\s*([^|]+)", instructions)
        if m: cadeau["dest_nom"] = m.group(1).strip()
        # Extraire tel destinataire
        m = re.search(r"Tel:([^\s|]+)", instructions)
        if m: cadeau["dest_tel"] = m.group(1).strip()
        # Extraire payeur
        m = re.search(r"Payeur:\s*([^|]+)\s*\(([^)]+)\)", instructions)
        if m:
            cadeau["payeur_nom"] = m.group(1).strip()
            cadeau["payeur_tel"] = m.group(2).strip()
        return cadeau
    except Exception:
        return {}


def serialize_cmd(c):
    instructions = c.client_instructions or ""
    cadeau_info  = parse_cadeau(instructions)

    # Nettoyer les instructions affichées (retirer le bloc cadeau technique)
    instructions_propres = re.sub(
        r"\s*\|?\s*🎁 CADEAU POUR:.*$", "", instructions, flags=re.DOTALL
    ).strip(" |")

    return {
        "id":                   c.id,
        "ref":                  c.ref,
        "statut":               c.statut,
        "statut_label":         STATUT_LABELS.get(c.statut, c.statut),
        "client_nom":           c.client_nom,
        "client_tel":           c.client_tel,
        "client_pays":          c.client_pays,
        "client_adresse":       c.client_adresse,
        "client_instructions":  instructions_propres,
        "operateur":            c.operateur,
        "monnaie":              c.monnaie,
        "total_euro":           c.total_euro,
        "total_local":          c.total_local,
        "poids_estime":         c.poids_estime,
        "poids_reel":           c.poids_reel,
        "nb_articles":          c.nb_articles,
        "articles":             json.loads(c.articles) if c.articles else [],
        "note_admin":           c.note_admin,
        "delai_livraison":      c.delai_livraison,
        "paiement_ref":         c.paiement_ref,
        "suivi_num":            getattr(c, "suivi_num", None),
        "motif_refus":          getattr(c, "motif_refus", None),
        "created_at":           c.created_at,
        # ✅ Champs cadeau dédiés — visibles clairement dans l'admin
        "is_cadeau":            bool(cadeau_info),
        "dest_nom":             cadeau_info.get("dest_nom"),
        "dest_tel":             cadeau_info.get("dest_tel"),
        "payeur_nom":           cadeau_info.get("payeur_nom"),
        "payeur_tel":           cadeau_info.get("payeur_tel"),
    }


def get_commission_palier(total_eu: float) -> int:
    if total_eu <= 50:   return 3500
    if total_eu <= 100:  return 5000
    if total_eu <= 200:  return 7000
    if total_eu <= 500:  return 12000
    return 20000


@router.get("/stats")
def stats(request: Request, db: Session = Depends(get_db),
          role: str = Depends(require_auth)):
    total    = db.query(Commande).count()
    by_statut = {s: db.query(Commande).filter(Commande.statut == s).count()
                 for s in STATUTS}
    encaisse = db.query(func.sum(Commande.total_local)).filter(
        Commande.statut.in_(["paye","achete","expedie","arrive","recupere"])
    ).scalar() or 0

    base = {"total": total, "by_statut": by_statut}

    if role == "patron":
        base["encaisse"] = round(encaisse)
        cmds_payees = db.query(Commande).filter(
            Commande.statut.in_(["paye","achete","expedie","arrive","recupere"])
        ).all()
        marge_totale = sum(
            get_commission_palier(c.total_euro or 0) for c in cmds_payees
        )
        base["marge_estimee"]        = round(marge_totale)
        base["nb_commandes_actives"] = len(cmds_payees)

    return base


@router.get("/commandes")
def liste_commandes(
    request: Request,
    statut: Optional[str]     = None,
    search: Optional[str]     = None,
    date_debut: Optional[str] = None,
    date_fin: Optional[str]   = None,
    db: Session = Depends(get_db),
    role: str   = Depends(require_auth),
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

    cmds   = q.order_by(Commande.created_at.desc()).all()
    result = []
    for c in cmds:
        d = serialize_cmd(c)
        if role in ("employe", "logisticien"):
            d.pop("total_local", None)
            d.pop("total_euro",  None)
            d.pop("monnaie",     None)
        result.append(d)
    return result


class StatutUpdate(BaseModel):
    statut:          str
    note_admin:      Optional[str]   = None
    poids_reel:      Optional[float] = None
    delai_livraison: Optional[str]   = None
    suivi_num:       Optional[str]   = None
    motif_refus:     Optional[str]   = None


@router.patch("/commandes/{ref}/statut")
def update_statut(
    ref: str, body: StatutUpdate,
    request: Request,
    db: Session = Depends(get_db),
    role: str   = Depends(require_auth),
):
    statuts_autorises = STATUTS_PAR_ROLE.get(role, ["paye", "achete"])
    if body.statut not in statuts_autorises:
        raise HTTPException(403, f"Statut '{body.statut}' non autorisé pour le rôle '{role}'")

    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")

    cmd.statut = body.statut

    if body.note_admin:
        note_existante = (cmd.note_admin or "")[-1500:]
        # ✅ Tronquer pour éviter dépassement de colonne
        cmd.note_admin = (note_existante + " | " + body.note_admin)[-2000:]

    if body.delai_livraison:
        cmd.delai_livraison = body.delai_livraison

    if body.suivi_num and hasattr(cmd, "suivi_num"):
        cmd.suivi_num = body.suivi_num

    if body.motif_refus and hasattr(cmd, "motif_refus"):
        cmd.motif_refus = body.motif_refus

    if body.poids_reel and role in ("patron", "logisticien"):
        cmd.poids_reel = body.poids_reel
        cfg  = db.query(Config).first()
        from models import PortKg
        port = db.query(PortKg).filter(PortKg.pays == cmd.client_pays).first()
        port_kg   = port.prix if port else 7000
        port_fcfa = round(port_kg * body.poids_reel)
        taux_local = (cfg.taux_gnf if cfg else 9500) if cmd.monnaie == "GNF" else 656
        port_local = round(port_fcfa * (taux_local / 656))
        cmd.total_local = (cmd.total_local or 0) + port_local
        note_port = f"Poids réel: {body.poids_reel}kg | Port: {port_local:,} {cmd.monnaie or 'FCFA'}"
        note_existante = (cmd.note_admin or "")[-1500:]
        cmd.note_admin  = (note_existante + " | " + note_port)[-2000:]

    db.commit()

    date_est = calculer_date_estimee(cmd.created_at, cmd.delai_livraison or "")

    # ✅ WhatsApp — détecter mode cadeau pour contacter aussi le destinataire
    cadeau_info = parse_cadeau(cmd.client_instructions or "")
    STATUTS_WA  = {"paye","achete","expedie","arrive","paiement_refuse","annulee"}

    if body.statut in STATUTS_WA and cmd.client_tel:
        wa_msg = message_statut(
            ref          = cmd.ref,
            statut       = body.statut,
            date_estimee = date_est,
            suivi_num    = getattr(cmd, "suivi_num", "") or "",
            motif        = body.motif_refus or "",
        )
        if wa_msg:
            # Toujours envoyer au payeur (client_tel)
            envoyer_whatsapp(cmd.client_tel, wa_msg)
            # ✅ Pour arrive et recupere — envoyer aussi au destinataire cadeau
            if cadeau_info.get("dest_tel") and body.statut in ("arrive", "recupere"):
                msg_dest = f"📦 Bonjour {cadeau_info.get('dest_nom', '')} !\n\n" \
                           f"Un colis vous est destiné — Réf: {cmd.ref}\n" \
                           f"Statut : {STATUT_LABELS.get(body.statut, body.statut)}\n" \
                           f"Veuillez contacter FougahShop pour le récupérer."
                envoyer_whatsapp(cadeau_info["dest_tel"], msg_dest)

    labels = {
        "paye":            "💰 Paiement confirmé ! On achète votre article.",
        "achete":          "🛍️ Article acheté ! Préparation en cours.",
        "expedie":         f"✈️ Votre colis est en route !{f' Arrivée estimée : {date_est}' if date_est else ''}",
        "arrive":          "📦 Votre colis est arrivé ! Contactez-nous.",
        "paiement_refuse": "❌ Paiement non confirmé. Contactez-nous.",
    }
    if body.statut in labels:
        msg = labels[body.statut]
        notifier_client(db, cmd.ref, f"🛍️ Commande {cmd.ref}", msg)
        if body.statut == "paye":
            notifier_patron(db, "💰 Nouveau paiement reçu",
                f"{cmd.client_nom} · {cmd.ref} · {round(cmd.total_local or 0):,} {cmd.monnaie or 'FCFA'}", cmd.ref)
        if role == "logisticien" and body.statut in ("achete","expedie","arrive"):
            notifier_patron(db, f"📦 Logistique — {STATUT_LABELS.get(body.statut, body.statut)}",
                f"{cmd.ref} · {cmd.client_nom} · {cmd.client_pays}", cmd.ref)

    return {"ref": cmd.ref, "statut": cmd.statut, "date_estimee": date_est}


# ── Archives ──────────────────────────────────────────────────
# ✅ ensure_archived_column() appelé au startup — pas ici

@router.post("/commandes/{ref}/archiver")
def archiver_commande(ref: str, request: Request,
                      db: Session = Depends(get_db),
                      role: str = Depends(require_auth)):
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    from sqlalchemy import text as sqlt
    try:
        db.execute(sqlt("UPDATE commandes SET archived = TRUE WHERE ref = :r"), {"r": ref})
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "Erreur archivage")
    return {"ok": True, "ref": ref}


@router.post("/commandes/{ref}/desarchiver")
def desarchiver_commande(ref: str, request: Request,
                         db: Session = Depends(get_db),
                         role: str = Depends(require_auth)):
    from sqlalchemy import text as sqlt
    try:
        db.execute(sqlt("UPDATE commandes SET archived = FALSE WHERE ref = :r"), {"r": ref})
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "Erreur désarchivage")
    return {"ok": True, "ref": ref}


@router.get("/commandes/archives")
def liste_archives(request: Request, db: Session = Depends(get_db),
                   role: str = Depends(require_auth)):
    from sqlalchemy import text as sqlt
    try:
        rows = db.execute(sqlt(
            "SELECT ref, client_nom, client_tel, client_pays, operateur, monnaie, "
            "total_local, total_euro, nb_articles, statut, created_at "
            "FROM commandes WHERE archived = TRUE ORDER BY created_at DESC"
        )).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []


@router.get("/export/csv")
def export_csv(request: Request, db: Session = Depends(get_db),
               role: str = Depends(require_patron)):
    cmds   = db.query(Commande).order_by(Commande.created_at.desc()).all()
    output = io.StringIO()
    w      = csv.writer(output)
    w.writerow([
        "Référence","Date","Client","Téléphone","Pays","Adresse",
        "Opérateur","Monnaie","Total €","Total local","Poids estimé",
        "Poids réel","Nb articles","Statut","Délai","N° Suivi","Notes",
        "Détail articles","Cadeau","Destinataire","Tel destinataire"
    ])
    for c in cmds:
        arts   = json.loads(c.articles) if c.articles else []
        detail = " | ".join([
            f"{a['nom']} x{a.get('qty',1)} ({a.get('poids',0.5)}kg)"
            for a in arts
        ])
        cadeau = parse_cadeau(c.client_instructions or "")
        w.writerow([
            c.ref,
            c.created_at.strftime("%d/%m/%Y %H:%M") if c.created_at else "",
            c.client_nom, c.client_tel, c.client_pays, c.client_adresse or "",
            c.operateur, c.monnaie, c.total_euro, c.total_local,
            c.poids_estime or "", c.poids_reel or "",
            c.nb_articles, STATUT_LABELS.get(c.statut, c.statut),
            c.delai_livraison or "",
            getattr(c, "suivi_num", "") or "",
            c.note_admin or "", detail,
            # ✅ Colonnes cadeau dans le CSV
            "Oui" if cadeau else "",
            cadeau.get("dest_nom", ""),
            cadeau.get("dest_tel", ""),
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=commandes_fougahshop.csv"}
    )
