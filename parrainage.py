from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, Any, Optional
import secrets, string
from database import get_db
from models import Commande
from routes.auth import require_auth, require_patron

router = APIRouter(prefix="/api", tags=["parrainage", "galerie"])

# ══════════════════════════════════════════════════════════════
# MIGRATIONS AUTOMATIQUES
# ══════════════════════════════════════════════════════════════
def ensure_parrainage_tables(db):
    """Crée les tables parrainage et galerie si elles n'existent pas."""
    migrations = [
        # Table codes de parrainage clients
        """CREATE TABLE IF NOT EXISTS parrainage_codes (
            id         SERIAL PRIMARY KEY,
            code       VARCHAR(12) UNIQUE NOT NULL,
            parrain_tel VARCHAR NOT NULL,
            parrain_nom VARCHAR,
            nb_filleuls INTEGER DEFAULT 0,
            credit_total FLOAT DEFAULT 0.0,
            actif      BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        # Table utilisations parrainage
        """CREATE TABLE IF NOT EXISTS parrainage_utilisations (
            id          SERIAL PRIMARY KEY,
            code        VARCHAR(12) NOT NULL,
            filleul_tel VARCHAR NOT NULL,
            filleul_nom VARCHAR,
            commande_ref VARCHAR,
            reduction_appliquee FLOAT DEFAULT 0.0,
            created_at  TIMESTAMP DEFAULT NOW()
        )""",
        # Table galerie photos livraisons
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
    """Génère un code parrainage unique basé sur le tel + aléatoire."""
    suffix = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    prefix = tel[-4:] if len(tel) >= 4 else tel
    return f"FG{prefix}{suffix}"


# ══════════════════════════════════════════════════════════════
# PARRAINAGE — routes publiques
# ══════════════════════════════════════════════════════════════

@router.get("/parrainage/code/{tel}")
def get_mon_code(tel: str, db: Session = Depends(get_db)):
    """Retourne le code parrainage d'un client, le crée si besoin."""
    ensure_parrainage_tables(db)

    # Vérifier que le client a au moins une commande récupérée
    cmd = db.query(Commande).filter(
        Commande.client_tel == tel,
        Commande.statut == "recupere"
    ).first()
    if not cmd:
        raise HTTPException(403, "Vous devez avoir au moins une commande récupérée pour obtenir un code de parrainage.")

    # Chercher un code existant
    row = db.execute(text(
        "SELECT code, nb_filleuls, credit_total FROM parrainage_codes WHERE parrain_tel = :t AND actif = TRUE"
    ), {"t": tel}).mappings().first()

    if row:
        return {"code": row["code"], "nb_filleuls": row["nb_filleuls"], "credit_total": row["credit_total"]}

    # Créer un nouveau code
    code = gen_code_parrainage(tel)
    # Éviter collision
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
    return {"code": code, "nb_filleuls": 0, "credit_total": 0.0}


@router.get("/parrainage/verifier/{code}")
def verifier_code(code: str, db: Session = Depends(get_db)):
    """Vérifie si un code parrainage est valide avant de l'appliquer."""
    ensure_parrainage_tables(db)
    row = db.execute(text(
        "SELECT parrain_nom, nb_filleuls FROM parrainage_codes WHERE code = :c AND actif = TRUE"
    ), {"c": code.upper()}).mappings().first()
    if not row:
        raise HTTPException(404, "Code de parrainage invalide ou expiré.")
    return {"valide": True, "parrain_nom": row["parrain_nom"] or "un client FougahShop"}


@router.post("/parrainage/utiliser")
def utiliser_code(body: Dict[str, Any], db: Session = Depends(get_db)):
    """Enregistre l'utilisation d'un code par un filleul (appelé à la création de commande)."""
    ensure_parrainage_tables(db)
    code        = str(body.get("code", "")).upper().strip()
    filleul_tel = str(body.get("filleul_tel", "")).strip()
    filleul_nom = str(body.get("filleul_nom", "")).strip()
    commande_ref = str(body.get("commande_ref", "")).strip()
    reduction   = float(body.get("reduction_fcfa", 1000))

    if not code or not filleul_tel:
        raise HTTPException(400, "Code et téléphone requis.")

    # Vérifier le code
    parrain = db.execute(text(
        "SELECT parrain_tel FROM parrainage_codes WHERE code = :c AND actif = TRUE"
    ), {"c": code}).mappings().first()
    if not parrain:
        raise HTTPException(404, "Code invalide.")

    # Vérifier que le filleul n'a pas déjà utilisé ce code
    deja = db.execute(text(
        "SELECT 1 FROM parrainage_utilisations WHERE code = :c AND filleul_tel = :t"
    ), {"c": code, "t": filleul_tel}).fetchone()
    if deja:
        raise HTTPException(409, "Ce code a déjà été utilisé par ce numéro.")

    # Vérifier que le filleul ne parraine pas lui-même
    if parrain["parrain_tel"] == filleul_tel:
        raise HTTPException(400, "Vous ne pouvez pas utiliser votre propre code.")

    # Enregistrer l'utilisation
    db.execute(text(
        "INSERT INTO parrainage_utilisations (code, filleul_tel, filleul_nom, commande_ref, reduction_appliquee) "
        "VALUES (:c, :t, :n, :r, :red)"
    ), {"c": code, "t": filleul_tel, "n": filleul_nom, "r": commande_ref, "red": reduction})

    # Mettre à jour le compteur et crédit du parrain
    credit_parrain = reduction * 0.5  # Le parrain gagne 50% de la réduction accordée
    db.execute(text(
        "UPDATE parrainage_codes SET nb_filleuls = nb_filleuls + 1, credit_total = credit_total + :cr WHERE code = :c"
    ), {"cr": credit_parrain, "c": code})
    db.commit()
    return {"ok": True, "reduction_fcfa": reduction}


# ══════════════════════════════════════════════════════════════
# PARRAINAGE — routes admin
# ══════════════════════════════════════════════════════════════

@router.get("/admin/parrainage")
def liste_parrainages(request: Request, db: Session = Depends(get_db),
                      role: str = Depends(require_patron)):
    ensure_parrainage_tables(db)
    rows = db.execute(text(
        "SELECT p.code, p.parrain_nom, p.parrain_tel, p.nb_filleuls, p.credit_total, p.created_at, "
        "COUNT(u.id) as nb_util "
        "FROM parrainage_codes p "
        "LEFT JOIN parrainage_utilisations u ON u.code = p.code "
        "GROUP BY p.id ORDER BY p.nb_filleuls DESC, p.created_at DESC"
    )).mappings().all()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════
# GALERIE LIVRAISONS
# ══════════════════════════════════════════════════════════════

@router.get("/admin/galerie-all")
def get_galerie_admin(request: Request, db: Session = Depends(get_db),
                      role: str = Depends(require_patron)):
    """Retourne toutes les photos (visibles + masquées) pour l'admin."""
    ensure_parrainage_tables(db)
    rows = db.execute(text(
        "SELECT id, img_url, legende, pays, article, visible FROM galerie_livraisons "
        "ORDER BY ordre ASC, created_at DESC"
    )).mappings().all()
    return [dict(r) for r in rows]


@router.get("/galerie")
def get_galerie(db: Session = Depends(get_db)):
    """Retourne les photos visibles pour la landing."""
    ensure_parrainage_tables(db)
    rows = db.execute(text(
        "SELECT id, img_url, legende, pays, article FROM galerie_livraisons "
        "WHERE visible = TRUE ORDER BY ordre ASC, created_at DESC LIMIT 20"
    )).mappings().all()
    return [dict(r) for r in rows]


@router.post("/admin/galerie", status_code=201)
def add_galerie(body: Dict[str, Any], request: Request,
                db: Session = Depends(get_db), role: str = Depends(require_patron)):
    ensure_parrainage_tables(db)
    img_url = str(body.get("img_url", "")).strip()
    if not img_url:
        raise HTTPException(400, "URL image requise.")
    legende = str(body.get("legende", "")).strip()
    pays    = str(body.get("pays", "")).strip()
    article = str(body.get("article", "")).strip()
    db.execute(text(
        "INSERT INTO galerie_livraisons (img_url, legende, pays, article) VALUES (:u, :l, :p, :a)"
    ), {"u": img_url, "l": legende or None, "p": pays or None, "a": article or None})
    db.commit()
    return {"ok": True}


@router.delete("/admin/galerie/{item_id}")
def del_galerie(item_id: int, request: Request,
                db: Session = Depends(get_db), role: str = Depends(require_patron)):
    ensure_parrainage_tables(db)
    db.execute(text("DELETE FROM galerie_livraisons WHERE id = :id"), {"id": item_id})
    db.commit()
    return {"ok": True}


@router.patch("/admin/galerie/{item_id}/toggle")
def toggle_galerie(item_id: int, request: Request,
                   db: Session = Depends(get_db), role: str = Depends(require_patron)):
    ensure_parrainage_tables(db)
    db.execute(text(
        "UPDATE galerie_livraisons SET visible = NOT visible WHERE id = :id"
    ), {"id": item_id})
    db.commit()
    return {"ok": True}
