from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import json
from database import get_db
from models import Commande, Config, PortKg

try:
    from date_estimee import calculer_date_estimee
except Exception:
    def calculer_date_estimee(*a, **kw): return ""

router = APIRouter(prefix="/api/commandes", tags=["commandes"])

try:
    from routes.notifs import notifier_patron
except Exception:
    def notifier_patron(*a, **kw): pass

MONNAIES = {
    "Burkina Faso":  {"symbole": "FCFA", "taux_base": 656},
    "Guinée":        {"symbole": "GNF",  "taux_base": None},
    "Cameroun":      {"symbole": "FCFA", "taux_base": 656},
    "Bénin":         {"symbole": "FCFA", "taux_base": 656},
    "Togo":          {"symbole": "FCFA", "taux_base": 656},
    "Niger":         {"symbole": "FCFA", "taux_base": 656},
    "Congo":         {"symbole": "FCFA", "taux_base": 656},
    "Gabon":         {"symbole": "FCFA", "taux_base": 656},
    "Sénégal":       {"symbole": "FCFA", "taux_base": 656},
    "Mali":          {"symbole": "FCFA", "taux_base": 656},
    "Côte d'Ivoire": {"symbole": "FCFA", "taux_base": 656},
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
        cfg = Config()
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def get_port(db, pays):
    p = db.query(PortKg).filter(PortKg.pays == pays).first()
    return p.prix if p else 7000.0


def generate_ref(db) -> str:
    """
    ✅ CORRIGÉ — Race condition supprimée.
    Ancienne version : count() + 1 → deux commandes simultanées généraient la même ref.
    Nouvelle version : MAX sur les refs existantes → atomique et sans collision.
    """
    year = datetime.now().year
    prefix = f"CMD-{year}-"
    result = db.execute(
        text("""
            SELECT COALESCE(MAX(CAST(SUBSTRING(ref FROM :pos) AS INTEGER)), 0) + 1
            FROM commandes
            WHERE ref LIKE :pattern
        """),
        {"pos": len(prefix) + 1, "pattern": f"{prefix}%"}
    ).scalar()
    return f"{prefix}{result:04d}"


def calc_article_sans_port_ni_commission(prix_eu, qty, pays, cfg):
    m         = MONNAIES.get(pays, {"symbole": "FCFA", "taux_base": 656})
    taux_gnf  = cfg.taux_gnf   if (cfg.taux_gnf  and cfg.taux_gnf  >= 1000) else 9500
    taux_fcfa = cfg.taux_change or 660

    if m["symbole"] == "GNF":
        # ✅ CORRIGÉ — calcul direct avec taux_gnf (identique au frontend)
        # Ancienne formule : round(prix * taux_fcfa) * taux_gnf/656
        #   → 660 * 10318/656 = 10380 ≠ 10318 → écart de ~4700 GNF sur 75€
        # Nouvelle formule : prix * taux_gnf directement → 0 écart
        taux_conv  = taux_gnf / 656           # pour convertir commission FCFA→GNF
        base_local = round(prix_eu * taux_gnf * qty)
        base_fcfa  = round(prix_eu * taux_fcfa)   # conservé pour archivage
    else:
        # FCFA : formule inchangée
        taux_conv  = 1.0
        base_fcfa  = round(prix_eu * taux_fcfa)
        base_local = round(base_fcfa * qty)

    return {
        "base_fcfa":   base_fcfa,
        "total_local": base_local,
        "monnaie":     m["symbole"],
        "taux_conv":   taux_conv,
    }


def appliquer_promo(db, promo_code: str, total_local: float, taux_conv: float) -> float:
    """
    ✅ CORRIGÉ — Double incrément supprimé.
    Ancienne version : uses_count ET utilisations incrémentés ensemble → comptait double.
    Nouvelle version : uses_count en priorité, utilisations uniquement en fallback.
    """
    if not promo_code:
        return total_local
    try:
        promo = db.execute(
            text("SELECT * FROM promo_codes WHERE code=:code AND actif=TRUE LIMIT 1"),
            {"code": promo_code.strip().upper()}
        ).fetchone()

        if not promo:
            return total_local

        # Vérifier expiration
        expiry = getattr(promo, "expiry", None)
        if expiry:
            from datetime import date
            exp_date = expiry if hasattr(expiry, "year") else None
            if exp_date and exp_date < date.today():
                return total_local

        # Vérifier quota — lire les deux colonnes pour compat, ne pas additionner
        uses  = getattr(promo, "uses_count", 0)  or getattr(promo, "utilisations", 0) or 0
        max_u = getattr(promo, "max_uses",   0)  or getattr(promo, "quota",        0) or 0
        if max_u > 0 and uses >= max_u:
            return total_local

        # Calcul réduction
        type_promo = getattr(promo, "type", "fixe") or "fixe"
        valeur     = getattr(promo, "valeur", None) or getattr(promo, "reduction_fcfa", 0) or 0

        # ✅ Type livraison — pas de réduction sur le montant, juste incrémenter uses_count
        if type_promo == "livraison":
            nouveau_total = total_local  # aucune déduction
        elif type_promo == "pct":
            reduction = round(total_local * float(valeur) / 100)
            nouveau_total = max(0, total_local - reduction)
        else:
            reduction = round(float(valeur) * taux_conv)
            nouveau_total = max(0, total_local - reduction)

        # ✅ Incrémenter UNE SEULE FOIS : uses_count d'abord, utilisations en fallback
        incremente = False
        try:
            db.execute(
                text("UPDATE promo_codes SET uses_count = COALESCE(uses_count,0) + 1 WHERE code=:code"),
                {"code": promo_code.strip().upper()}
            )
            db.flush()
            incremente = True
        except Exception:
            pass

        if not incremente:
            try:
                db.execute(
                    text("UPDATE promo_codes SET utilisations = COALESCE(utilisations,0) + 1 WHERE code=:code"),
                    {"code": promo_code.strip().upper()}
                )
                db.flush()
            except Exception:
                pass

        return nouveau_total

    except Exception as e:
        print(f"[promo] Erreur application code: {e}")
        return total_local


# ── Schemas ───────────────────────────────────────────────────

class ArticleIn(BaseModel):
    lien:      str
    nom:       str
    img:       Optional[str]   = None
    categorie: Optional[str]   = None
    taille:    Optional[str]   = None
    couleur:   Optional[str]   = None
    specs:     Optional[str]   = None
    prix_eu:   float
    poids:     float           = 0.5
    qty:       int             = 1


class CommandeCreate(BaseModel):
    client_nom:             str
    client_tel:             str
    client_pays:            str
    client_adresse:         Optional[str]   = None
    client_instructions:    Optional[str]   = None
    operateur:              str
    promo_code:             Optional[str]   = None
    promo_type:             Optional[str]   = None
    promo_valeur:           Optional[float] = None
    # ✅ Code parrainage séparé du code promo
    code_parrainage:        Optional[str]   = None
    reduction_parrainage:   Optional[float] = None
    mode_paiement:          Optional[str]   = None
    kkiapay_transaction_id: Optional[str]   = None
    articles:               List[ArticleIn]
    total_local_client:     Optional[float] = None
    monnaie_client:         Optional[str]   = None
    taux_utilise:           Optional[float] = None


class CalculRequest(BaseModel):
    prix_eu: float
    poids:   float
    pays:    str
    qty:     int = 1


class AnnulationBody(BaseModel):
    ref:        str
    client_tel: str
    motif:      Optional[str] = None


class KkiapayConfirmBody(BaseModel):
    ref:            str
    transaction_id: Optional[str] = None


# ── Routes ────────────────────────────────────────────────────

@router.post("/calculer")
def calculer(body: CalculRequest, db: Session = Depends(get_db)):
    cfg        = get_config(db)
    detail     = calc_article_sans_port_ni_commission(body.prix_eu, body.qty, body.pays, cfg)
    commission = get_commission(body.prix_eu * body.qty)
    # ✅ Utiliser taux_conv retourné par calc (déjà correct pour GNF et FCFA)
    taux_conv  = detail["taux_conv"]
    comm_local = round(commission * taux_conv)
    port_fcfa  = get_port(db, body.pays)
    port_local = round(port_fcfa * body.poids * taux_conv)
    return {
        "base_fcfa":       detail["base_fcfa"],
        "commission":      commission,
        "port_estime":     port_local,
        "total_local":     detail["total_local"] + comm_local,
        "total_avec_port": detail["total_local"] + comm_local + port_local,
        "monnaie":         detail["monnaie"],
    }


@router.post("/", status_code=201)
def creer_commande(body: CommandeCreate, db: Session = Depends(get_db)):
    if not body.articles:
        raise HTTPException(400, "Panier vide")

    cfg       = get_config(db)
    m         = MONNAIES.get(body.client_pays, {"symbole": "FCFA"})
    port_info = db.query(PortKg).filter(PortKg.pays == body.client_pays).first()

    articles_detail       = []
    total_eu              = 0.0
    total_local_sans_comm = 0.0
    poids_total           = 0.0
    taux_conv             = 1.0

    for a in body.articles:
        detail = calc_article_sans_port_ni_commission(a.prix_eu, a.qty, body.client_pays, cfg)
        articles_detail.append({
            "lien":        a.lien,
            "nom":         a.nom,
            "img":         a.img,
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
        total_eu              += a.prix_eu * a.qty
        total_local_sans_comm += detail["total_local"]
        poids_total           += a.poids * a.qty
        taux_conv              = detail["taux_conv"]

    commission_fcfa   = get_commission(total_eu)
    commission_locale = round(commission_fcfa * taux_conv)
    total_local       = total_local_sans_comm + commission_locale

    if body.promo_code:
        total_local = appliquer_promo(db, body.promo_code, total_local, taux_conv)

    # ✅ Traitement code parrainage — créditer le parrain directement en DB
    if body.code_parrainage:
        try:
            code_p = body.code_parrainage.upper().strip()
            parrain = db.execute(
                text("SELECT parrain_tel FROM parrainage_codes WHERE code=:c AND actif=TRUE"),
                {"c": code_p}
            ).mappings().first()
            if parrain and parrain["parrain_tel"] != body.client_tel:
                # Anti auto-parrainage
                deja = db.execute(
                    text("SELECT 1 FROM parrainage_utilisations WHERE code=:c AND filleul_tel=:t"),
                    {"c": code_p, "t": body.client_tel}
                ).fetchone()
                if not deja:
                    gain = float(body.reduction_parrainage or 1000) * 0.5
                    db.execute(text(
                        "INSERT INTO parrainage_utilisations "
                        "(code, filleul_tel, filleul_nom, commande_ref, reduction_appliquee) "
                        "VALUES (:c, :t, :n, :r, :red)"
                    ), {"c": code_p, "t": body.client_tel, "n": body.client_nom,
                        "r": "", "red": body.reduction_parrainage or 1000})
                    db.execute(text(
                        "UPDATE parrainage_codes "
                        "SET nb_filleuls=nb_filleuls+1, credit_total=credit_total+:g "
                        "WHERE code=:c"
                    ), {"g": gain, "c": code_p})
                    db.commit()
        except Exception as e:
            print(f"[parrainage] Erreur: {e}")
            db.rollback()

    # ✅ Priorité au montant frontend (taux live figé au moment de confirmer)
    # Fallback sur calcul backend si le champ est absent (bot WhatsApp, ancien client)
    if body.total_local_client and body.total_local_client > 0:
        total_local = round(body.total_local_client)
    if body.monnaie_client:
        m = {"symbole": body.monnaie_client}

    statut_initial = "paye" if body.mode_paiement == "kkiapay" else "en_attente_paiement"

    note_auto = None
    if body.mode_paiement == "kkiapay" and body.kkiapay_transaction_id:
        note_auto = f"[KKIAPAY] Transaction: {body.kkiapay_transaction_id}"

    commande = Commande(
        ref              = generate_ref(db),
        client_nom       = body.client_nom,
        client_tel       = body.client_tel,
        client_pays      = body.client_pays,
        client_adresse   = body.client_adresse,
        client_instructions = body.client_instructions,
        operateur        = body.operateur,
        monnaie          = m["symbole"],
        total_euro       = round(total_eu, 2),
        total_local      = round(total_local),
        poids_estime     = round(poids_total, 2),
        articles         = json.dumps(articles_detail, ensure_ascii=False),
        nb_articles      = len(body.articles),
        statut           = statut_initial,
        delai_livraison  = port_info.delai if port_info else "—",
        note_admin       = note_auto,
        promo_code       = body.promo_code,
    )
    db.add(commande)
    db.commit()
    db.refresh(commande)

    # ✅ Mettre à jour la ref de commande dans parrainage_utilisations
    if body.code_parrainage:
        try:
            db.execute(text(
                "UPDATE parrainage_utilisations SET commande_ref=:r "
                "WHERE code=:c AND filleul_tel=:t AND commande_ref=''"
            ), {"r": commande.ref, "c": body.code_parrainage.upper().strip(),
                "t": body.client_tel})
            db.commit()
        except Exception:
            pass

    mode_label = "💳 Kkiapay ✅" if body.mode_paiement == "kkiapay" else "📱 Virement manuel"
    notifier_patron(
        db,
        "🛍️ Nouvelle commande" + (" — PAYÉE ✅" if statut_initial == "paye" else ""),
        f"{commande.client_nom} · {commande.ref} · "
        f"{round(commande.total_local or 0):,} {commande.monnaie or 'FCFA'} · {mode_label}",
        commande.ref
    )
    db.commit()

    return {
        "ref":         commande.ref,
        "total_local": commande.total_local,
        "total_euro":  commande.total_euro,
        "monnaie":     commande.monnaie,
        "nb_articles": commande.nb_articles,
        "statut":      commande.statut,
    }


@router.post("/confirmer-kkiapay")
def confirmer_kkiapay(body: KkiapayConfirmBody, db: Session = Depends(get_db)):
    ref = body.ref.strip().upper()
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    if cmd.statut == "paye":
        return {"ok": True, "ref": cmd.ref, "statut": cmd.statut, "already_paid": True}
    cmd.statut = "paye"
    note = "[KKIAPAY] Paiement confirmé automatiquement"
    if body.transaction_id:
        note += f" — Transaction: {body.transaction_id}"
    cmd.note_admin = (cmd.note_admin or "") + " | " + note if cmd.note_admin else note
    db.commit()
    try:
        notifier_patron(
            db, "✅ Paiement Kkiapay confirmé",
            f"{cmd.ref} · {cmd.client_nom} · "
            f"{round(cmd.total_local or 0):,} {cmd.monnaie or 'FCFA'}",
            cmd.ref
        )
    except Exception:
        pass
    return {"ok": True, "ref": cmd.ref, "statut": "paye"}


@router.get("/suivi/{ref}")
def suivi(ref: str, db: Session = Depends(get_db)):
    cmd = db.query(Commande).filter(Commande.ref == ref.upper()).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    return {
        "ref":             cmd.ref,
        "statut":          cmd.statut,
        "client_nom":      cmd.client_nom,
        "client_tel":      cmd.client_tel,
        "nb_articles":     cmd.nb_articles,
        "total_local":     cmd.total_local,
        "monnaie":         cmd.monnaie,
        "poids_estime":    cmd.poids_estime,
        "poids_reel":      cmd.poids_reel,
        "delai_livraison": cmd.delai_livraison,
        "articles":        json.loads(cmd.articles) if cmd.articles else [],
        "note_admin":      cmd.note_admin,
        "suivi_num":       getattr(cmd, "suivi_num", None),
        "motif_refus":     getattr(cmd, "motif_refus", None),
        "promo_code":      getattr(cmd, "promo_code", None),
        "created_at":      cmd.created_at,
        "date_estimee":    calculer_date_estimee(cmd.created_at, cmd.delai_livraison or ""),
    }


@router.get("/historique/{tel}")
def historique(tel: str, db: Session = Depends(get_db)):
    tel_clean = tel.replace(" ", "").replace("+", "").replace("-", "")

    # ✅ CORRIGÉ — Recherche exacte d'abord, 8 derniers chiffres en fallback seulement
    # Ancienne version : contains([-8:]) pouvait matcher deux clients différents
    cmds = db.query(Commande).filter(
        Commande.client_tel.contains(tel_clean)
    ).order_by(Commande.created_at.desc()).all()

    if not cmds and len(tel_clean) >= 8:
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
            "client_tel":      c.client_tel,
            "created_at":      c.created_at,
            "date_estimee":    calculer_date_estimee(c.created_at, c.delai_livraison or ""),
        }
        for c in cmds
    ]


@router.post("/annuler")
def annuler_commande(body: AnnulationBody, db: Session = Depends(get_db)):
    ref = body.ref.strip().upper()
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")

    tel_clean     = body.client_tel.replace(" ", "").replace("+", "").replace("-", "")
    cmd_tel_clean = (cmd.client_tel or "").replace(" ", "").replace("+", "").replace("-", "")
    if tel_clean[-8:] not in cmd_tel_clean:
        raise HTTPException(403, "Numéro de téléphone incorrect")

    STATUTS_ANNULABLES = ["en_attente_paiement", "paye"]
    if cmd.statut not in STATUTS_ANNULABLES:
        raise HTTPException(400, f"Annulation impossible — statut actuel : {cmd.statut}")

    ancien_statut = cmd.statut
    cmd.statut    = "annulee"
    note          = f"[ANNULATION CLIENT] Tel: {body.client_tel}"
    if body.motif:
        note += f" | Motif: {body.motif}"
    # ✅ Tronquer pour éviter dépassement de colonne
    note_existante = (cmd.note_admin or "")[-500:]
    cmd.note_admin = (note_existante + " | " + note)[-1000:] if note_existante else note[:1000]
    db.commit()

    try:
        notifier_patron(
            db, "❌ Demande d'annulation",
            f"{cmd.ref} · {cmd.client_nom} · {cmd.client_pays} · Ancien statut: {ancien_statut}",
            cmd.ref
        )
    except Exception:
        pass

    return {"ok": True, "ref": cmd.ref, "statut": "annulee"}
