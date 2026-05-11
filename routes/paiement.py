from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from typing import Dict, Any
import httpx, os, json, html as html_module
from database import get_db
from models import Commande

router = APIRouter(prefix="/api/paiement", tags=["paiement"])

CINETPAY_SITE_ID = os.environ.get("CINETPAY_SITE_ID", "")
CINETPAY_API_KEY = os.environ.get("CINETPAY_API_KEY", "")
CINETPAY_URL     = "https://api-checkout.cinetpay.com/v2/payment"
APP_URL          = os.environ.get("APP_URL", "https://fougahshop.com")


@router.post("/init")
async def init_paiement(body: Dict[str, Any], db: Session = Depends(get_db)):
    ref = str(body.get("ref", ""))
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    if cmd.statut != "en_attente_paiement":
        raise HTTPException(400, "Commande déjà payée")
    if not CINETPAY_SITE_ID or not CINETPAY_API_KEY:
        return {"mode": "test", "message": "CinetPay non configuré",
                "payment_url": f"{APP_URL}/paiement-test?ref={cmd.ref}", "ref": cmd.ref}
    payload = {
        "apikey":                 CINETPAY_API_KEY,
        "site_id":                CINETPAY_SITE_ID,
        "transaction_id":         cmd.ref,
        "amount":                 int(cmd.total_local),
        "currency":               "XOF" if cmd.monnaie == "FCFA" else "GNF",
        "description":            f"FougahShop — {cmd.nb_articles} article(s) — {cmd.ref}",
        "return_url":             f"{APP_URL}/api/paiement/retour?ref={cmd.ref}",
        "notify_url":             f"{APP_URL}/api/paiement/webhook",
        "customer_name":          cmd.client_nom.split()[0] if cmd.client_nom else "Client",
        "customer_surname":       cmd.client_nom.split()[-1] if cmd.client_nom else "",
        "customer_phone_number":  cmd.client_tel,
        "customer_address":       cmd.client_adresse or "",
        "customer_city":          cmd.client_pays,
        "customer_country":       "CI",
        "channels":               "ALL",
        "lang":                   "fr",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(CINETPAY_URL, json=payload)
        data = r.json()
    if data.get("code") != "201":
        raise HTTPException(400, f"Erreur CinetPay: {data.get('message', 'Inconnue')}")
    cmd.paiement_ref = data["data"]["payment_token"]
    db.commit()
    return {"payment_url": data["data"]["payment_url"], "ref": cmd.ref}


@router.post("/webhook")
async def webhook_cinetpay(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    ref       = body.get("cpm_trans_id") or body.get("transaction_id")
    statut_cp = body.get("cpm_result")   or body.get("status")
    if not ref:
        return {"ok": False}
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        return {"ok": False}
    cmd.paiement_statut = statut_cp
    if statut_cp in ("ACCEPTED", "00"):
        cmd.statut = "paye"
    elif statut_cp in ("REFUSED", "CANCELLED"):
        cmd.statut = "paiement_refuse"
    db.commit()
    return {"ok": True}


@router.get("/retour")
async def retour_paiement(ref: str, db: Session = Depends(get_db)):
    from fastapi.responses import HTMLResponse
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        return HTMLResponse("<h2>Commande introuvable</h2>")
    ref_safe = html_module.escape(ref)
    if cmd.statut == "paye":
        content = f'<html><head><meta charset="UTF-8"/><meta http-equiv="refresh" content="3;url=/?paye={ref_safe}"/></head><body style="font-family:sans-serif;text-align:center;padding:40px"><div style="font-size:48px">✅</div><h2>Paiement confirmé !</h2><p>Référence : <b>{ref_safe}</b></p></body></html>'
    else:
        content = f'<html><head><meta charset="UTF-8"/><meta http-equiv="refresh" content="3;url=/?echec={ref_safe}"/></head><body style="font-family:sans-serif;text-align:center;padding:40px"><div style="font-size:48px">❌</div><h2>Paiement non abouti</h2><p>Référence : <b>{ref_safe}</b></p></body></html>'
    return HTMLResponse(content)


@router.post("/test-confirmer/{ref}")
async def test_confirmer(ref: str, db: Session = Depends(get_db)):
    if os.environ.get("RENDER") == "true":
        raise HTTPException(404, "Not found")
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    cmd.statut = "paye"
    db.commit()
    return {"ok": True, "ref": ref, "statut": "paye"}


@router.post("/confirmer-kkiapay")
async def confirmer_kkiapay(body: Dict[str, Any], db: Session = Depends(get_db)):
    ref = str(body.get("ref", "")).strip().upper()
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    if cmd.statut == "paye":
        return {"ok": True, "ref": cmd.ref, "statut": cmd.statut, "already_paid": True}
    cmd.statut = "paye"
    note = "[KKIAPAY] Paiement confirmé automatiquement"
    transaction_id = body.get("transaction_id")
    if transaction_id:
        note += f" — Transaction: {transaction_id}"
    cmd.note_admin = (cmd.note_admin or "") + " | " + note if cmd.note_admin else note
    db.commit()
    return {"ok": True, "ref": cmd.ref, "statut": "paye"}
