from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, Any
from database import get_db
from routes.auth import require_auth, require_patron
from datetime import date, datetime

router = APIRouter(prefix="/api/promos", tags=["promos"])


# ══════════════════════════════════════════════════════════════
# TABLE — migration (appeler UNE SEULE FOIS au startup dans main.py)
# ══════════════════════════════════════════════════════════════

def ensure_tables(db: Session):
    """Crée ou migre la table promo_codes. À appeler au startup, pas à chaque requête."""
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                id             SERIAL PRIMARY KEY,
                code           VARCHAR UNIQUE NOT NULL,
                influenceur    VARCHAR,
                gain_influenceur FLOAT DEFAULT 0,
                type           VARCHAR DEFAULT 'fixe',
                valeur         FLOAT NOT NULL DEFAULT 0,
                reduction_fcfa FLOAT DEFAULT 0,
                client_tel     VARCHAR,
                max_uses       INTEGER DEFAULT 0,
                uses_count     INTEGER DEFAULT 0,
                quota          INTEGER DEFAULT 0,
                note           VARCHAR,
                expiry         DATE,
                actif          BOOLEAN DEFAULT TRUE,
                created_at     TIMESTAMP DEFAULT NOW()
            )
        """))
        migrations = [
            "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS type VARCHAR DEFAULT 'fixe'",
            "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS valeur FLOAT DEFAULT 0",
            "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS client_tel VARCHAR",
            "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS max_uses INTEGER DEFAULT 0",
            "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS uses_count INTEGER DEFAULT 0",
            "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS note VARCHAR",
            "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS expiry DATE",
            "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS gain_influenceur FLOAT DEFAULT 0",
            "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS pays VARCHAR DEFAULT NULL",
            "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS quota INTEGER DEFAULT 0",
            "UPDATE promo_codes SET valeur = reduction_fcfa WHERE valeur = 0 AND reduction_fcfa > 0",
            "ALTER TABLE commandes ADD COLUMN IF NOT EXISTS promo_code VARCHAR",
        ]
        for sql in migrations:
            try:
                db.execute(text(sql))
            except Exception:
                db.rollback()
        try:
            db.execute(text(
                "UPDATE promo_codes SET uses_count = utilisations "
                "WHERE uses_count = 0 AND utilisations > 0"
            ))
        except Exception:
            pass
        try:
            db.execute(text(
                "UPDATE promo_codes SET max_uses = quota "
                "WHERE max_uses = 0 AND quota > 0"
            ))
        except Exception:
            pass
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[promo] ensure_tables error: {e}")


def is_expired(expiry) -> bool:
    if not expiry:
        return False
    try:
        if isinstance(expiry, (date, datetime)):
            return expiry < date.today() if isinstance(expiry, date) else expiry.date() < date.today()
        return str(expiry) < str(date.today())
    except Exception:
        return False


def check_quota(p) -> bool:
    max_u = getattr(p, "max_uses", 0) or getattr(p, "quota", 0) or 0
    if max_u == 0:
        return True
    uses = getattr(p, "uses_count", 0) or getattr(p, "utilisations", 0) or 0
    return uses < max_u


# ══════════════════════════════════════════════════════════════
# ENDPOINTS PUBLICS
# ══════════════════════════════════════════════════════════════

@router.get("/verifier/{code}")
def verifier_code_get(code: str, db: Session = Depends(get_db)):
    """Vérification publique d'un code promo. Pas d'ensure_tables ici — fait au startup."""
    code = code.strip().upper()
    row = db.execute(
        text("SELECT * FROM promo_codes WHERE code=:code AND actif=TRUE LIMIT 1"),
        {"code": code}
    ).fetchone()
    if not row:
        return {"valide": False, "message": "Code invalide ou désactivé."}
    if is_expired(row.expiry):
        return {"valide": False, "message": "Ce code promo a expiré."}
    if not check_quota(row):
        return {"valide": False, "message": "Ce code a atteint son quota d'utilisations."}

    valeur = row.valeur or row.reduction_fcfa or 0
    type_  = row.type or "fixe"
    if type_ == "livraison":
        msg = "Code valide — livraison locale gratuite"
    else:
        msg = f"Code valide — réduction de {int(valeur)}{'%' if type_ == 'pct' else ' FCFA'}"

    return {
        "valide":         True,
        "code":           row.code,
        "type":           type_,
        "valeur":         valeur,
        "valeur_fcfa":    valeur if type_ == "fixe" else None,
        "reduction_fcfa": valeur if type_ in ("fixe",) else None,
        "influenceur":    getattr(row, "influenceur", None),
        "message":        msg,
    }


@router.post("/verifier")
def verifier_code_post(body: Dict[str, Any], db: Session = Depends(get_db)):
    code = str(body.get("code", "")).strip().upper()
    if not code:
        raise HTTPException(400, "Code manquant")
    return verifier_code_get(code, db)


# ✅ NOUVEAU — Endpoint public pour la page influenceur
@router.get("/influenceur/{code}")
def get_stats_influenceur(code: str, db: Session = Depends(get_db)):
    """
    Stats publiques pour un influenceur — pas d'authentification requise.
    Accessible uniquement pour les codes avec un champ influenceur renseigné.
    """
    row = db.execute(
        text("SELECT * FROM promo_codes WHERE code=:code AND actif=TRUE LIMIT 1"),
        {"code": code.strip().upper()}
    ).mappings().first()

    if not row:
        raise HTTPException(404, "Code introuvable ou inactif.")

    promo = dict(row)
    influenceur = promo.get("influenceur") or ""
    if not influenceur:
        raise HTTPException(404, "Ce code n'est pas un code influenceur.")

    uses_count   = int(promo.get("uses_count") or promo.get("utilisations") or 0)
    gain_par_cmd = float(promo.get("gain_influenceur") or 0)

    # Commandes générées par ce code
    try:
        cmds_rows = db.execute(text("""
            SELECT ref, statut, created_at
            FROM commandes
            WHERE promo_code = :code
            ORDER BY created_at DESC
            LIMIT 50
        """), {"code": code.strip().upper()}).mappings().all()
        commandes = [dict(r) for r in cmds_rows]
    except Exception:
        commandes = []

    # ✅ Gain calculé sur commandes actives seulement (exclut annulées/refusées)
    STATUTS_EXCLUS = {"annulee", "paiement_refuse", "en_attente_paiement"}
    commandes_actives = [c for c in commandes if c.get("statut") not in STATUTS_EXCLUS]
    gain_total = round(gain_par_cmd * len(commandes_actives))
    ca_euro    = sum(float(c.get("total_euro") or 0) for c in commandes_actives)

    return {
        "code":             promo["code"],
        "influenceur":      influenceur,
        "pays":             promo.get("pays") or "",   # ✅ pays de l'influenceur
        "actif":            True,
        "type":             promo.get("type", "fixe"),
        "valeur":           float(promo.get("valeur") or 0),
        "uses_count":       uses_count,
        "nb_commandes":     len(commandes_actives),
        "gain_par_cmd":     gain_par_cmd,
        "gain_influenceur": gain_par_cmd,
        "gain_total":       gain_total,
        "expiry":           str(promo.get("expiry") or ""),
        "commandes":        commandes,
    }


# ══════════════════════════════════════════════════════════════
# ENDPOINTS ADMIN
# ══════════════════════════════════════════════════════════════

@router.get("/admin")
def list_promos(
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)   # ✅ require_patron — pas require_auth
):
    """Liste tous les codes promo avec stats."""
    rows = db.execute(
        text("SELECT * FROM promo_codes ORDER BY created_at DESC")
    ).fetchall()

    result = []
    for p in rows:
        try:
            cmds = db.execute(
                text("SELECT ref, statut, total_euro FROM commandes WHERE promo_code=:code"),
                {"code": p.code}
            ).fetchall()
        except Exception:
            cmds = []

        uses  = p.uses_count or getattr(p, "utilisations", 0) or 0
        max_u = p.max_uses or p.quota or 0
        valeur = p.valeur or p.reduction_fcfa or 0
        type_  = p.type or "fixe"
        gain_par_cmd = getattr(p, "gain_influenceur", 0) or 0
        gain  = gain_par_cmd * uses
        ca    = sum(c.total_euro or 0 for c in cmds)

        result.append({
            "id":          p.id,
            "code":        p.code,
            "type":        type_,
            "valeur":      valeur,
            "reduction_fcfa": valeur if type_ == "fixe" else None,
            "influenceur": getattr(p, "influenceur", None),
            "gain_influenceur": gain_par_cmd,
            "gain_par_cmd":    gain_par_cmd,
            "client_tel":  getattr(p, "client_tel", None),
            "max_uses":    max_u,
            "uses_count":  uses,
            "quota":       max_u,
            "utilisations": uses,
            "utilisations_restantes": max(0, max_u - uses) if max_u > 0 else None,
            "note":        getattr(p, "note", None),
            "expiry":      str(p.expiry) if p.expiry else None,
            "actif":       bool(p.actif),
            "ca_euro":     round(ca, 2),
            "gain_total_fcfa": round(gain),
            "commandes":   [{"ref": c.ref, "statut": c.statut} for c in cmds],
            "created_at":  str(p.created_at),
        })
    return result


@router.post("", status_code=201)
@router.post("/", status_code=201)
def create_promo(
    body: Dict[str, Any],
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    code = str(body.get("code", "")).strip().upper()
    if not code:
        raise HTTPException(400, "Code manquant")
    if len(code) < 3:
        raise HTTPException(400, "Le code doit faire au moins 3 caractères")

    existing = db.execute(
        text("SELECT id FROM promo_codes WHERE code=:code"), {"code": code}
    ).fetchone()
    if existing:
        raise HTTPException(400, "Ce code existe déjà")

    type_ = str(body.get("type", "fixe")).lower()
    if type_ not in ("fixe", "pct", "livraison"):
        raise HTTPException(400, "Type invalide (fixe, pct ou livraison)")

    valeur = float(body.get("valeur", body.get("reduction_fcfa", 0)))
    if valeur <= 0:
        raise HTTPException(400, "La valeur doit être positive")
    if type_ == "pct" and valeur > 100:
        raise HTTPException(400, "Le pourcentage ne peut pas dépasser 100")

    max_uses   = int(body.get("max_uses", body.get("quota", 0)))
    expiry_raw = body.get("expiry") or None
    expiry     = None
    if expiry_raw:
        try:
            expiry = date.fromisoformat(str(expiry_raw))
        except ValueError:
            raise HTTPException(400, "Format de date invalide (YYYY-MM-DD)")

    row = db.execute(text("""
        INSERT INTO promo_codes
            (code, type, valeur, reduction_fcfa,
             influenceur, gain_influenceur,
             client_tel, max_uses, quota,
             uses_count, note, expiry, pays, actif)
        VALUES
            (:code, :type, :valeur, :reduction_fcfa,
             :influenceur, :gain_influenceur,
             :client_tel, :max_uses, :max_uses,
             0, :note, :expiry, :pays, TRUE)
        RETURNING id, code
    """), {
        "code":             code,
        "type":             type_,
        "valeur":           valeur,
        "reduction_fcfa":   valeur if type_ == "fixe" else 0,
        "influenceur":      body.get("influenceur") or None,
        "gain_influenceur": float(body.get("gain_influenceur", 0)),
        "client_tel":       body.get("client_tel") or None,
        "max_uses":         max_uses,
        "note":             body.get("note") or None,
        "expiry":           expiry,
        "pays":             str(body.get("pays", "")).strip() or None,
    }).fetchone()

    db.commit()
    return {"id": row.id, "code": row.code, "ok": True}


@router.patch("/{code}/toggle")
def toggle_promo(
    code: str,
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    code = code.strip().upper()
    row = db.execute(
        text("SELECT id, actif FROM promo_codes WHERE code=:code"), {"code": code}
    ).fetchone()
    if not row:
        raise HTTPException(404, "Code introuvable")
    db.execute(
        text("UPDATE promo_codes SET actif=:actif WHERE code=:code"),
        {"actif": not bool(row.actif), "code": code}
    )
    db.commit()
    return {"ok": True, "actif": not bool(row.actif)}


@router.patch("/admin/{promo_id}")
def update_promo_by_id(
    promo_id: int,
    body: Dict[str, Any],
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    # Whitelist stricte des colonnes modifiables
    ALLOWED_COLS = {
        "actif", "type", "valeur", "reduction_fcfa",
        "quota", "max_uses", "gain_influenceur",
        "note", "expiry", "client_tel"
    }
    updates, params = [], {"id": promo_id}

    if "actif"           in body: updates.append("actif=:actif");                      params["actif"]           = bool(body["actif"])
    if "type"            in body: updates.append("type=:type");                         params["type"]            = str(body["type"])
    if "valeur"          in body: updates.append("valeur=:valeur");                     params["valeur"]          = float(body["valeur"])
    if "reduction_fcfa"  in body: updates.append("reduction_fcfa=:reduction_fcfa");     params["reduction_fcfa"]  = float(body["reduction_fcfa"])
    if "gain_influenceur"in body: updates.append("gain_influenceur=:gain_influenceur"); params["gain_influenceur"]= float(body["gain_influenceur"])
    if "note"            in body: updates.append("note=:note");                         params["note"]            = body["note"] or None
    if "expiry"          in body: updates.append("expiry=:expiry");                     params["expiry"]          = body["expiry"] or None
    if "client_tel"      in body: updates.append("client_tel=:client_tel");             params["client_tel"]      = body["client_tel"] or None
    if "quota"           in body:
        updates += ["quota=:quota","max_uses=:quota"]; params["quota"] = int(body["quota"])
    if "max_uses"        in body:
        updates += ["max_uses=:max_uses","quota=:max_uses"]; params["max_uses"] = int(body["max_uses"])
    if body.get("reset_utilisations"):
        updates += ["uses_count=0","actif=TRUE"]

    if updates:
        # Colonnes construites depuis la whitelist — pas d'injection possible
        db.execute(text(f"UPDATE promo_codes SET {', '.join(updates)} WHERE id=:id"), params)
        db.commit()
    return {"ok": True}


@router.delete("/{code}")
def delete_promo_by_code(
    code: str,
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    code = code.strip().upper()
    result = db.execute(
        text("DELETE FROM promo_codes WHERE code=:code RETURNING id"), {"code": code}
    ).fetchone()
    if not result:
        raise HTTPException(404, "Code introuvable")
    db.commit()
    return {"ok": True}


@router.delete("/admin/{promo_id}")
def delete_promo_by_id(
    promo_id: int,
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    db.execute(text("DELETE FROM promo_codes WHERE id=:id"), {"id": promo_id})
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════
# FONCTION INTERNE — appelée depuis commandes.py
# ══════════════════════════════════════════════════════════════

def utiliser_code(code: str, db: Session):
    """
    ✅ CORRIGÉ — Incrémente uses_count UNE SEULE FOIS.
    Ancienne version incrémentait uses_count ET utilisations → doublait le compteur.
    """
    if not code:
        return
    try:
        db.execute(
            text("UPDATE promo_codes SET uses_count = uses_count + 1 WHERE code=:code"),
            {"code": code.strip().upper()}
        )
        db.flush()
    except Exception as e:
        print(f"[promo] utiliser_code uses_count error: {e}")
        try:
            db.execute(
                text("UPDATE promo_codes SET utilisations = utilisations + 1 WHERE code=:code"),
                {"code": code.strip().upper()}
            )
            db.flush()
        except Exception:
            pass
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[promo] utiliser_code commit error: {e}")
