from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, Any
from database import get_db
from routes.auth import require_auth

router = APIRouter(prefix="/api/promo", tags=["promo"])

def ensure_tables(db):
    """Créer la table promo_codes si elle n'existe pas"""
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                id SERIAL PRIMARY KEY,
                code VARCHAR UNIQUE NOT NULL,
                influenceur VARCHAR,
                reduction_fcfa FLOAT DEFAULT 500,
                gain_influenceur FLOAT DEFAULT 1000,
                quota INTEGER DEFAULT 50,
                utilisations INTEGER DEFAULT 0,
                actif BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        db.execute(text("ALTER TABLE commandes ADD COLUMN IF NOT EXISTS promo_code VARCHAR"))
        db.commit()
    except Exception:
        db.rollback()

@router.post("/verifier")
def verifier_code(body: Dict[str, Any], db: Session = Depends(get_db)):
    ensure_tables(db)
    code = str(body.get("code", "")).strip().upper()
    if not code:
        raise HTTPException(400, "Code manquant")
    result = db.execute(text(
        "SELECT * FROM promo_codes WHERE code=:code AND actif=TRUE LIMIT 1"
    ), {"code": code}).fetchone()
    if not result:
        raise HTTPException(404, "Code invalide ou expiré")
    if result.utilisations >= result.quota:
        raise HTTPException(400, "Code épuisé")
    return {
        "valide": True,
        "code": result.code,
        "influenceur": result.influenceur,
        "reduction_fcfa": result.reduction_fcfa,
        "utilisations_restantes": result.quota - result.utilisations,
    }

@router.get("/admin")
def list_promos(request: Request, db: Session = Depends(get_db), role: str = Depends(require_auth)):
    ensure_tables(db)
    promos = db.execute(text("SELECT * FROM promo_codes ORDER BY created_at DESC")).fetchall()
    result = []
    for p in promos:
        try:
            cmds = db.execute(text(
                "SELECT ref, statut, total_euro FROM commandes WHERE promo_code=:code"
            ), {"code": p.code}).fetchall()
        except Exception:
            cmds = []
        ca_euro = sum(c.total_euro or 0 for c in cmds)
        gain_total = (p.gain_influenceur or 1000) * (p.utilisations or 0)
        result.append({
            "id": p.id, "code": p.code, "influenceur": p.influenceur,
            "reduction_fcfa": p.reduction_fcfa, "gain_influenceur": p.gain_influenceur,
            "quota": p.quota, "utilisations": p.utilisations or 0,
            "utilisations_restantes": max(0, (p.quota or 50) - (p.utilisations or 0)),
            "actif": p.actif, "ca_euro": round(ca_euro, 2),
            "gain_total_fcfa": round(gain_total),
            "commandes": [{"ref": c.ref, "statut": c.statut} for c in cmds],
            "created_at": str(p.created_at),
        })
    return result

@router.post("/admin", status_code=201)
def create_promo(body: Dict[str, Any], request: Request, db: Session = Depends(get_db), role: str = Depends(require_auth)):
    ensure_tables(db)
    code = str(body.get("code", "")).strip().upper()
    if not code:
        raise HTTPException(400, "Code manquant")
    existing = db.execute(text("SELECT id FROM promo_codes WHERE code=:code"), {"code": code}).fetchone()
    if existing:
        raise HTTPException(400, "Code déjà existant")
    result = db.execute(text("""
        INSERT INTO promo_codes (code, influenceur, reduction_fcfa, gain_influenceur, quota)
        VALUES (:code, :influenceur, :reduction_fcfa, :gain_influenceur, :quota)
        RETURNING id, code
    """), {
        "code": code,
        "influenceur": str(body.get("influenceur", "")),
        "reduction_fcfa": float(body.get("reduction_fcfa", 500)),
        "gain_influenceur": float(body.get("gain_influenceur", 1000)),
        "quota": int(body.get("quota", 50)),
    }).fetchone()
    db.commit()
    return {"id": result.id, "code": result.code}

@router.patch("/admin/{promo_id}")
def update_promo(promo_id: int, body: Dict[str, Any], request: Request, db: Session = Depends(get_db), role: str = Depends(require_auth)):
    updates = []
    params = {"id": promo_id}
    if "actif" in body: updates.append("actif=:actif"); params["actif"] = bool(body["actif"])
    if "quota" in body: updates.append("quota=:quota"); params["quota"] = int(body["quota"])
    if "reduction_fcfa" in body: updates.append("reduction_fcfa=:reduction_fcfa"); params["reduction_fcfa"] = float(body["reduction_fcfa"])
    if "gain_influenceur" in body: updates.append("gain_influenceur=:gain_influenceur"); params["gain_influenceur"] = float(body["gain_influenceur"])
    if body.get("reset_utilisations"):
        updates.append("utilisations=0")
        updates.append("actif=TRUE")
    if updates:
        db.execute(text(f"UPDATE promo_codes SET {', '.join(updates)} WHERE id=:id"), params)
        db.commit()
    return {"ok": True}

@router.delete("/admin/{promo_id}")
def delete_promo(promo_id: int, request: Request, db: Session = Depends(get_db), role: str = Depends(require_auth)):
    db.execute(text("DELETE FROM promo_codes WHERE id=:id"), {"id": promo_id})
    db.commit()
    return {"ok": True}
