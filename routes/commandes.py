from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import json, traceback
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

# ── Commission progressive : 5000 FCFA de base, +3300 FCFA par tranche de 50€ entamée ──
# Règle de borne : un montant exactement égal à un multiple de 50€ reste dans la tranche inférieure
# (ex: 50,00€ → 5000 FCFA ; 50,01€ → 8300 FCFA). Sans plafond — continue indéfiniment.
COMMISSION_BASE          = 5000
COMMISSION_PALIER_EUROS  = 50
COMMISSION_PALIER_AJOUT  = 3300

TOTAL_VALIDATION_WARN_ONLY = True
TOTAL_TOLERANCE_PCT        = 5


def get_commission(total_euros: float) -> float:
    import math
    depasse     = max(0.0, total_euros - COMMISSION_PALIER_EUROS)
    nb_tranches = math.ceil(depasse / COMMISSION_PALIER_EUROS)
    return COMMISSION_BASE + nb_tranches * COMMISSION_PALIER_AJOUT


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
    year   = datetime.now().year
    prefix = f"CMD-{year}-"
    try:
        result = db.execute(
            text("""
                SELECT COALESCE(MAX(CAST(SUBSTRING(ref FROM :pos) AS INTEGER)), 0) + 1
                FROM commandes
                WHERE ref LIKE :pattern
            """),
            {"pos": len(prefix) + 1, "pattern": f"{prefix}%"}
        ).scalar()
    except Exception as e:
        print(f"[generate_ref] Erreur SQL : {e}")
        result = 1
    return f"{prefix}{result or 1:04d}"


def calc_article_sans_port_ni_commission(prix_eu, qty, pays, cfg):
    m         = MONNAIES.get(pays, {"symbole": "FCFA", "taux_base": 656})
    taux_gnf  = cfg.taux_gnf   if (cfg.taux_gnf  and cfg.taux_gnf  >= 1000) else 9500
    taux_fcfa = cfg.taux_change or 660

    if m["symbole"] == "GNF":
        taux_conv  = taux_gnf / 656
        base_local = round(prix_eu * taux_gnf * qty)
        base_fcfa  = round(prix_eu * taux_fcfa)
    else:
        taux_conv  = 1.0
        base_fcfa  = round(prix_eu * taux_fcfa)
        base_local = round(prix_eu * taux_fcfa * qty)

    return {
        "base_fcfa":   base_fcfa,
        "total_local": base_local,
        "monnaie":     m["symbole"],
        "taux_conv":   taux_conv,
    }


def appliquer_promo(db, promo_code: str, total_panier: float, commission_locale: float, taux_conv: float) -> float:
    """
    Applique un code promo UNIQUEMENT sur la commission de service — jamais sur le prix du panier.
    Retourne la nouvelle commission (toujours >= 0, jamais négative).
    total_panier est utilisé en lecture seule pour le calcul du pourcentage (% du panier, pas de la commission).
    """
    if not promo_code:
        return commission_locale
    try:
        promo = db.execute(
            text("SELECT * FROM promo_codes WHERE code=:code AND actif=TRUE LIMIT 1"),
            {"code": promo_code.strip().upper()}
        ).fetchone()

        if not promo:
            return commission_locale

        expiry = getattr(promo, "expiry", None)
        if expiry:
            from datetime import date
            exp_date = None
            if hasattr(expiry, "year"):
                exp_date = expiry
            elif isinstance(expiry, str) and expiry:
                try:
                    exp_date = date.fromisoformat(expiry[:10])
                except ValueError:
                    pass
            if exp_date and exp_date < date.today():
                return commission_locale

        uses  = getattr(promo, "uses_count", 0)  or getattr(promo, "utilisations", 0) or 0
        max_u = getattr(promo, "max_uses",   0)  or getattr(promo, "quota",        0) or 0
        if max_u > 0 and uses >= max_u:
            return commission_locale

        type_promo = getattr(promo, "type", "fixe") or "fixe"
        # ✅ FIX : valeur en priorité, fallback reduction_fcfa
        valeur = getattr(promo, "valeur", None) or getattr(promo, "reduction_fcfa", 0) or 0

        if type_promo == "livraison":
            nouvelle_commission = commission_locale
        elif type_promo == "pct":
            # Le pourcentage s'applique sur le panier (référence métier habituelle pour le client),
            # mais la réduction qui en résulte est déduite de la COMMISSION uniquement, jamais du panier.
            reduction = round(total_panier * float(valeur) / 100)
            nouvelle_commission = max(0, commission_locale - reduction)
        else:
            reduction = round(float(valeur) * taux_conv)
            nouvelle_commission = max(0, commission_locale - reduction)

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

        return nouvelle_commission

    except Exception as e:
        print(f"[promo] Erreur application code: {e}")
        return commission_locale


def _get_gain_parrain(db, reduction_fcfa: float) -> float:
    try:
        cfg_row = db.execute(
            text("SELECT gain_parrain FROM configs WHERE id=1 LIMIT 1")
        ).fetchone()
        if cfg_row and cfg_row[0]:
            return float(cfg_row[0])
    except Exception:
        pass
    return round(reduction_fcfa * 0.5)


def _normaliser_tel(tel: str) -> str:
    """Retourne uniquement les chiffres du numéro."""
    return ''.join(filter(str.isdigit, tel or ""))


def _suffix(tel: str, n: int = 8) -> str:
    """Retourne les n derniers chiffres — robuste peu importe le format."""
    digits = _normaliser_tel(tel)
    return digits[-n:] if len(digits) >= n else digits


def _enregistrer_parrainage(db, code, filleul_tel, filleul_nom, commande_ref, reduction_fcfa):
    """
    Enregistre l'utilisation d'un code parrainage.
    ✅ FIX : comparaison par suffixe robuste (plus d'échec silencieux)
    ✅ FIX : vérification doublon par suffixe aussi
    """
    code = code.upper().strip()
    try:
        parrain = db.execute(
            text("SELECT parrain_tel FROM parrainage_codes WHERE code=:c AND actif=TRUE"),
            {"c": code}
        ).mappings().first()
        if not parrain:
            print(f"[parrainage] Code {code} invalide ou inactif")
            return False

        # ✅ FIX : comparaison par suffixe (8 derniers chiffres)
        filleul_suffix = _suffix(filleul_tel)
        parrain_suffix = _suffix(parrain["parrain_tel"])

        if filleul_suffix == parrain_suffix:
            print(f"[parrainage] Auto-parrainage bloqué pour {filleul_tel}")
            return False

        # ✅ FIX : doublon vérifié par suffixe aussi
        deja = db.execute(
            text("""
                SELECT 1 FROM parrainage_utilisations
                WHERE code = :c
                AND RIGHT(REGEXP_REPLACE(filleul_tel, '[^0-9]', '', 'g'), 8) = :suffix
            """),
            {"c": code, "suffix": filleul_suffix}
        ).fetchone()

        if not deja:
            # Fallback sans REGEXP_REPLACE
            try:
                deja = db.execute(
                    text("""
                        SELECT 1 FROM parrainage_utilisations
                        WHERE code = :c
                        AND RIGHT(REPLACE(REPLACE(REPLACE(filleul_tel,' ',''),'-',''),'+',''), 8) = :suffix
                    """),
                    {"c": code, "suffix": filleul_suffix}
                ).fetchone()
            except Exception:
                pass

        if deja:
            print(f"[parrainage] Déjà utilisé par {filleul_tel} pour code {code}")
            return False

        gain = _get_gain_parrain(db, reduction_fcfa)

        db.execute(
            text("""
                INSERT INTO parrainage_utilisations
                    (code, filleul_tel, filleul_nom, commande_ref, reduction_appliquee)
                VALUES (:c, :t, :n, :r, :red)
            """),
            {"c": code, "t": filleul_tel, "n": filleul_nom,
             "r": commande_ref, "red": reduction_fcfa}
        )
        db.execute(
            text("""
                UPDATE parrainage_codes
                SET nb_filleuls  = nb_filleuls + 1,
                    credit_total = credit_total + :g
                WHERE code = :c
            """),
            {"g": gain, "c": code}
        )
        db.flush()
        print(f"[parrainage] ✅ Code {code} utilisé par {filleul_tel} — gain parrain: {gain} FCFA")
        return True

    except Exception as e:
        print(f"[parrainage] Erreur _enregistrer_parrainage: {e}")
        traceback.print_exc()
        return False


# ── Schemas ───────────────────────────────────────────────────

class ArticleIn(BaseModel):
    lien:                    str
    nom:                     str
    img:                     Optional[str]   = None
    categorie:               Optional[str]   = None
    taille:                  Optional[str]   = None
    couleur:                 Optional[str]   = None
    specs:                   Optional[str]   = None
    prix_eu:                 float
    frais_livraison_boutique: Optional[float] = 0.0
    poids:                   float           = 0.5
    qty:                     int             = 1


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
    code_parrainage:        Optional[str]   = None
    reduction_parrainage:   Optional[float] = None
    mode_paiement:          Optional[str]   = None
    kkiapay_transaction_id: Optional[str]   = None
    articles:               List[ArticleIn]
    total_local_client:     Optional[float] = None
    monnaie_client:         Optional[str]   = None
    taux_utilise:           Optional[float] = None
    is_cadeau:              Optional[bool]  = False
    dest_nom:               Optional[str]   = None
    dest_tel:               Optional[str]   = None
    payeur_nom:             Optional[str]   = None


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


class PanierWA(BaseModel):
    lien:      str
    prix:      float
    livraison: Optional[float] = 0.0


class CommandeWACreate(BaseModel):
    client_nom:          str
    client_tel:          str
    client_pays:         str
    client_adresse:      str
    client_instructions: Optional[str]   = None
    paniers:             List[PanierWA]
    total_eur:           float
    total_local:         float
    devise:              str
    taux:                float
    promo_code:          Optional[str]   = None
    code_parrainage:     Optional[str]   = None
    reduction_appliquee: Optional[float] = None


# ── Fonctions utilitaires ─────────────────────────────────────

def _sanitize_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    lower = url.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        return url
    return ""


def _calculer_total_serveur(articles, pays, cfg, promo_code, db):
    m = MONNAIES.get(pays, {"symbole": "FCFA", "taux_base": 656})
    total_local_sans_comm = 0.0
    total_eu              = 0.0
    poids_total           = 0.0
    taux_conv             = 1.0

    for a in articles:
        frais_b       = float(getattr(a, 'frais_livraison_boutique', 0) or 0)
        prix_total_eu = a.prix_eu + frais_b
        detail        = calc_article_sans_port_ni_commission(prix_total_eu, a.qty, pays, cfg)
        total_eu              += prix_total_eu * a.qty
        total_local_sans_comm += detail["total_local"]
        poids_total           += a.poids * a.qty
        taux_conv              = detail["taux_conv"]

    commission_fcfa   = get_commission(total_eu)
    commission_locale = round(commission_fcfa * taux_conv)

    if promo_code:
        commission_locale = appliquer_promo(db, promo_code, total_local_sans_comm, commission_locale, taux_conv)

    total_local = total_local_sans_comm + commission_locale

    return total_local, total_eu, poids_total, taux_conv, m["symbole"]


def _valider_total(total_serveur: float, total_client: Optional[float]) -> float:
    if not total_client or total_client <= 0:
        return total_serveur
    ecart_pct = abs(total_serveur - total_client) / max(total_serveur, 1) * 100
    if ecart_pct <= TOTAL_TOLERANCE_PCT:
        return total_serveur
    print(
        f"[VALIDATION TOTAL] Écart détecté : "
        f"serveur={total_serveur:.0f} / client={total_client:.0f} "
        f"({ecart_pct:.1f}%) — total serveur utilisé."
    )
    if not TOTAL_VALIDATION_WARN_ONLY:
        raise HTTPException(
            400,
            f"Montant incohérent (écart {ecart_pct:.0f}%). "
            "Rechargez la page et réessayez."
        )
    return total_serveur


# ── Routes ────────────────────────────────────────────────────

@router.post("/calculer")
def calculer(body: CalculRequest, db: Session = Depends(get_db)):
    cfg        = get_config(db)
    detail     = calc_article_sans_port_ni_commission(body.prix_eu, body.qty, body.pays, cfg)
    commission = get_commission(body.prix_eu * body.qty)
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
def creer_commande(body: CommandeCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        if not body.articles:
            raise HTTPException(400, "Panier vide")

        cfg       = get_config(db)
        port_info = db.query(PortKg).filter(PortKg.pays == body.client_pays).first()

        total_local_serveur, total_eu, poids_total, taux_conv, monnaie_symbole = (
            _calculer_total_serveur(body.articles, body.client_pays, cfg, body.promo_code, db)
        )

        total_local = _valider_total(total_local_serveur, body.total_local_client)
        monnaie     = MONNAIES.get(body.client_pays, {"symbole": "FCFA"})["symbole"]

        articles_detail = []
        for a in body.articles:
            frais_b       = float(getattr(a, 'frais_livraison_boutique', 0) or 0)
            prix_total_eu = a.prix_eu + frais_b
            detail        = calc_article_sans_port_ni_commission(prix_total_eu, a.qty, body.client_pays, cfg)
            articles_detail.append({
                "lien":                     _sanitize_url(a.lien or ""),
                "nom":                      a.nom,
                "img":                      a.img,
                "categorie":                a.categorie,
                "taille":                   a.taille,
                "couleur":                  a.couleur,
                "specs":                    a.specs,
                "prix_eu":                  a.prix_eu,
                "frais_livraison_boutique": frais_b,
                "poids":                    a.poids,
                "qty":                      a.qty,
                "total_local":              detail["total_local"],
                "monnaie":                  detail["monnaie"],
            })

        statut_initial = "paye" if body.mode_paiement == "kkiapay" else "en_attente_paiement"
        note_auto = None
        if body.mode_paiement == "kkiapay" and body.kkiapay_transaction_id:
            note_auto = f"[KKIAPAY] Transaction: {body.kkiapay_transaction_id}"

        commande = Commande(
            ref                 = generate_ref(db),
            client_nom          = body.client_nom,
            client_tel          = body.client_tel,
            client_pays         = body.client_pays,
            client_adresse      = body.client_adresse,
            client_instructions = body.client_instructions,
            operateur           = body.operateur,
            monnaie             = monnaie,
            total_euro          = round(total_eu, 2),
            total_local         = round(total_local),
            poids_estime        = round(poids_total, 2),
            articles            = json.dumps(articles_detail, ensure_ascii=False),
            nb_articles         = len(body.articles),
            statut              = statut_initial,
            delai_livraison     = port_info.delai if port_info else "—",
            note_admin          = note_auto,
            promo_code          = body.promo_code,
        )

        for col, val in [
            ("is_cadeau",  body.is_cadeau or False),
            ("dest_nom",   body.dest_nom),
            ("dest_tel",   body.dest_tel),
            ("payeur_nom", body.payeur_nom),
        ]:
            try:
                setattr(commande, col, val)
            except Exception:
                pass

        db.add(commande)
        db.commit()
        db.refresh(commande)

    except HTTPException:
        raise
    except Exception as e:
        print(f"[creer_commande] ERREUR CRITIQUE:\n{traceback.format_exc()}")
        db.rollback()
        raise HTTPException(500, f"Erreur lors de la création de la commande : {str(e)}")

    if body.code_parrainage and body.code_parrainage.strip():
        try:
            _enregistrer_parrainage(
                db             = db,
                code           = body.code_parrainage,
                filleul_tel    = body.client_tel,
                filleul_nom    = body.client_nom,
                commande_ref   = commande.ref,
                reduction_fcfa = float(body.reduction_parrainage or 1000),
            )
            db.commit()
        except Exception as e:
            print(f"[parrainage] Erreur enregistrement (non bloquant): {e}")
            db.rollback()

    mode_label = "💳 Kkiapay ✅" if body.mode_paiement == "kkiapay" else "📱 Virement manuel"
    try:
        notifier_patron(
            db,
            "🛍️ Nouvelle commande" + (" — PAYÉE ✅" if statut_initial == "paye" else ""),
            f"{commande.client_nom} · {commande.ref} · "
            f"{round(commande.total_local or 0):,} {commande.monnaie or 'FCFA'} · {mode_label}",
            commande.ref
        )
        db.commit()
    except Exception as e:
        print(f"[notif] Erreur (non bloquant): {e}")

    try:
        from routes.onedrive import ajouter_commande_excel
        background_tasks.add_task(ajouter_commande_excel, {
            "ref":         commande.ref,
            "client_nom":  commande.client_nom,
            "client_tel":  commande.client_tel,
            "client_pays": commande.client_pays,
            "total_euro":  commande.total_euro,
            "monnaie":     commande.monnaie,
            "statut":      commande.statut,
            "articles":    commande.articles,
            "note_admin":  commande.note_admin,
            "promo_code":  commande.promo_code,
            "created_at":  commande.created_at,
            "taux_gnf":    cfg.taux_gnf or 9500,
        })
    except Exception as e:
        print(f"[OneDrive] Erreur sync commande (non bloquant): {e}")

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
def suivi(ref: str, tel: str = Query(...), db: Session = Depends(get_db)):
    cmd = db.query(Commande).filter(Commande.ref == ref.upper()).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")

    tel_saisi = _normaliser_tel(tel)
    tel_cmd   = _normaliser_tel(cmd.client_tel or "")

    if len(tel_saisi) < 8:
        raise HTTPException(403, "Numéro de téléphone incorrect")

    suffix_saisi = tel_saisi[-9:] if len(tel_saisi) >= 9 else tel_saisi
    suffix_cmd   = tel_cmd[-9:]   if len(tel_cmd)   >= 9 else tel_cmd

    match = False
    for n in range(8, min(len(suffix_saisi), len(suffix_cmd)) + 1):
        if suffix_saisi[-n:] == suffix_cmd[-n:]:
            match = True
            break
    if not match:
        raise HTTPException(403, "Numéro de téléphone incorrect")

    total_display = cmd.total_local
    note_display  = cmd.note_admin
    if not total_display or total_display == 0:
        total_display = None

    return {
        "ref":             cmd.ref,
        "statut":          cmd.statut,
        "client_nom":      cmd.client_nom,
        "client_tel":      cmd.client_tel,
        "nb_articles":     cmd.nb_articles,
        "total_local":     total_display,
        "monnaie":         cmd.monnaie,
        "poids_estime":    cmd.poids_estime,
        "poids_reel":      cmd.poids_reel,
        "delai_livraison": cmd.delai_livraison,
        "articles":        json.loads(cmd.articles) if cmd.articles else [],
        "note_admin":      note_display,
        "suivi_num":       getattr(cmd, "suivi_num", None),
        "motif_refus":     getattr(cmd, "motif_refus", None),
        "promo_code":      getattr(cmd, "promo_code", None),
        "created_at":      cmd.created_at,
        "date_estimee":    calculer_date_estimee(cmd.created_at, cmd.delai_livraison or ""),
    }


@router.get("/historique/{tel}")
def historique(tel: str, db: Session = Depends(get_db)):
    tel_chiffres = _normaliser_tel(tel)
    if len(tel_chiffres) < 8:
        raise HTTPException(404, "Numéro trop court")

    suffixe = tel_chiffres[-9:]
    cmds    = []

    try:
        rows = db.execute(text("""
            SELECT * FROM commandes
            WHERE REGEXP_REPLACE(client_tel, '[^0-9]', '', 'g') LIKE :pattern
            ORDER BY created_at DESC
        """), {"pattern": f"%{suffixe}%"}).mappings().all()
        cmds = list(rows)
    except Exception as e:
        print(f"[historique] REGEXP_REPLACE non supporté, fallback: {e}")

    if not cmds:
        try:
            rows = db.execute(text("""
                SELECT * FROM commandes
                WHERE REPLACE(REPLACE(REPLACE(REPLACE(client_tel, ' ', ''), '+', ''), '-', ''), '.', '')
                      LIKE :pattern
                ORDER BY created_at DESC
            """), {"pattern": f"%{suffixe}%"}).mappings().all()
            cmds = list(rows)
        except Exception as e:
            print(f"[historique] REPLACE fallback échoué: {e}")

    if not cmds:
        tel_clean  = tel.replace(" ", "").replace("+", "").replace("-", "")
        cmds_orm   = db.query(Commande).filter(
            Commande.client_tel.contains(tel_clean)
        ).order_by(Commande.created_at.desc()).all()
        cmds = [
            {col.name: getattr(c, col.name) for col in Commande.__table__.columns}
            for c in cmds_orm
        ]

    if not cmds:
        raise HTTPException(404, "Aucune commande trouvée")

    result = []
    for c in cmds:
        def g(key):
            if isinstance(c, dict): return c.get(key)
            return getattr(c, key, None)
        created = g("created_at")
        delai   = g("delai_livraison") or ""
        total   = g("total_local")
        result.append({
            "ref":             g("ref"),
            "statut":          g("statut"),
            "nb_articles":     g("nb_articles"),
            "total_local":     total if total else None,
            "monnaie":         g("monnaie"),
            "delai_livraison": delai,
            "note_admin":      g("note_admin"),
            "client_nom":      g("client_nom"),
            "client_tel":      g("client_tel"),
            "created_at":      created,
            "date_estimee":    calculer_date_estimee(created, delai),
        })
    return result


@router.post("/annuler")
def annuler_commande(body: AnnulationBody, db: Session = Depends(get_db)):
    ref = body.ref.strip().upper()
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")

    tel_chiffres     = _normaliser_tel(body.client_tel)
    cmd_tel_chiffres = _normaliser_tel(cmd.client_tel or "")
    if len(tel_chiffres) < 8 or tel_chiffres[-8:] not in cmd_tel_chiffres:
        raise HTTPException(403, "Numéro de téléphone incorrect")

    STATUTS_ANNULABLES = ["en_attente_paiement", "paye", "en_attente"]
    if cmd.statut not in STATUTS_ANNULABLES:
        raise HTTPException(400, f"Annulation impossible — statut actuel : {cmd.statut}")

    ancien_statut  = cmd.statut
    cmd.statut     = "annulee"
    note           = f"[ANNULATION CLIENT] Tel: {body.client_tel}"
    if body.motif:
        note += f" | Motif: {body.motif}"
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


@router.post("/whatsapp", status_code=201)
def creer_commande_whatsapp(body: CommandeWACreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        if not body.paniers:
            raise HTTPException(400, "Aucun panier fourni")

        cfg    = get_config(db)
        m      = MONNAIES.get(body.client_pays, {"symbole": "FCFA", "taux_base": 656})
        is_gnf = m["symbole"] == "GNF"
        taux   = (cfg.taux_gnf or 9500) if is_gnf else (cfg.taux_change or 656)

        total_eur  = sum(p.prix + (p.livraison or 0) for p in body.paniers)
        taux_conv  = taux / 656 if is_gnf else 1.0

        is_panier_lien = (total_eur == 0)

        promo_code_valide      = None
        parrain_code_valide    = None
        reduction_parrain_fcfa = 1000.0

        if is_panier_lien:
            total_local        = 0
            total_brut_serveur = 0
            commission_locale  = 0
        else:
            total_converti = sum(
                round(p.prix * taux) + round((p.livraison or 0) * taux)
                for p in body.paniers
            )
            commission_fcfa    = get_commission(total_eur)
            commission_locale  = round(commission_fcfa * taux_conv)

            if body.promo_code:
                try:
                    code_upper           = body.promo_code.strip().upper()
                    commission_avant     = commission_locale
                    commission_locale    = appliquer_promo(db, code_upper, total_converti, commission_locale, taux_conv)
                    if commission_locale != commission_avant:
                        promo_code_valide = code_upper
                except Exception as e:
                    print(f"[WA] Erreur application promo: {e}")

            if body.code_parrainage and not promo_code_valide:
                try:
                    parrain_row = db.execute(
                        text("SELECT parrain_tel FROM parrainage_codes WHERE code=:c AND actif=TRUE"),
                        {"c": body.code_parrainage.strip().upper()}
                    ).mappings().first()
                    # ✅ FIX : comparaison par suffixe
                    if parrain_row and _suffix(parrain_row["parrain_tel"]) != _suffix(body.client_tel):
                        parrain_code_valide = body.code_parrainage.strip().upper()
                        cfg_red = db.execute(
                            text("SELECT reduction_parrainage FROM configs WHERE id=1 LIMIT 1")
                        ).fetchone()
                        reduction_parrain_fcfa = float(cfg_red[0]) if cfg_red and cfg_red[0] else 1000.0
                        # Le parrainage suit la même règle : déduit de la commission, jamais du panier.
                        reduction_a_appliquer = round(reduction_parrain_fcfa * taux_conv)
                        commission_locale     = max(0, commission_locale - reduction_a_appliquer)
                except Exception as e:
                    print(f"[WA] Erreur vérif parrainage: {e}")

            total_brut_serveur = total_converti + commission_locale
            total_local = _valider_total(total_brut_serveur, body.total_local)

        articles_detail = []
        for i, p in enumerate(body.paniers):
            prix_avec_livr  = p.prix + (p.livraison or 0)
            total_local_art = round(prix_avec_livr * taux) if not is_panier_lien else 0
            articles_detail.append({
                "lien":                     _sanitize_url(p.lien),
                "nom":                      f"Panier {i + 1}",
                "prix_eu":                  p.prix,
                "frais_livraison_boutique": p.livraison or 0,
                "poids":                    0.5,
                "qty":                      1,
                "monnaie":                  m["symbole"],
                "total_local":              total_local_art,
            })

        port_info = db.query(PortKg).filter(PortKg.pays == body.client_pays).first()

        if is_panier_lien:
            note = "[WhatsApp] 📎 Panier lien — PRIX À CONFIRMER"
        else:
            note = f"[WhatsApp] {len(body.paniers)} panier(s) — en attente de confirmation"

        commande = Commande(
            ref                 = generate_ref(db),
            client_nom          = body.client_nom,
            client_tel          = body.client_tel,
            client_pays         = body.client_pays,
            client_adresse      = body.client_adresse,
            client_instructions = body.client_instructions,
            operateur           = "WhatsApp",
            monnaie             = m["symbole"],
            total_euro          = round(total_eur, 2),
            total_local         = round(total_local),
            poids_estime        = round(len(body.paniers) * 0.5, 2),
            articles            = json.dumps(articles_detail, ensure_ascii=False),
            nb_articles         = len(body.paniers),
            statut              = "en_attente_paiement",
            delai_livraison     = port_info.delai if port_info else "—",
            note_admin          = note,
            promo_code          = (promo_code_valide or parrain_code_valide) if not is_panier_lien else None,
        )
        db.add(commande)
        db.commit()
        db.refresh(commande)

    except HTTPException:
        raise
    except Exception as e:
        print(f"[creer_commande_whatsapp] ERREUR CRITIQUE:\n{traceback.format_exc()}")
        db.rollback()
        raise HTTPException(500, f"Erreur lors de la création de la commande WhatsApp : {str(e)}")

    if not is_panier_lien and parrain_code_valide:
        try:
            _enregistrer_parrainage(
                db             = db,
                code           = parrain_code_valide,
                filleul_tel    = body.client_tel,
                filleul_nom    = body.client_nom,
                commande_ref   = commande.ref,
                reduction_fcfa = reduction_parrain_fcfa,
            )
            db.commit()
        except Exception as e:
            print(f"[WA] Erreur enregistrement parrainage (non bloquant): {e}")
            db.rollback()

    titre_notif = "📎 Panier lien — PRIX À CONFIRMER" if is_panier_lien else "📲 Nouvelle commande WhatsApp"
    try:
        notifier_patron(
            db,
            titre_notif,
            f"{commande.client_nom} · {commande.ref} · {commande.client_pays}"
            + (f" · {round(commande.total_local or 0):,} {commande.monnaie}" if not is_panier_lien else " · Prix à confirmer"),
            commande.ref
        )
        db.commit()
    except Exception as e:
        print(f"[notif] Erreur (non bloquant): {e}")

    try:
        from routes.onedrive import ajouter_commande_excel
        background_tasks.add_task(ajouter_commande_excel, {
            "ref":         commande.ref,
            "client_nom":  commande.client_nom,
            "client_tel":  commande.client_tel,
            "client_pays": commande.client_pays,
            "total_euro":  commande.total_euro,
            "monnaie":     commande.monnaie,
            "statut":      commande.statut,
            "articles":    commande.articles,
            "note_admin":  commande.note_admin,
            "promo_code":  commande.promo_code,
            "created_at":  commande.created_at,
            "taux_gnf":    cfg.taux_gnf or 9500,
        })
    except Exception as e:
        print(f"[OneDrive] Erreur sync commande WA (non bloquant): {e}")

    return {
        "ref":         commande.ref,
        "total_local": commande.total_local,
        "total_euro":  commande.total_euro,
        "monnaie":     commande.monnaie,
        "statut":      commande.statut,
    }
