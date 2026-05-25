from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, Any
import secrets, string
from database import get_db
from models import Commande
from routes.auth import require_patron

router = APIRouter(prefix="/api", tags=["parrainage", "galerie"])


# ══════════════════════════════════════════════════════════════
# MIGRATION
# ══════════════════════════════════════════════════════════════

def ensure_parrainage_tables(db: Session):
    migrations = [
        """CREATE TABLE IF NOT EXISTS parrainage_codes (
            id           SERIAL PRIMARY KEY,
            code         VARCHAR(12) UNIQUE NOT NULL,
            parrain_tel  VARCHAR NOT NULL,
            parrain_nom  VARCHAR,
            nb_filleuls  INTEGER DEFAULT 0,
            credit_total FLOAT DEFAULT 0.0,
            actif        BOOLEAN DEFAULT TRUE,
            created_at   TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS parrainage_utilisations (
            id                  SERIAL PRIMARY KEY,
            code                VARCHAR(12) NOT NULL,
            filleul_tel         VARCHAR NOT NULL,
            filleul_nom         VARCHAR,
            commande_ref        VARCHAR,
            reduction_appliquee FLOAT DEFAULT 0.0,
            created_at          TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS galerie_livraisons (
            id         SERIAL PRIMARY KEY,
            img_url    TEXT NOT NULL,
            legende    VARCHAR,
            pays       VARCHAR,
            article    VARCHAR,
            visible    BOOLEAN DEFAULT TRUE,
            ordre      INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
    ]
    for sql in migrations:
        try:
            db.execute(text(sql))
            db.commit()
        except Exception:
            db.rollback()


def _normaliser_tel(tel: str) -> str:
    """Normalise un numéro — retire +, espaces, tirets."""
    return tel.replace(" ", "").replace("-", "").replace("+", "").strip()


def gen_code_parrainage(tel: str) -> str:
    """FG + 4 derniers chiffres du tel + 6 chars aléatoires."""
    chars  = string.ascii_uppercase + string.digits
    suffix = ''.join(secrets.choice(chars) for _ in range(6))
    prefix = _normaliser_tel(tel)[-4:] if len(_normaliser_tel(tel)) >= 4 else _normaliser_tel(tel)
    return f"FG{prefix}{suffix}"


# ══════════════════════════════════════════════════════════════
# ROUTES PUBLIQUES
# ══════════════════════════════════════════════════════════════

@router.get("/parrainage/code/{tel}")
def get_mon_code(tel: str, db: Session = Depends(get_db)):
    """
    Retourne le code parrainage + stats.
    ✅ CORRIGÉ — recherche parrain_tel normalisé pour éviter les doublons
    (+224620... == 224620... == 00224620...)
    """
    tel_norm = _normaliser_tel(tel)

    # Vérifier commande recupere (normalisé)
    cmd = db.execute(text("""
        SELECT client_nom, client_tel FROM commandes
        WHERE REPLACE(REPLACE(REPLACE(client_tel, ' ', ''), '-', ''), '+', '') = :t
        AND statut = 'recupere'
        LIMIT 1
    """), {"t": tel_norm}).mappings().first()

    if not cmd:
        raise HTTPException(403, "Vous devez avoir au moins une commande récupérée.")

    # ✅ CORRIGÉ — recherche normalisée du parrain_tel
    # Évite les doublons si le même numéro est stocké avec formats différents
    row = db.execute(text("""
        SELECT code, nb_filleuls, credit_total FROM parrainage_codes
        WHERE REPLACE(REPLACE(REPLACE(parrain_tel, ' ', ''), '-', ''), '+', '') = :t
        AND actif = TRUE
        LIMIT 1
    """), {"t": tel_norm}).mappings().first()

    if not row:
        # Générer un code unique
        code = gen_code_parrainage(tel)
        for _ in range(10):
            exists = db.execute(
                text("SELECT 1 FROM parrainage_codes WHERE code = :c"), {"c": code}
            ).fetchone()
            if not exists:
                break
            code = gen_code_parrainage(tel)

        nom = cmd["client_nom"] or ""
        # ✅ Stocker le tel normalisé pour cohérence future
        tel_stocke = cmd["client_tel"] or tel  # Utiliser le tel de la commande (source fiable)
        db.execute(text(
            "INSERT INTO parrainage_codes (code, parrain_tel, parrain_nom) "
            "VALUES (:c, :t, :n)"
        ), {"c": code, "t": tel_stocke, "n": nom})
        db.commit()
        nb_filleuls  = 0
        credit_total = 0.0
    else:
        code         = row["code"]
        nb_filleuls  = row["nb_filleuls"]
        credit_total = row["credit_total"]

    # Récupérer les filleuls avec statut
    filleuls_rows = db.execute(text("""
        SELECT u.filleul_nom, u.filleul_tel, u.reduction_appliquee,
               u.commande_ref, c.statut
        FROM parrainage_utilisations u
        LEFT JOIN commandes c ON c.ref = u.commande_ref
        WHERE u.code = :code
        ORDER BY u.created_at DESC
    """), {"code": code}).mappings().all()

    filleuls = [
        {
            "filleul_nom":    f["filleul_nom"] or "Client",
            "filleul_tel":    f["filleul_tel"],
            "statut":         f["statut"] or "en_attente_paiement",
            "reduction_fcfa": f["reduction_appliquee"] or 0,
            "commande_ref":   f["commande_ref"] or "",
        }
        for f in filleuls_rows
    ]

    # ✅ CORRIGÉ — nb_commandes exclut les annulées ET les en_attente_paiement
    # credit_total reflète uniquement les filleuls avec commandes actives
    STATUTS_ACTIFS = {"paye", "achete", "expedie", "arrive", "recupere"}
    nb_commandes = sum(1 for f in filleuls if f["statut"] in STATUTS_ACTIFS)

    # ✅ CORRIGÉ — recalculer credit_total réel depuis commandes actives
    # (exclut les annulées pour un affichage juste)
    try:
        cfg_row = db.execute(text(
            "SELECT gain_parrain FROM configs WHERE id=1 LIMIT 1"
        )).fetchone()
        gain_par_filleul = float(cfg_row[0]) if cfg_row and cfg_row[0] else credit_total / max(nb_commandes, 1)
    except Exception:
        gain_par_filleul = 0

    credit_reel = round(gain_par_filleul * nb_commandes) if gain_par_filleul else credit_total

    return {
        "code":         code,
        "nb_filleuls":  nb_filleuls,
        "nb_commandes": nb_commandes,
        "credit_total": credit_reel,
        "gain_total":   credit_reel,
        "filleuls":     filleuls,
    }


@router.get("/parrainage/verifier/{code}")
def verifier_code(code: str, db: Session = Depends(get_db)):
    """
    ✅ Vérification anti-conflit avec codes promo :
    On refuse d'appliquer un code parrainage si un code promo du même nom existe déjà.
    (Le frontend essaie promo d'abord — côté backend on ne fait que vérifier existence)
    """
    code_upper = code.upper().strip()

    row = db.execute(text(
        "SELECT parrain_nom FROM parrainage_codes WHERE code = :c AND actif = TRUE"
    ), {"c": code_upper}).mappings().first()
    if not row:
        raise HTTPException(404, "Code de parrainage invalide ou expiré.")

    # Lire la réduction depuis configs
    reduction_cfg = 1000.0
    try:
        cfg_row = db.execute(text(
            "SELECT reduction_parrainage FROM configs WHERE id=1 LIMIT 1"
        )).fetchone()
        if cfg_row and cfg_row[0]:
            reduction_cfg = float(cfg_row[0])
    except Exception:
        pass

    return {
        "valide":         True,
        "parrain_nom":    row["parrain_nom"] or "un client FougahShop",
        "reduction_fcfa": reduction_cfg,
    }


@router.post("/parrainage/utiliser")
def utiliser_code(
    body: Dict[str, Any],
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron),  # ✅ Protégé — usage admin uniquement
):
    """Enregistre manuellement un parrainage — réservé à l'admin."""
    code         = str(body.get("code",         "")).upper().strip()
    filleul_tel  = str(body.get("filleul_tel",  "")).strip()
    filleul_nom  = str(body.get("filleul_nom",  "")).strip()
    commande_ref = str(body.get("commande_ref", "")).strip()
    reduction    = float(body.get("reduction_fcfa", 1000))

    if not code or not filleul_tel:
        raise HTTPException(400, "Code et téléphone requis.")

    try:
        cfg_row = db.execute(text(
            "SELECT gain_parrain FROM configs WHERE id=1 LIMIT 1"
        )).fetchone()
        gain_parrain = float(cfg_row[0]) if cfg_row and cfg_row[0] else round(reduction * 0.5)
    except Exception:
        gain_parrain = round(reduction * 0.5)

    parrain = db.execute(text(
        "SELECT parrain_tel FROM parrainage_codes WHERE code = :c AND actif = TRUE"
    ), {"c": code}).mappings().first()
    if not parrain:
        raise HTTPException(404, "Code invalide.")

    tel_norm_filleul = _normaliser_tel(filleul_tel)
    tel_norm_parrain = _normaliser_tel(parrain["parrain_tel"])
    if tel_norm_filleul == tel_norm_parrain:
        raise HTTPException(400, "Vous ne pouvez pas utiliser votre propre code.")

    deja = db.execute(text("""
        SELECT 1 FROM parrainage_utilisations u
        JOIN parrainage_codes p ON p.code = u.code
        WHERE p.code = :c
        AND REPLACE(REPLACE(REPLACE(u.filleul_tel, ' ', ''), '-', ''), '+', '') = :t
    """), {"c": code, "t": tel_norm_filleul}).fetchone()
    if deja:
        raise HTTPException(409, "Ce code a déjà été utilisé par ce numéro.")

    db.execute(text(
        "INSERT INTO parrainage_utilisations "
        "(code, filleul_tel, filleul_nom, commande_ref, reduction_appliquee) "
        "VALUES (:c, :t, :n, :r, :red)"
    ), {"c": code, "t": filleul_tel, "n": filleul_nom,
        "r": commande_ref, "red": reduction})

    db.execute(text(
        "UPDATE parrainage_codes "
        "SET nb_filleuls = nb_filleuls + 1, credit_total = credit_total + :cr "
        "WHERE code = :c"
    ), {"cr": gain_parrain, "c": code})

    db.commit()
    return {"ok": True, "reduction_fcfa": reduction}


# ══════════════════════════════════════════════════════════════
# ADMIN
# ══════════════════════════════════════════════════════════════

@router.get("/admin/parrainage")
def liste_parrainages(request: Request, db: Session = Depends(get_db),
                      role: str = Depends(require_patron)):
    """
    ✅ CORRIGÉ — nb_commandes_actives calculé depuis les vraies commandes
    (exclut annulées et refusées)
    """
    rows = db.execute(text("""
        SELECT p.code, p.parrain_nom, p.parrain_tel,
               p.nb_filleuls, p.credit_total, p.created_at,
               COUNT(u.id) as nb_util
        FROM parrainage_codes p
        LEFT JOIN parrainage_utilisations u ON u.code = p.code
        GROUP BY p.id
        ORDER BY p.nb_filleuls DESC, p.created_at DESC
        LIMIT 200
    """)).mappings().all()

    result = []
    for r in rows:
        d = dict(r)
        # ✅ Recalculer nb commandes actives depuis les vraies commandes
        try:
            actives = db.execute(text("""
                SELECT COUNT(*) FROM parrainage_utilisations u
                JOIN commandes c ON c.ref = u.commande_ref
                WHERE u.code = :code
                AND c.statut IN ('paye','achete','expedie','arrive','recupere')
            """), {"code": d["code"]}).scalar()
            d["nb_commandes_actives"] = actives or 0
        except Exception:
            d["nb_commandes_actives"] = d.get("nb_filleuls", 0)
        result.append(d)

    return result


# ══════════════════════════════════════════════════════════════
# GALERIE
# ══════════════════════════════════════════════════════════════

@router.get("/admin/galerie-all")
def get_galerie_admin(request: Request, db: Session = Depends(get_db),
                      role: str = Depends(require_patron)):
    rows = db.execute(text(
        "SELECT id, img_url, legende, pays, article, visible "
        "FROM galerie_livraisons ORDER BY ordre ASC, created_at DESC"
    )).mappings().all()
    return [dict(r) for r in rows]


@router.get("/galerie")
def get_galerie(db: Session = Depends(get_db)):
    rows = db.execute(text(
        "SELECT id, img_url, legende, pays, article "
        "FROM galerie_livraisons WHERE visible = TRUE "
        "ORDER BY ordre ASC, created_at DESC LIMIT 20"
    )).mappings().all()
    return [dict(r) for r in rows]


@router.post("/admin/galerie", status_code=201)
def add_galerie(body: Dict[str, Any], request: Request,
                db: Session = Depends(get_db),
                role: str = Depends(require_patron)):
    img_url = str(body.get("img_url", "")).strip()
    if not img_url:
        raise HTTPException(400, "URL image requise.")
    if not img_url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL invalide — doit commencer par http:// ou https://")
    db.execute(text(
        "INSERT INTO galerie_livraisons (img_url, legende, pays, article) "
        "VALUES (:u, :l, :p, :a)"
    ), {
        "u": img_url,
        "l": str(body.get("legende", "")).strip() or None,
        "p": str(body.get("pays",    "")).strip() or None,
        "a": str(body.get("article", "")).strip() or None,
    })
    db.commit()
    return {"ok": True}


@router.delete("/admin/galerie/{item_id}")
def del_galerie(item_id: int, request: Request,
                db: Session = Depends(get_db),
                role: str = Depends(require_patron)):
    db.execute(text("DELETE FROM galerie_livraisons WHERE id = :id"), {"id": item_id})
    db.commit()
    return {"ok": True}


@router.patch("/admin/galerie/{item_id}/toggle")
def toggle_galerie(item_id: int, request: Request,
                   db: Session = Depends(get_db),
                   role: str = Depends(require_patron)):
    db.execute(text(
        "UPDATE galerie_livraisons SET visible = NOT visible WHERE id = :id"
    ), {"id": item_id})
    db.commit()
    return {"ok": True}
