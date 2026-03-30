from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, Any
from database import get_db
from models import PromoCode, Commande
from routes.auth import require_auth

router = APIRouter(prefix="/api/promo", tags=["promo"])

def ensure_promo_columns(db):
    """Ajouter les colonnes promo si elles n'existent pas (migration douce)"""
    try:
        db.execute(text("ALTER TABLE commandes ADD COLUMN promo_code VARCHAR"))
        db.commit()
    except Exception:
        pass
    try:
        db.execute(text("ALTER TABLE commandes ADD COLUMN promo_reduction FLOAT DEFAULT 0"))
        db.commit()
    except Exception:
        pass

@router.post("/verifier")
def verifier_code(body: Dict[str, Any], db: Session = Depends(get_db)):
    code = str(body.get("code", "")).strip().upper()
    if not code:
        raise HTTPException(400, "Code manquant")
    promo = db.query(PromoCode).filter(
        PromoCode.code == code,
        PromoCode.actif == True
    ).first()
    if not promo:
        raise HTTPException(404, "Code invalide ou expiré")
    if promo.utilisations >= promo.quota:
        raise HTTPException(400, "Code épuisé")
    return {
        "valide": True,
        "code": promo.code,
        "influenceur": promo.influenceur,
        "reduction_fcfa": promo.reduction_fcfa,
        "utilisations_restantes": promo.quota - promo.utilisations,
    }

@router.get("/admin")
def list_promos(request: Request, db: Session = Depends(get_db), role: str = Depends(require_auth)):
    ensure_promo_columns(db)
    promos = db.query(PromoCode).order_by(PromoCode.created_at.desc()).all()
    result = []
    for p in promos:
        try:
            cmds = db.query(Commande).filter(Commande.promo_code == p.code).all()
        except Exception:
            cmds = []
        ca_euro = sum(c.total_euro or 0 for c in cmds)
        gain_total = p.gain_influenceur * p.utilisations
        result.append({
            "id": p.id, "code": p.code, "influenceur": p.influenceur,
            "reduction_fcfa": p.reduction_fcfa, "gain_influenceur": p.gain_influenceur,
            "quota": p.quota, "utilisations": p.utilisations,
            "utilisations_restantes": max(0, p.quota - p.utilisations),
            "actif": p.actif, "ca_euro": round(ca_euro, 2),
            "gain_total_fcfa": round(gain_total),
            "commandes": [{"ref": c.ref, "statut": c.statut} for c in cmds],
            "created_at": str(p.created_at),
        })
    return result

@router.post("/admin", status_code=201)
def create_promo(body: Dict[str, Any], request: Request, db: Session = Depends(get_db), role: str = Depends(require_auth)):
    code = str(body.get("code", "")).strip().upper()
    if not code:
        raise HTTPException(400, "Code manquant")
    if db.query(PromoCode).filter(PromoCode.code == code).first():
        raise HTTPException(400, "Code déjà existant")
    promo = PromoCode(
        code=code, influenceur=str(body.get("influenceur", "")),
        reduction_fcfa=float(body.get("reduction_fcfa", 500)),
        gain_influenceur=float(body.get("gain_influenceur", 1000)),
        quota=int(body.get("quota", 50)),
    )
    db.add(promo); db.commit(); db.refresh(promo)
    return {"id": promo.id, "code": promo.code}

@router.patch("/admin/{promo_id}")
def update_promo(promo_id: int, body: Dict[str, Any], request: Request, db: Session = Depends(get_db), role: str = Depends(require_auth)):
    promo = db.query(PromoCode).filter(PromoCode.id == promo_id).first()
    if not promo:
        raise HTTPException(404, "Code introuvable")
    if "actif" in body: promo.actif = bool(body["actif"])
    if "quota" in body: promo.quota = int(body["quota"])
    if "reduction_fcfa" in body: promo.reduction_fcfa = float(body["reduction_fcfa"])
    if "gain_influenceur" in body: promo.gain_influenceur = float(body["gain_influenceur"])
    db.commit()
    return {"ok": True}

@router.delete("/admin/{promo_id}")
def delete_promo(promo_id: int, request: Request, db: Session = Depends(get_db), role: str = Depends(require_auth)):
    promo = db.query(PromoCode).filter(PromoCode.id == promo_id).first()
    if promo: promo.actif = False; db.commit()
    return {"ok": True}
