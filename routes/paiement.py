from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from typing import Dict, Any
import httpx, os
from database import get_db
from models import Commande
from routes.auth import require_patron

router = APIRouter(prefix="/api/paiement", tags=["paiement"])

# ── CinetPay ──────────────────────────────────────────────────────────────────
CINETPAY_SITE_ID = os.environ.get("CINETPAY_SITE_ID", "")
CINETPAY_API_KEY = os.environ.get("CINETPAY_API_KEY", "")
CINETPAY_URL     = "https://api-checkout.cinetpay.com/v2/payment"

# ── Genius Pay ────────────────────────────────────────────────────────────────
GENIUSPAY_API_KEY    = os.environ.get("GENIUSPAY_API_KEY", "pk_sandbox_qvTQ0QCkvVYhkUNq9yZvSZvNvJSuZoPl")
GENIUSPAY_API_SECRET = os.environ.get("GENIUSPAY_API_SECRET", "sk_sandbox_bda3d83954042d6af58f9b0bebceaac0dcd3ef0b00e3ea41e790255c9c789c95")  # ← coller votre clé secrète sandbox ici ou dans Render
GENIUSPAY_URL        = "https://pay.genius.ci/api/v1/merchant/payments"

# ── Config générale ───────────────────────────────────────────────────────────
APP_URL = os.environ.get("APP_URL", "http://localhost:8000")

# Pays routés vers Genius Pay (Guinée → GNF)
GENIUS_PAYS = {"Guinée"}

# Correspondance pays → code ISO (pour CinetPay)
PAYS_ISO = {
    "Guinée":        "GN",
    "Bénin":         "BJ",
    "Sénégal":       "SN",
    "Togo":          "TG",
    "Mali":          "ML",
    "Burkina Faso":  "BF",
    "Niger":         "NE",
    "Cameroun":      "CM",
    "Congo":         "CG",
    "Gabon":         "GA",
    "Côte d'Ivoire": "CI",
}


# ══════════════════════════════════════════════════════════════════════════════
# INIT PAIEMENT — route principale
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/init")
async def init_paiement(body: Dict[str, Any], db: Session = Depends(get_db)):
    ref = str(body.get("ref", ""))
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    if cmd.statut != "en_attente_paiement":
        raise HTTPException(400, "Commande déjà payée ou annulée")

    # Routage selon le pays du client
    if cmd.client_pays in GENIUS_PAYS:
        return await _init_geniuspay(cmd, db)
    else:
        return await _init_cinetpay(cmd, db)


# ══════════════════════════════════════════════════════════════════════════════
# GENIUS PAY — Guinée (GNF)
# ══════════════════════════════════════════════════════════════════════════════
async def _init_geniuspay(cmd: Commande, db: Session):
    headers = {
        "X-API-Key":    GENIUSPAY_API_KEY,
        "X-API-Secret": GENIUSPAY_API_SECRET,
        "Content-Type": "application/json",
    }
    payload = {
        "amount":      int(cmd.total_local),
        "description": f"FougahShop — {cmd.nb_articles} article(s) — {cmd.ref}",
        "customer": {
            "name":  cmd.client_nom or "Client",
            "phone": cmd.client_tel or "",
        },
        "metadata": {
            "order_id": cmd.ref,
            "pays":     cmd.client_pays,
        },
        # Pas de payment_method → Genius Pay affiche sa page de checkout
        # avec tous les opérateurs disponibles (Orange Money GN, MTN, etc.)
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(GENIUSPAY_URL, json=payload, headers=headers)
        data = r.json()

    if not data.get("success"):
        raise HTTPException(400, f"Erreur Genius Pay : {data.get('message', 'Inconnue')}")

    paiement_data = data["data"]

    # Stocker la référence Genius Pay dans la commande
    cmd.paiement_ref      = paiement_data.get("reference", "")
    cmd.paiement_provider = "geniuspay"
    db.commit()

    # Genius Pay retourne checkout_url quand pas de payment_method spécifié
    checkout_url = paiement_data.get("checkout_url") or paiement_data.get("payment_url", "")
    return {
        "payment_url": checkout_url,
        "ref":         cmd.ref,
        "provider":    "geniuspay",
    }


# ══════════════════════════════════════════════════════════════════════════════
# CINETPAY — reste de l'Afrique (XOF)
# ══════════════════════════════════════════════════════════════════════════════
async def _init_cinetpay(cmd: Commande, db: Session):
    if not CINETPAY_SITE_ID or not CINETPAY_API_KEY:
        return {
            "mode":        "test",
            "message":     "CinetPay non configuré",
            "payment_url": f"{APP_URL}/paiement-test?ref={cmd.ref}",
            "ref":         cmd.ref,
        }

    pays_iso = PAYS_ISO.get(cmd.client_pays or "", "CI")
    payload = {
        "apikey":                CINETPAY_API_KEY,
        "site_id":               CINETPAY_SITE_ID,
        "transaction_id":        cmd.ref,
        "amount":                int(cmd.total_local),
        "currency":              "XOF" if cmd.monnaie == "FCFA" else "GNF",
        "description":           f"FougahShop — {cmd.nb_articles} article(s) — {cmd.ref}",
        "return_url":            f"{APP_URL}/api/paiement/retour?ref={cmd.ref}",
        "notify_url":            f"{APP_URL}/api/paiement/webhook",
        "customer_name":         cmd.client_nom.split()[0] if cmd.client_nom else "Client",
        "customer_surname":      cmd.client_nom.split()[-1] if cmd.client_nom else "",
        "customer_phone_number": cmd.client_tel,
        "customer_address":      cmd.client_adresse or "",
        "customer_city":         cmd.client_pays,
        "customer_country":      pays_iso,
        "channels":              "ALL",
        "lang":                  "fr",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(CINETPAY_URL, json=payload)
        data = r.json()

    if data.get("code") != "201":
        raise HTTPException(400, f"Erreur CinetPay : {data.get('message', 'Inconnue')}")

    cmd.paiement_ref      = data["data"]["payment_token"]
    cmd.paiement_provider = "cinetpay"
    db.commit()

    return {
        "payment_url": data["data"]["payment_url"],
        "ref":         cmd.ref,
        "provider":    "cinetpay",
    }


# ══════════════════════════════════════════════════════════════════════════════
# WEBHOOK GENIUS PAY
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/geniuspay/webhook")
async def webhook_geniuspay(request: Request, db: Session = Depends(get_db)):
    body = await request.json()

    # Genius Pay envoie la référence dans metadata.order_id
    ref = None
    if isinstance(body.get("metadata"), dict):
        ref = body["metadata"].get("order_id")
    if not ref:
        ref = body.get("reference")  # fallback

    statut_gp = body.get("status", "")  # "paid", "failed", "cancelled"

    if not ref:
        return {"ok": False, "reason": "ref manquante"}

    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        return {"ok": False, "reason": "commande introuvable"}

    cmd.paiement_statut = statut_gp
    if statut_gp == "paid":
        cmd.statut = "paye"
    elif statut_gp in ("failed", "cancelled"):
        cmd.statut = "paiement_refuse"

    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# WEBHOOK CINETPAY
# ══════════════════════════════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════════════════════════════
# PAGE DE RETOUR (après paiement)
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/retour")
async def retour_paiement(ref: str, db: Session = Depends(get_db)):
    from fastapi.responses import HTMLResponse
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        return HTMLResponse("<h2>Commande introuvable</h2>")

    if cmd.statut == "paye":
        html = (
            f'<html><head><meta charset="UTF-8"/>'
            f'<meta http-equiv="refresh" content="3;url=/?paye={ref}"/></head>'
            f'<body style="font-family:sans-serif;text-align:center;padding:40px">'
            f'<div style="font-size:48px">✅</div>'
            f'<h2>Paiement confirmé !</h2>'
            f'<p>Référence : <b>{ref}</b></p></body></html>'
        )
    else:
        html = (
            f'<html><head><meta charset="UTF-8"/>'
            f'<meta http-equiv="refresh" content="3;url=/?echec={ref}"/></head>'
            f'<body style="font-family:sans-serif;text-align:center;padding:40px">'
            f'<div style="font-size:48px">❌</div>'
            f'<h2>Paiement non abouti</h2>'
            f'<p>Référence : <b>{ref}</b></p></body></html>'
        )
    return HTMLResponse(html)
