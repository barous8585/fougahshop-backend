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


def _normaliser_tel(tel: str) -> str:
    return tel.replace(" ", "").replace("-", "").replace("+", "").strip()


def gen_code_parrainage(tel: str) -> str:
    chars  = string.ascii_uppercase + string.digits
    suffix = ''.join(secrets.choice(chars) for _ in range(6))
    prefix = _normaliser_tel(tel)[-4:] if len(_normaliser_tel(tel)) >= 4 else _normaliser_tel(tel)
    return f"FG{prefix}{suffix}"


# ══════════════════════════════════════════════════════════════
# ROUTES PUBLIQUES
# ══════════════════════════════════════════════════════════════

@router.get("/parrainage/code/{tel}")
def get_mon_code(tel: str, db: Session = Depends(get_db)):
    tel_norm = _normaliser_tel(tel)

    cmd = db.execute(text("""
        SELECT client_nom, client_tel FROM commandes
        WHERE REPLACE(REPLACE(REPLACE(client_tel, ' ', ''), '-', ''), '+', '') = :t
        AND statut = 'recupere'
        LIMIT 1
    """), {"t": tel_norm}).mappings().first()

    if not cmd:
        raise HTTPException(403, "Vous devez avoir au moins une commande récupérée.")

    row = db.execute(text("""
        SELECT code, nb_filleuls, credit_total FROM parrainage_codes
        WHERE REPLACE(REPLACE(REPLACE(parrain_tel, ' ', ''), '-', ''), '+', '') = :t
        AND actif = TRUE
        LIMIT 1
    """), {"t": tel_norm}).mappings().first()

    if not row:
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
        nb_filleuls  = row["nb_filleuls"]
        credit_total = row["credit_total"]

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
    nb_commandes   = sum(1 for f in filleuls if f["statut"] in STATUTS_ACTIFS)

    # FIX: Utiliser credit_total en base comme source fiable
    # Recalculer uniquement si incohérence détectée
    try:
        cfg_row = db.execute(text(
            "SELECT gain_parrain FROM configs WHERE id=1 LIMIT 1"
        )).fetchone()
        gain_par_filleul = float(cfg_row[0]) if cfg_row and cfg_row[0] else 0
    except Exception:
        gain_par_filleul = 0

    # Recalculer le crédit réel depuis les utilisations actives
    credit_reel = round(
        sum(f["reduction_fcfa"] * 0.5 for f in filleuls if f["statut"] in STATUTS_ACTIFS)
        if not gain_par_filleul
        else gain_par_filleul * nb_commandes
    )
    # Toujours afficher au moins le credit_total enregistré en base
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
    rows = db.execute(text("""
        SELECT p.code, p.parrain_nom, p.parrain_tel,
               p.nb_filleuls, p.credit_total, p.created_at
        FROM parrainage_codes p
        ORDER BY p.nb_filleuls DESC, p.created_at DESC
        LIMIT 200
    """)).mappings().all()

    # FIX: Charger tous les filleuls en une seule query (évite N+1)
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
        filleuls = filleuls_map.get(d["code"], [])
        nb_actifs = sum(1 for f in filleuls if f["statut"] in STATUTS_ACTIFS)
        d["filleuls"]              = filleuls
        d["nb_commandes_actives"]  = nb_actifs
        d["created_at"]            = str(d.get("created_at", ""))
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
