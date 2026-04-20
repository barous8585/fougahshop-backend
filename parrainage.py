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
# MIGRATION — appeler UNE SEULE FOIS au démarrage depuis main.py
# from routes.parrainage import ensure_parrainage_tables
# puis dans le lifespan : ensure_parrainage_tables(db)
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


def gen_code_parrainage(tel: str) -> str:
    suffix = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    prefix = tel[-4:] if len(tel) >= 4 else tel
    return f"FG{prefix}{suffix}"


# ══════════════════════════════════════════════════════════════
# PARRAINAGE — routes publiques
# ══════════════════════════════════════════════════════════════

@router.get("/parrainage/code/{tel}")
def get_mon_code(tel: str, db: Session = Depends(get_db)):
    """
    Retourne le code parrainage + stats complètes attendues par le frontend :
    code, nb_filleuls, nb_commandes, credit_total, gain_total, filleuls[]
    """
    cmd = db.query(Commande).filter(
        Commande.client_tel == tel,
        Commande.statut == "recupere"
    ).first()
    if not cmd:
        raise HTTPException(403, "Vous devez avoir au moins une commande récupérée.")

    row = db.execute(text(
        "SELECT code, nb_filleuls, credit_total FROM parrainage_codes "
        "WHERE parrain_tel = :t AND actif = TRUE"
    ), {"t": tel}).mappings().first()

    if not row:
        code = gen_code_parrainage(tel)
        for _ in range(5):
            exists = db.execute(text("SELECT 1 FROM parrainage_codes WHERE code = :c"), {"c": code}).fetchone()
            if not exists:
                break
            code = gen_code_parrainage(tel)
        nom = cmd.client_nom or ""
        db.execute(text(
            "INSERT INTO parrainage_codes (code, parrain_tel, parrain_nom) VALUES (:c, :t, :n)"
        ), {"c": code, "t": tel, "n": nom})
        db.commit()
        nb_filleuls = 0
        credit_total = 0.0
    else:
        code = row["code"]
        nb_filleuls = row["nb_filleuls"]
        credit_total = row["credit_total"]

    # Récupérer les filleuls avec statut de leur commande
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

    # Compter uniquement les commandes actives (pas annulées)
    nb_commandes = sum(
        1 for f in filleuls
        if f["statut"] not in ("en_attente_paiement", "annulee", "paiement_refuse")
    )

    return {
        "code":         code,
        "nb_filleuls":  nb_filleuls,
        "nb_commandes": nb_commandes,
        "credit_total": credit_total,
        "gain_total":   credit_total,  # alias pour le frontend
        "filleuls":     filleuls,
    }


@router.get("/parrainage/verifier/{code}")
def verifier_code(code: str, db: Session = Depends(get_db)):
    row = db.execute(text(
        "SELECT parrain_nom FROM parrainage_codes WHERE code = :c AND actif = TRUE"
    ), {"c": code.upper()}).mappings().first()
    if not row:
        raise HTTPException(404, "Code de parrainage invalide ou expiré.")
    return {"valide": True, "parrain_nom": row["parrain_nom"] or "un client FougahShop"}


@router.post("/parrainage/utiliser")
def utiliser_code(body: Dict[str, Any], db: Session = Depends(get_db)):
    """
    Enregistre l'utilisation d'un code parrainage.
    gain_parrain : gain fixe du parrain (vient de promo.gain_par_cmd)
                   fallback = 50% de la réduction si non fourni
    """
    code         = str(body.get("code", "")).upper().strip()
    filleul_tel  = str(body.get("filleul_tel", "")).strip()
    filleul_nom  = str(body.get("filleul_nom", "")).strip()
    commande_ref = str(body.get("commande_ref", "")).strip()
    reduction    = float(body.get("reduction_fcfa", 1000))
    gain_parrain = float(body.get("gain_parrain", 0)) or round(reduction * 0.5)

    if not code or not filleul_tel:
        raise HTTPException(400, "Code et téléphone requis.")

    parrain = db.execute(text(
        "SELECT parrain_tel FROM parrainage_codes WHERE code = :c AND actif = TRUE"
    ), {"c": code}).mappings().first()
    if not parrain:
        raise HTTPException(404, "Code invalide.")

    if parrain["parrain_tel"] == filleul_tel:
        raise HTTPException(400, "Vous ne pouvez pas utiliser votre propre code.")

    deja = db.execute(text(
        "SELECT 1 FROM parrainage_utilisations WHERE code = :c AND filleul_tel = :t"
    ), {"c": code, "t": filleul_tel}).fetchone()
    if deja:
        raise HTTPException(409, "Ce code a déjà été utilisé par ce numéro.")

    db.execute(text(
        "INSERT INTO parrainage_utilisations "
        "(code, filleul_tel, filleul_nom, commande_ref, reduction_appliquee) "
        "VALUES (:c, :t, :n, :r, :red)"
    ), {"c": code, "t": filleul_tel, "n": filleul_nom, "r": commande_ref, "red": reduction})

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
    return [dict(r) for r in rows]


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
                db: Session = Depends(get_db), role: str = Depends(require_patron)):
    img_url = str(body.get("img_url", "")).strip()
    if not img_url:
        raise HTTPException(400, "URL image requise.")
    db.execute(text(
        "INSERT INTO galerie_livraisons (img_url, legende, pays, article) VALUES (:u, :l, :p, :a)"
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
                db: Session = Depends(get_db), role: str = Depends(require_patron)):
    db.execute(text("DELETE FROM galerie_livraisons WHERE id = :id"), {"id": item_id})
    db.commit()
    return {"ok": True}


@router.patch("/admin/galerie/{item_id}/toggle")
def toggle_galerie(item_id: int, request: Request,
                   db: Session = Depends(get_db), role: str = Depends(require_patron)):
    db.execute(text(
        "UPDATE galerie_livraisons SET visible = NOT visible WHERE id = :id"
    ), {"id": item_id})
    db.commit()
    return {"ok": True}
