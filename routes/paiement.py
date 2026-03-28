from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
import httpx, os, json
from database import get_db
from models import Commande

router = APIRouter(prefix="/api/paiement", tags=["paiement"])

# ── Clés CinetPay (à remplir quand tu as ton compte) ─────────
CINETPAY_SITE_ID = os.environ.get("CINETPAY_SITE_ID", "")
CINETPAY_API_KEY = os.environ.get("CINETPAY_API_KEY", "")
CINETPAY_URL     = "https://api-checkout.cinetpay.com/v2/payment"

# URL de ton app hébergée (à mettre à jour quand tu déploies)
APP_URL = os.environ.get("APP_URL", "http://localhost:8000")

class InitPaiementRequest(BaseModel):
    ref: str  # référence de la commande

@router.post("/init")
async def init_paiement(body: InitPaiementRequest, db: Session = Depends(get_db)):
    """
    Initialise un paiement CinetPay pour une commande.
    Retourne l'URL de paiement vers laquelle rediriger le client.
    """
    cmd = db.query(Commande).filter(Commande.ref == body.ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    if cmd.statut != "en_attente_paiement":
        raise HTTPException(400, "Cette commande a déjà été payée")

    if not CINETPAY_SITE_ID or not CINETPAY_API_KEY:
        # MODE TEST — pas encore de clés CinetPay
        return {
            "mode": "test",
            "message": "CinetPay non configuré — mode test actif",
            "payment_url": f"{APP_URL}/paiement-test?ref={cmd.ref}",
            "ref": cmd.ref,
        }

    payload = {
        "apikey":           CINETPAY_API_KEY,
        "site_id":          CINETPAY_SITE_ID,
        "transaction_id":   cmd.ref,
        "amount":           int(cmd.total_local),
        "currency":         "XOF" if cmd.monnaie == "FCFA" else "GNF",
        "description":      f"ProxyShop — {cmd.nb_articles} article(s) — {cmd.ref}",
        "return_url":       f"{APP_URL}/paiement/retour?ref={cmd.ref}",
        "notify_url":       f"{APP_URL}/api/paiement/webhook",
        "customer_name":    cmd.client_nom.split()[0] if cmd.client_nom else "Client",
        "customer_surname": cmd.client_nom.split()[-1] if cmd.client_nom else "",
        "customer_phone_number": cmd.client_tel,
        "customer_address": cmd.client_adresse or "",
        "customer_city":    cmd.client_pays,
        "customer_country": "CI",
        "channels":         "ALL",  # Orange, MTN, Moov, etc.
        "lang":             "fr",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(CINETPAY_URL, json=payload)
        data = r.json()

    if data.get("code") != "201":
        raise HTTPException(400, f"Erreur CinetPay: {data.get('message', 'Inconnue')}")

    # Sauvegarder la ref de paiement
    cmd.paiement_ref = data["data"]["payment_token"]
    db.commit()

    return {
        "payment_url": data["data"]["payment_url"],
        "token": data["data"]["payment_token"],
        "ref": cmd.ref,
    }


@router.post("/webhook")
async def webhook_cinetpay(request: Request, db: Session = Depends(get_db)):
    """
    CinetPay appelle cette URL automatiquement après chaque paiement.
    C'est ici que la commande passe au statut "paye" automatiquement.
    """
    body = await request.json()
    ref = body.get("cpm_trans_id") or body.get("transaction_id")
    statut_cp = body.get("cpm_result") or body.get("status")

    if not ref:
        return {"ok": False, "error": "ref manquante"}

    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        return {"ok": False, "error": "commande introuvable"}

    # Vérifier le paiement auprès de CinetPay (sécurité)
    if CINETPAY_SITE_ID and CINETPAY_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    "https://api-checkout.cinetpay.com/v2/payment/check",
                    json={
                        "apikey": CINETPAY_API_KEY,
                        "site_id": CINETPAY_SITE_ID,
                        "transaction_id": ref,
                    }
                )
                check = r.json()
                statut_cp = check.get("data", {}).get("status", statut_cp)
        except:
            pass  # En cas d'erreur réseau, on utilise le statut du webhook

    cmd.paiement_statut = statut_cp

    if statut_cp in ("ACCEPTED", "00"):
        cmd.statut = "paye"
        # Ici tu pourrais déclencher une notification WhatsApp
    elif statut_cp in ("REFUSED", "CANCELLED"):
        cmd.statut = "paiement_refuse"

    db.commit()
    return {"ok": True}


@router.get("/retour")
async def retour_paiement(ref: str, db: Session = Depends(get_db)):
    """Page de retour après paiement CinetPay (redirect navigateur)"""
    from fastapi.responses import HTMLResponse
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        return HTMLResponse("<h2>Commande introuvable</h2>")

    if cmd.statut == "paye":
        html = f"""
        <html><head><meta charset="UTF-8"/>
        <meta http-equiv="refresh" content="3;url=/?paye={ref}"/>
        </head><body style="font-family:sans-serif;text-align:center;padding:40px">
        <div style="font-size:48px">✅</div>
        <h2>Paiement confirmé !</h2>
        <p>Référence : <b>{ref}</b></p>
        <p>Redirection en cours…</p>
        </body></html>
        """
    else:
        html = f"""
        <html><head><meta charset="UTF-8"/>
        <meta http-equiv="refresh" content="3;url=/?echec={ref}"/>
        </head><body style="font-family:sans-serif;text-align:center;padding:40px">
        <div style="font-size:48px">❌</div>
        <h2>Paiement non abouti</h2>
        <p>Référence : <b>{ref}</b></p>
        <p>Vous pouvez réessayer depuis l'app.</p>
        </body></html>
        """
    return HTMLResponse(html)


@router.post("/test-confirmer/{ref}")
async def test_confirmer(ref: str, db: Session = Depends(get_db)):
    """
    ROUTE DE TEST UNIQUEMENT — simule un paiement accepté.
    À supprimer en production !
    """
    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    cmd.statut = "paye"
    cmd.paiement_statut = "TEST_ACCEPTED"
    db.commit()
    return {"ok": True, "ref": ref, "statut": "paye"}
