from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from pydantic import BaseModel
from typing import Optional
import json, csv, io, re
from fastapi.responses import StreamingResponse
from database import get_db
from models import Commande, Config
from routes.auth import require_auth, require_patron, PWD_MIN_LENGTH, hash_password

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

CAT_TARIF_UNITE = {
    "smartphone":  "telephone",
    "baskets":     "chaussures",
    "bottes":      "chaussures",
    "cosmetique":  "parfum",
    "bijou":       "montre",
    "montre":      "montre",
    "parfum":      "parfum",
    "chaussures":  "chaussures",
}

# ── Point 3 : taille de page par défaut ──────────────────────
DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE     = 200  # garde-fou — jamais plus de 200 en un appel


def ensure_archived_column(db: Session):
    try:
        db.execute(text("ALTER TABLE commandes ADD COLUMN IF NOT EXISTS archived BOOLEAN DEFAULT FALSE"))
        db.commit()
    except Exception:
        db.rollback()


def parse_cadeau(instructions: str) -> dict:
    if not instructions or "CADEAU POUR" not in instructions:
        return {}
    try:
        cadeau = {}
        m = re.search(r"CADEAU POUR:\s*([^|]+)", instructions)
        if m: cadeau["dest_nom"] = m.group(1).strip()
        m = re.search(r"Tel:([^\s|]+)", instructions)
        if m: cadeau["dest_tel"] = m.group(1).strip()
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

    instructions_propres = re.sub(
        r"\s*\|?\s*🎁 CADEAU POUR:.*$", "", instructions, flags=re.DOTALL
    ).strip(" |")

    try:
        articles = json.loads(c.articles) if c.articles else []
    except Exception:
        articles = []

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
        "articles":             articles,
        "note_admin":           c.note_admin,
        "delai_livraison":      c.delai_livraison,
        "paiement_ref":         c.paiement_ref,
        "suivi_num":            c.suivi_num,
        "motif_refus":          c.motif_refus,
        "created_at":           c.created_at,
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


# ── Point 5 : endpoint finances dédié — remplace loadFinances() + renderCharts() ──

@router.get("/stats/finances")
def stats_finances(request: Request, db: Session = Depends(get_db),
                   role: str = Depends(require_patron)):
    """
    Calcule côté serveur toutes les données financières dont le frontend a besoin.
    Remplace les appels /commandes?limit=0 dans loadFinances() et renderCharts().
    """
    from datetime import datetime, date
    import calendar

    now          = datetime.utcnow()
    mois_courant = now.strftime("%Y-%m")
    statuts_payes = ["paye","achete","expedie","arrive","recupere"]

    try:
        cfg = db.query(Config).first()
        taux_gnf = (cfg.taux_gnf if cfg else None) or 9500
    except Exception:
        taux_gnf = 9500

    def comm_en_fcfa(total_eu, monnaie):
        comm = get_commission_palier(float(total_eu or 0))
        if monnaie == "GNF":
            comm = round(comm * (taux_gnf / 656))
        return comm

    def to_fcfa(comm, monnaie):
        if monnaie == "GNF":
            return round(comm * 656 / taux_gnf)
        return comm

    # ── Toutes les commandes payées (pour les graphiques 6 mois) ──
    rows = db.execute(text("""
        SELECT ref, statut, monnaie, total_euro, total_local,
               TO_CHAR(created_at, 'YYYY-MM') AS mois,
               TO_CHAR(created_at, 'YYYY-MM-DD') AS jour,
               client_pays
        FROM commandes
        WHERE statut = ANY(:statuts)
        ORDER BY created_at DESC
    """), {"statuts": statuts_payes}).mappings().all()

    # ── KPIs mois courant ─────────────────────────────────────
    cmds_mois = [r for r in rows if r["mois"] == mois_courant]
    comm_mois_fcfa  = 0
    volume_eu       = 0.0
    comm_encaissee  = 0
    comm_attente    = 0
    total_enc_fcfa  = 0

    for r in cmds_mois:
        monnaie  = r["monnaie"] or "FCFA"
        comm     = comm_en_fcfa(r["total_euro"], monnaie)
        comm_f   = to_fcfa(comm, monnaie)
        comm_mois_fcfa += comm_f
        volume_eu      += float(r["total_euro"] or 0)
        tl = float(r["total_local"] or 0)
        total_enc_fcfa += round(tl * 656 / taux_gnf) if monnaie == "GNF" else tl
        if r["statut"] == "recupere":
            comm_encaissee += comm_f
        else:
            comm_attente   += comm_f

    # ── Commissions sur 6 mois ────────────────────────────────
    mois_6 = []
    for i in range(5, -1, -1):
        m = now.month - i
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        key   = f"{y}-{m:02d}"
        label = date(y, m, 1).strftime("%b")
        mois_6.append({"key": key, "label": label, "comm": 0, "volume": 0.0})

    mois_map = {m["key"]: m for m in mois_6}
    for r in rows:
        k = r["mois"]
        if k in mois_map:
            monnaie = r["monnaie"] or "FCFA"
            comm_f  = to_fcfa(comm_en_fcfa(r["total_euro"], monnaie), monnaie)
            mois_map[k]["comm"]   += comm_f
            mois_map[k]["volume"] += float(r["total_euro"] or 0)

    # ── Top 5 pays mois courant ───────────────────────────────
    par_pays = {}
    for r in cmds_mois:
        p = r["client_pays"] or "Inconnu"
        if p not in par_pays:
            par_pays[p] = {"nb": 0, "comm": 0}
        monnaie = r["monnaie"] or "FCFA"
        par_pays[p]["nb"]   += 1
        par_pays[p]["comm"] += to_fcfa(comm_en_fcfa(r["total_euro"], monnaie), monnaie)

    top_pays = sorted(par_pays.items(), key=lambda x: x[1]["comm"], reverse=True)[:5]

    return {
        # KPIs mois
        "comm_mois_fcfa":   comm_mois_fcfa,
        "volume_eu":        round(volume_eu, 2),
        "nb_mois":          len(cmds_mois),
        "comm_encaissee":   comm_encaissee,
        "comm_attente":     comm_attente,
        "total_enc_fcfa":   round(total_enc_fcfa),
        # Graphique 6 mois
        "mois_6": [
            {"key": m["key"], "label": m["label"],
             "comm": m["comm"], "volume": round(m["volume"], 2)}
            for m in mois_6
        ],
        # Top pays
        "top_pays": [
            {"pays": p, "nb": d["nb"], "comm": d["comm"]}
            for p, d in top_pays
        ],
        "taux_gnf": taux_gnf,
    }


# ── Point 3 : /commandes avec pagination ─────────────────────

@router.get("/commandes")
def liste_commandes(
    request:    Request,
    statut:     Optional[str] = None,
    search:     Optional[str] = None,
    date_debut: Optional[str] = None,
    date_fin:   Optional[str] = None,
    # Pagination — page commence à 1, limit = nombre de résultats par page
    # page=0 ou limit=0 → comportement legacy : retourne TOUT (pour loadFinances/renderCharts)
    page:       int = Query(default=1,  ge=0),
    limit:      int = Query(default=DEFAULT_PAGE_SIZE, ge=0, le=MAX_PAGE_SIZE),
    db:         Session = Depends(get_db),
    role:       str     = Depends(require_auth),
):
    """
    Retourne les commandes avec pagination.

    Réponse paginée (page >= 1 et limit > 0) :
        { "total": int, "page": int, "limit": int, "pages": int, "commandes": [...] }

    Réponse legacy (page=0 OU limit=0) :
        [ ...liste complète... ]
    Le mode legacy est conservé pour loadFinances() et renderCharts() dans index.html
    qui ont besoin de toutes les commandes pour faire les calculs côté client.
    À terme, ces calculs devraient migrer vers un endpoint /stats/finances dédié.
    """
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

    q = q.order_by(Commande.created_at.desc())

    # ── Mode legacy : pas de pagination (page=0 ou limit=0) ──
    if page == 0 or limit == 0:
        cmds   = q.all()
        result = []
        for c in cmds:
            d = serialize_cmd(c)
            if role in ("employe", "logisticien"):
                d.pop("total_local", None)
                d.pop("total_euro",  None)
                d.pop("monnaie",     None)
            result.append(d)
        return result

    # ── Mode paginé ───────────────────────────────────────────
    total_count = q.count()
    offset      = (page - 1) * limit
    cmds        = q.offset(offset).limit(limit).all()

    result = []
    for c in cmds:
        d = serialize_cmd(c)
        if role in ("employe", "logisticien"):
            d.pop("total_local", None)
            d.pop("total_euro",  None)
            d.pop("monnaie",     None)
        result.append(d)

    return {
        "total":      total_count,
        "page":       page,
        "limit":      limit,
        "pages":      max(1, -(-total_count // limit)),  # ceil division
        "commandes":  result,
    }


class StatutUpdate(BaseModel):
    statut:          str
    note_admin:      Optional[str]   = None
    poids_reel:      Optional[float] = None
    delai_livraison: Optional[str]   = None
    suivi_num:       Optional[str]   = None
    motif_refus:     Optional[str]   = None
    port_categorie:  Optional[str]   = None


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
    port_local = 0

    if body.note_admin:
        note_existante = (cmd.note_admin or "")[-1500:]
        cmd.note_admin = (note_existante + " | " + body.note_admin)[-2000:]

    if body.delai_livraison:
        cmd.delai_livraison = body.delai_livraison

    if body.suivi_num:
        cmd.suivi_num = body.suivi_num

    if body.motif_refus:
        cmd.motif_refus = body.motif_refus

    if body.poids_reel and role in ("patron", "logisticien"):
        cmd.poids_reel = body.poids_reel
        cfg  = db.query(Config).first()
        from models import PortKg
        port = db.query(PortKg).filter(PortKg.pays == cmd.client_pays).first()
        port_kg = port.prix if port else 7000

        port_fcfa = 0
        try:
            tarifs_row = db.execute(text(
                "SELECT tarifs_unite FROM configs WHERE id = :id"
            ), {"id": cfg.id if cfg else 1}).mappings().first()
            tarifs_unite = json.loads(tarifs_row["tarifs_unite"]) if tarifs_row and tarifs_row.get("tarifs_unite") else []

            if body.port_categorie and tarifs_unite:
                def match_cat(nom, cat):
                    n = (nom or "").lower()
                    if cat == "iphone":     return "iphone" in n or "haut de gamme" in n
                    if cat == "telephone":  return "phone" in n and "iphone" not in n
                    if cat == "parfum":     return "parfum" in n
                    if cat == "montre":     return "montre" in n or "bijou" in n
                    if cat == "chaussures": return "chaussure" in n
                    return False
                for tu in tarifs_unite:
                    if match_cat(tu.get("nom"), body.port_categorie):
                        taux_ch   = cfg.taux_change or 660
                        port_fcfa = round(float(tu.get("prix", 0)) * taux_ch)
                        break
                if not port_fcfa:
                    port_fcfa = round(port_kg * body.poids_reel)

            else:
                try:
                    articles = json.loads(cmd.articles) if cmd.articles else []
                except Exception:
                    articles = []

                for art in articles:
                    categorie = (art.get("categorie") or "").lower().strip()
                    prix_eu   = float(art.get("prix_eu") or 0)
                    qty       = int(art.get("qty") or 1)

                    tarif_unite_trouve = None
                    nom_tarif = CAT_TARIF_UNITE.get(categorie)

                    if categorie == "smartphone":
                        for tu in tarifs_unite:
                            nom_tu = (tu.get("nom") or "").lower()
                            note   = (tu.get("note") or "").replace(" ", "")
                            if "iphone" in nom_tu or "haut de gamme" in nom_tu:
                                if note.startswith(">"):
                                    try:
                                        seuil = float(note.replace(">","").replace("€",""))
                                        if prix_eu > seuil:
                                            tarif_unite_trouve = tu
                                            break
                                    except Exception:
                                        pass
                            elif "phone" in nom_tu and "iphone" not in nom_tu:
                                if not tarif_unite_trouve:
                                    tarif_unite_trouve = tu
                        if not tarif_unite_trouve:
                            for tu in tarifs_unite:
                                if "phone" in (tu.get("nom") or "").lower():
                                    tarif_unite_trouve = tu
                                    break

                    elif nom_tarif and tarifs_unite:
                        for tu in tarifs_unite:
                            if nom_tarif in (tu.get("nom") or "").lower():
                                tarif_unite_trouve = tu
                                break

                    if not tarif_unite_trouve and tarifs_unite:
                        nom_art = (art.get("nom") or "").lower()
                        for tu in tarifs_unite:
                            nom_tu = (tu.get("nom") or "").lower()
                            if nom_tu and nom_tu in nom_art:
                                tarif_unite_trouve = tu
                                break

                    if tarif_unite_trouve:
                        taux_ch = cfg.taux_change if cfg else 660
                        port_fcfa += round(float(tarif_unite_trouve.get("prix", 0)) * taux_ch * qty)
                    else:
                        poids_art = float(art.get("poids") or 0.5) * qty
                        port_fcfa += round(port_kg * poids_art)

        except Exception as e:
            print(f"[port] Erreur calcul tarif: {e}")
            port_fcfa = round(port_kg * body.poids_reel)

        if port_fcfa == 0:
            port_fcfa = round(port_kg * body.poids_reel)

        taux_local = (cfg.taux_gnf if cfg else 9500) if cmd.monnaie == "GNF" else 656
        port_local = round(port_fcfa * (taux_local / 656))
        cmd.total_local = (cmd.total_local or 0) + port_local
        note_port = f"Poids réel: {body.poids_reel}kg | Port: {port_local:,} {cmd.monnaie or 'FCFA'}"
        note_existante = (cmd.note_admin or "")[-1500:]
        cmd.note_admin  = (note_existante + " | " + note_port)[-2000:]

    db.commit()

    date_est = calculer_date_estimee(cmd.created_at, cmd.delai_livraison or "")

    cadeau_info = parse_cadeau(cmd.client_instructions or "")
    STATUTS_WA  = {"paye","achete","expedie","arrive","paiement_refuse","annulee"}

    if body.statut in STATUTS_WA and cmd.client_tel:
        livraison_info = {}
        try:
            livr_row = db.execute(text(
                "SELECT livraison_domicile FROM configs WHERE id=1 LIMIT 1"
            )).fetchone()
            if livr_row and livr_row[0]:
                livraison_info = json.loads(livr_row[0])
        except Exception:
            pass

        wa_msg = message_statut(
            ref            = cmd.ref,
            statut         = body.statut,
            date_estimee   = date_est,
            suivi_num      = cmd.suivi_num or "",
            motif          = body.motif_refus or "",
            port_local     = port_local if body.poids_reel else 0,
            monnaie        = cmd.monnaie or "FCFA",
            livraison_info = livraison_info,
        )
        if wa_msg:
            envoyer_whatsapp(cmd.client_tel, wa_msg)
            if cadeau_info.get("dest_tel") and body.statut in ("arrive", "recupere"):
                msg_dest = (
                    f"📦 Bonjour {cadeau_info.get('dest_nom', '')} !\n\n"
                    f"Un colis vous est destiné — Réf: {cmd.ref}\n"
                    f"Statut : {STATUT_LABELS.get(body.statut, body.statut)}\n"
                    f"Veuillez contacter FougahShop pour le récupérer."
                )
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

    # ── OneDrive : mise à jour du statut ─────────────────────
    try:
        from routes.onedrive import mettre_a_jour_statut
        import asyncio
        asyncio.create_task(mettre_a_jour_statut(
            ref=ref,
            nouveau_statut=body.statut,
            frais_port=port_local if port_local else None
        ))
    except Exception as e:
        print(f"[OneDrive] Erreur sync statut: {e}")

    return {"ref": cmd.ref, "statut": cmd.statut, "date_estimee": date_est}


class EmployeCreate(BaseModel):
    nom:  str
    pwd:  str
    role: Optional[str] = "employe"


@router.post("/employes")
def creer_employe(body: EmployeCreate, request: Request,
                  db: Session = Depends(get_db),
                  role: str = Depends(require_patron)):
    """Point 2a : création employé avec validation mot de passe min 8 chars."""
    nom = (body.nom or "").strip()
    pwd = (body.pwd or "").strip()

    if not nom:
        raise HTTPException(400, "Nom requis")
    if not pwd or len(pwd) < PWD_MIN_LENGTH:
        raise HTTPException(
            400,
            f"Mot de passe trop court (minimum {PWD_MIN_LENGTH} caractères)"
        )
    if body.role not in ("employe", "logisticien"):
        raise HTTPException(400, "Rôle invalide (employe ou logisticien)")

    from models import Employe
    emp = Employe(nom=nom, pwd=hash_password(pwd), role=body.role, actif=True)
    db.add(emp)
    db.commit()
    db.refresh(emp)
    return {"id": emp.id, "nom": emp.nom, "role": emp.role}


# ── Archives ──────────────────────────────────────────────────

@router.post("/commandes/{ref}/archiver")
def archiver_commande(ref: str, request: Request,
                      db: Session = Depends(get_db),
                      role: str = Depends(require_auth)):
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    try:
        db.execute(text("UPDATE commandes SET archived = TRUE WHERE ref = :r"), {"r": ref})
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "Erreur archivage")
    return {"ok": True, "ref": ref}


@router.post("/commandes/{ref}/desarchiver")
def desarchiver_commande(ref: str, request: Request,
                         db: Session = Depends(get_db),
                         role: str = Depends(require_auth)):
    try:
        db.execute(text("UPDATE commandes SET archived = FALSE WHERE ref = :r"), {"r": ref})
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "Erreur désarchivage")
    return {"ok": True, "ref": ref}


@router.get("/commandes/archives")
def liste_archives(request: Request, db: Session = Depends(get_db),
                   role: str = Depends(require_auth)):
    try:
        rows = db.execute(text(
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
    """
    Point 4 : Export CSV en streaming par chunks de 200 commandes.
    Ne charge plus toutes les commandes en RAM d'un coup.
    """
    CHUNK = 200

    def generate():
        output = io.StringIO()
        w = csv.writer(output)
        w.writerow([
            "Référence","Date","Client","Téléphone","Pays","Adresse",
            "Opérateur","Monnaie","Total €","Total local","Poids estimé",
            "Poids réel","Nb articles","Statut","Délai","N° Suivi","Notes",
            "Détail articles","Cadeau","Destinataire","Tel destinataire"
        ])
        yield output.getvalue()

        offset = 0
        while True:
            cmds = db.query(Commande)\
                     .order_by(Commande.created_at.desc())\
                     .offset(offset).limit(CHUNK).all()
            if not cmds:
                break
            for c in cmds:
                output = io.StringIO()
                w = csv.writer(output)
                try:
                    arts = json.loads(c.articles) if c.articles else []
                except Exception:
                    arts = []
                detail = " | ".join([
                    f"{a.get('nom','?')} x{a.get('qty',1)} ({a.get('poids',0.5)}kg)"
                    for a in arts
                ])
                cadeau = parse_cadeau(c.client_instructions or "")
                note_export = re.sub(r'\[PRIVE\].*', '', c.note_admin or '').strip(' |')
                w.writerow([
                    c.ref,
                    c.created_at.strftime("%d/%m/%Y %H:%M") if c.created_at else "",
                    c.client_nom, c.client_tel, c.client_pays, c.client_adresse or "",
                    c.operateur, c.monnaie, c.total_euro, c.total_local,
                    c.poids_estime or "", c.poids_reel or "",
                    c.nb_articles, STATUT_LABELS.get(c.statut, c.statut),
                    c.delai_livraison or "", c.suivi_num or "",
                    note_export, detail,
                    "Oui" if cadeau else "",
                    cadeau.get("dest_nom", ""),
                    cadeau.get("dest_tel", ""),
                ])
                yield output.getvalue()
            offset += CHUNK

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=commandes_fougahshop.csv"}
    )
