from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, Any
import secrets, string
from database import get_db
from models import Commande
from routes.auth import require_auth, require_patron

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

    # Resync nb_filleuls et credit_total depuis les vraies utilisations
    _resync_parrainage(db)


def _normaliser_tel(tel: str) -> str:
    """Normalise un numéro de téléphone — garde uniquement les chiffres."""
    return ''.join(filter(str.isdigit, tel or ""))


def _suffix(tel: str, n: int = 8) -> str:
    """Retourne les n derniers chiffres du téléphone."""
    digits = _normaliser_tel(tel)
    return digits[-n:] if len(digits) >= n else digits


def _resync_parrainage(db: Session):
    """
    Resync nb_filleuls et credit_total depuis les vraies données.
    Corrige les compteurs à 0 causés par des bugs de normalisation passés.
    """
    try:
        cfg_row = db.execute(text(
            "SELECT gain_parrain FROM configs WHERE id=1 LIMIT 1"
        )).fetchone()
        gain_par_filleul = float(cfg_row[0]) if cfg_row and cfg_row[0] else 500.0
    except Exception:
        gain_par_filleul = 500.0

    try:
        # Recalculer nb_filleuls depuis parrainage_utilisations
        db.execute(text("""
            UPDATE parrainage_codes p
            SET nb_filleuls = (
                SELECT COUNT(DISTINCT u.filleul_tel)
                FROM parrainage_utilisations u
                WHERE u.code = p.code
            )
        """))
        # Recalculer credit_total = nb_filleuls_actifs * gain_par_filleul
        db.execute(text("""
            UPDATE parrainage_codes p
            SET credit_total = (
                SELECT COUNT(*) * :gain
                FROM parrainage_utilisations u
                LEFT JOIN commandes c ON c.ref = u.commande_ref
                WHERE u.code = p.code
                  AND (c.statut IS NULL OR c.statut NOT IN ('annulee', 'paiement_refuse'))
            )
        """), {"gain": gain_par_filleul})
        db.commit()
        print("[parrainage] ✅ Resync nb_filleuls + credit_total effectué")
    except Exception as e:
        db.rollback()
        print(f"[parrainage] Erreur resync: {e}")


def gen_code_parrainage(tel: str) -> str:
    chars  = string.ascii_uppercase + string.digits
    suffix = ''.join(secrets.choice(chars) for _ in range(6))
    digits = _normaliser_tel(tel)
    prefix = digits[-4:] if len(digits) >= 4 else digits
    return f"FG{prefix}{suffix}"


# ══════════════════════════════════════════════════════════════
# ROUTES PUBLIQUES
# ══════════════════════════════════════════════════════════════

@router.get("/parrainage/code/{tel}")
def get_mon_code(tel: str, db: Session = Depends(get_db)):
    tel_norm = _normaliser_tel(tel)
    tel_suffix = tel_norm[-9:] if len(tel_norm) >= 9 else tel_norm

    # Chercher une commande récupérée avec ce numéro (suffixe)
    cmd = db.execute(text("""
        SELECT client_nom, client_tel FROM commandes
        WHERE REGEXP_REPLACE(client_tel, '[^0-9]', '', 'g') LIKE :pattern
        AND statut = 'recupere'
        ORDER BY created_at DESC
        LIMIT 1
    """), {"pattern": f"%{tel_suffix}%"}).mappings().first()

    if not cmd:
        # Fallback sans REGEXP_REPLACE
        try:
            cmd = db.execute(text("""
                SELECT client_nom, client_tel FROM commandes
                WHERE REPLACE(REPLACE(REPLACE(client_tel,' ',''),'-',''),'+','') LIKE :pattern
                AND statut = 'recupere'
                ORDER BY created_at DESC
                LIMIT 1
            """), {"pattern": f"%{tel_suffix}%"}).mappings().first()
        except Exception:
            pass

    if not cmd:
        raise HTTPException(403, "Vous devez avoir au moins une commande récupérée.")

    # Chercher un code existant pour ce numéro (suffixe robuste)
    row = db.execute(text("""
        SELECT code, nb_filleuls, credit_total FROM parrainage_codes
        WHERE REGEXP_REPLACE(parrain_tel, '[^0-9]', '', 'g') LIKE :pattern
        AND actif = TRUE
        ORDER BY created_at ASC
        LIMIT 1
    """), {"pattern": f"%{tel_suffix}%"}).mappings().first()

    if not row:
        # Fallback
        try:
            row = db.execute(text("""
                SELECT code, nb_filleuls, credit_total FROM parrainage_codes
                WHERE REPLACE(REPLACE(REPLACE(parrain_tel,' ',''),'-',''),'+','') LIKE :pattern
                AND actif = TRUE
                ORDER BY created_at ASC
                LIMIT 1
            """), {"pattern": f"%{tel_suffix}%"}).mappings().first()
        except Exception:
            pass

    if not row:
        # Créer un nouveau code
        code = gen_code_parrainage(tel)
        for _ in range(10):
            exists = db.execute(
                text("SELECT 1 FROM parrainage_codes WHERE code = :c"), {"c": code}
            ).fetchone()
            if not exists:
                break
            code = gen_code_parrainage(tel)

        nom        = cmd["client_nom"] or ""
        tel_stocke = cmd["client_tel"] or tel
        db.execute(text(
            "INSERT INTO parrainage_codes (code, parrain_tel, parrain_nom) "
            "VALUES (:c, :t, :n)"
        ), {"c": code, "t": tel_stocke, "n": nom})
        db.commit()
        nb_filleuls  = 0
        credit_total = 0.0
    else:
        code         = row["code"]
        nb_filleuls  = row["nb_filleuls"] or 0
        credit_total = row["credit_total"] or 0.0

    # Charger les filleuls avec statut commande
    filleuls_rows = db.execute(text("""
        SELECT u.filleul_nom, u.filleul_tel, u.reduction_appliquee,
               u.commande_ref, u.created_at, c.statut
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
            "created_at":     str(f["created_at"]) if f["created_at"] else "",
        }
        for f in filleuls_rows
    ]

    STATUTS_ACTIFS = {"paye", "achete", "expedie", "arrive", "recupere"}
    nb_commandes = sum(1 for f in filleuls if f["statut"] in STATUTS_ACTIFS)

    # Gain parrain depuis config
    try:
        cfg_row = db.execute(text(
            "SELECT gain_parrain FROM configs WHERE id=1 LIMIT 1"
        )).fetchone()
        gain_par_filleul = float(cfg_row[0]) if cfg_row and cfg_row[0] else 500.0
    except Exception:
        gain_par_filleul = 500.0

    # Recalcul crédit réel depuis filleuls actifs
    credit_reel    = round(gain_par_filleul * nb_commandes)
    credit_affiche = max(credit_reel, round(credit_total))

    return {
        "code":         code,
        "nb_filleuls":  nb_filleuls,
        "nb_commandes": nb_commandes,
        "credit_total": credit_affiche,
        "gain_total":   credit_affiche,
        "filleuls":     filleuls,
    }


@router.get("/parrainage/verifier/{code}")
def verifier_code(code: str, db: Session = Depends(get_db)):
    code_upper = code.upper().strip()
    row = db.execute(text(
        "SELECT parrain_nom FROM parrainage_codes WHERE code = :c AND actif = TRUE"
    ), {"c": code_upper}).mappings().first()
    if not row:
        raise HTTPException(404, "Code de parrainage invalide ou expiré.")

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
    role: str = Depends(require_patron),
):
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

    if _suffix(filleul_tel) == _suffix(parrain["parrain_tel"]):
        raise HTTPException(400, "Vous ne pouvez pas utiliser votre propre code.")

    filleul_suffix = _suffix(filleul_tel)
    deja = db.execute(text("""
        SELECT 1 FROM parrainage_utilisations u
        WHERE u.code = :c
        AND REGEXP_REPLACE(u.filleul_tel, '[^0-9]', '', 'g') LIKE :pattern
    """), {"c": code, "pattern": f"%{filleul_suffix}%"}).fetchone()

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
    # Resync avant affichage pour avoir les données fraîches
    _resync_parrainage(db)

    rows = db.execute(text("""
        SELECT p.code, p.parrain_nom, p.parrain_tel,
               p.nb_filleuls, p.credit_total, p.created_at
        FROM parrainage_codes p
        ORDER BY p.nb_filleuls DESC, p.created_at DESC
        LIMIT 200
    """)).mappings().all()

    codes = [r["code"] for r in rows]
    filleuls_map: Dict[str, list] = {c: [] for c in codes}

    if codes:
        placeholders = ', '.join(f':c{i}' for i in range(len(codes)))
        params = {f'c{i}': c for i, c in enumerate(codes)}
        try:
            filleuls_rows = db.execute(text(f"""
                SELECT u.code, u.filleul_nom, u.filleul_tel,
                       u.reduction_appliquee, u.commande_ref, u.created_at,
                       c.statut
                FROM parrainage_utilisations u
                LEFT JOIN commandes c ON c.ref = u.commande_ref
                WHERE u.code IN ({placeholders})
                ORDER BY u.created_at DESC
            """), params).mappings().all()

            for f in filleuls_rows:
                code_f = f["code"]
                if code_f in filleuls_map:
                    filleuls_map[code_f].append({
                        "filleul_nom":    f["filleul_nom"] or "Client",
                        "filleul_tel":    f["filleul_tel"] or "",
                        "statut":         f["statut"] or "en_attente_paiement",
                        "reduction_fcfa": float(f["reduction_appliquee"] or 0),
                        "commande_ref":   f["commande_ref"] or "",
                        "created_at":     str(f["created_at"]) if f["created_at"] else "",
                    })
        except Exception as e:
            print(f"[parrainage] Erreur chargement filleuls: {e}")

    STATUTS_ACTIFS = {"paye", "achete", "expedie", "arrive", "recupere"}
    result = []
    for r in rows:
        d = dict(r)
        filleuls  = filleuls_map.get(d["code"], [])
        nb_actifs = sum(1 for f in filleuls if f["statut"] in STATUTS_ACTIFS)
        d["filleuls"]             = filleuls
        d["nb_commandes_actives"] = nb_actifs
        d["created_at"]           = str(d.get("created_at", ""))
        result.append(d)

    return result


@router.post("/admin/parrainage/resync")
def resync_admin(request: Request, db: Session = Depends(get_db),
                 role: str = Depends(require_patron)):
    _resync_parrainage(db)
    return {"ok": True, "message": "Parrainage resynchronisé depuis les données réelles"}


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
