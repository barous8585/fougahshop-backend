from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from typing import Dict, Any
import httpx, os, json, hmac, hashlib
from database import get_db
from models import Commande

router = APIRouter(prefix="/api/paiement", tags=["paiement"])

# ── Config CinetPay (existant) ────────────────────────────────
CINETPAY_SITE_ID = os.environ.get("CINETPAY_SITE_ID", "")
CINETPAY_API_KEY = os.environ.get("CINETPAY_API_KEY", "")
CINETPAY_URL     = "https://api-checkout.cinetpay.com/v2/payment"

# ── Config GeniusPay ──────────────────────────────────────────
GENIUSPAY_PUBLIC_KEY  = os.environ.get("GENIUSPAY_PUBLIC_KEY", "")   # pk_live_xxx
GENIUSPAY_PRIVATE_KEY = os.environ.get("GENIUSPAY_PRIVATE_KEY", "")  # sk_live_xxx
GENIUSPAY_API_URL     = "https://pay.genius.ci/api/v1/merchant"
GENIUSPAY_WEBHOOK_SECRET = os.environ.get("GENIUSPAY_WEBHOOK_SECRET", "")

APP_URL = os.environ.get("APP_URL", "https://fougahshop.com")


# ══════════════════════════════════════════════════════════════
# GENIUSPAY — Initialiser un paiement
# ══════════════════════════════════════════════════════════════

@router.post("/geniuspay/init")
async def init_geniuspay(body: Dict[str, Any], db: Session = Depends(get_db)):
    """
    Crée un paiement GeniusPay et retourne l'URL de checkout.
    Le client est redirigé vers la page GeniusPay pour payer
    en Orange Money, MTN MoMo, etc.
    """
    ref = str(body.get("ref", "")).strip().upper()
    if not ref:
        raise HTTPException(400, "Référence commande requise")

    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    if cmd.statut != "en_attente_paiement":
        raise HTTPException(400, "Commande déjà payée ou annulée")
    if not cmd.total_local or cmd.total_local <= 0:
        raise HTTPException(400, "Montant invalide")

    if not GENIUSPAY_PUBLIC_KEY or not GENIUSPAY_PRIVATE_KEY:
        raise HTTPException(503, "GeniusPay non configuré — contactez l'administrateur")

    # Déterminer la devise selon la monnaie de la commande
    currency = "GNF" if cmd.monnaie == "GNF" else "XOF"

    payload = {
        "amount":      int(cmd.total_local),
        "currency":    currency,
        "description": f"FougahShop — {cmd.nb_articles} article(s) — {cmd.ref}",
        "customer": {
            "name":  cmd.client_nom or "Client",
            "phone": cmd.client_tel or "",
        },
        "metadata": {
            "order_id":   cmd.ref,
            "client_tel": cmd.client_tel,
            "client_pays": cmd.client_pays,
        },
        "return_url": f"{APP_URL}/?paye={cmd.ref}",
        "cancel_url": f"{APP_URL}/?echec={cmd.ref}",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{GENIUSPAY_API_URL}/payments",
                json=payload,
                headers={
                    "X-API-Key":    GENIUSPAY_PUBLIC_KEY,
                    "X-API-Secret": GENIUSPAY_PRIVATE_KEY,
                    "Content-Type": "application/json",
                }
            )
        data = r.json()
    except Exception as e:
        raise HTTPException(503, f"Erreur connexion GeniusPay: {str(e)}")

    if not data.get("success"):
        raise HTTPException(400, f"Erreur GeniusPay: {data.get('message', 'Inconnue')}")

    payment_data = data.get("data", {})
    checkout_url = payment_data.get("checkout_url")
    reference    = payment_data.get("reference")

    if not checkout_url:
        raise HTTPException(400, "URL de paiement non reçue")

    # Sauvegarder la référence GeniusPay
    cmd.paiement_ref = reference
    db.commit()

    return {
        "checkout_url": checkout_url,
        "reference":    reference,
        "ref":          cmd.ref,
        "montant":      cmd.total_local,
        "monnaie":      cmd.monnaie,
    }


# ══════════════════════════════════════════════════════════════
# GENIUSPAY — Webhook (notification automatique)
# ══════════════════════════════════════════════════════════════

@router.post("/geniuspay/webhook")
async def webhook_geniuspay(request: Request, db: Session = Depends(get_db)):
    """
    Reçoit les notifications GeniusPay en temps réel.
    Valide la signature HMAC avant de traiter.
    """
    body_bytes = await request.body()

    # ✅ Vérification de signature HMAC
    if GENIUSPAY_WEBHOOK_SECRET:
        signature_reçue = request.headers.get("X-Webhook-Signature", "")
        signature_attendue = hmac.new(
            GENIUSPAY_WEBHOOK_SECRET.encode(),
            body_bytes,
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature_reçue, signature_attendue):
            print(f"⚠️ Webhook GeniusPay — signature invalide")
            return {"ok": False, "error": "signature invalide"}

    try:
        payload = json.loads(body_bytes)
    except Exception:
        return {"ok": False, "error": "payload invalide"}

    # Extraire les données
    event   = payload.get("event", "")
    data    = payload.get("data", {})
    statut  = data.get("status", "")
    meta    = data.get("metadata", {})
    ref     = meta.get("order_id", "")

    print(f"[GeniusPay Webhook] event={event} statut={statut} ref={ref}")

    if not ref:
        return {"ok": False, "error": "ref manquante"}

    cmd = db.query(Commande).filter(Commande.ref == ref.upper()).first()
    if not cmd:
        return {"ok": False, "error": "commande introuvable"}

    # ✅ Traiter selon le statut
    if statut == "COMPLETED" or event == "payment.completed":
        if cmd.statut == "en_attente_paiement":
            cmd.statut          = "paye"
            cmd.paiement_statut = "COMPLETED"
            note = f"[GENIUSPAY] Paiement confirmé automatiquement"
            gp_ref = data.get("reference", "")
            if gp_ref:
                note += f" — Ref: {gp_ref}"
            cmd.note_admin = (cmd.note_admin + " | " + note) if cmd.note_admin else note
            db.commit()
            print(f"✅ Commande {ref} → payée via GeniusPay")

            # Notifier le patron
            try:
                from routes.notifs import notifier_patron
                notifier_patron(
                    db, "✅ Paiement GeniusPay confirmé",
                    f"{cmd.ref} · {cmd.client_nom} · "
                    f"{round(cmd.total_local or 0):,} {cmd.monnaie or 'GNF'}",
                    cmd.ref
                )
            except Exception:
                pass

    elif statut in ("FAILED", "CANCELLED"):
        if cmd.statut == "en_attente_paiement":
            cmd.statut          = "paiement_refuse"
            cmd.paiement_statut = statut
            db.commit()
            print(f"❌ Commande {ref} → paiement {statut}")

    return {"ok": True}


# ══════════════════════════════════════════════════════════════
# GENIUSPAY — Vérifier le statut d'un paiement
# ══════════════════════════════════════════════════════════════

@router.get("/geniuspay/statut/{ref}")
async def statut_geniuspay(ref: str, db: Session = Depends(get_db)):
    """
    Vérifie le statut d'un paiement GeniusPay.
    Utilisé en polling depuis le frontend après redirection.
    """
    cmd = db.query(Commande).filter(Commande.ref == ref.upper()).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")

    # Si déjà payé en base → retourner directement
    if cmd.statut == "paye":
        return {"statut": "paye", "ref": cmd.ref}

    # Sinon vérifier via l'API GeniusPay
    if cmd.paiement_ref and GENIUSPAY_PUBLIC_KEY:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{GENIUSPAY_API_URL}/payments/{cmd.paiement_ref}",
                    headers={
                        "X-API-Key":    GENIUSPAY_PUBLIC_KEY,
                        "X-API-Secret": GENIUSPAY_PRIVATE_KEY,
                    }
                )
            data = r.json()
            if data.get("success"):
                gp_statut = data.get("data", {}).get("status", "")
                if gp_statut == "COMPLETED" and cmd.statut == "en_attente_paiement":
                    cmd.statut = "paye"
                    cmd.paiement_statut = "COMPLETED"
                    db.commit()
                    return {"statut": "paye", "ref": cmd.ref}
        except Exception:
            pass

    return {"statut": cmd.statut, "ref": cmd.ref}


# ══════════════════════════════════════════════════════════════
# CINETPAY (existant — inchangé)
# ══════════════════════════════════════════════════════════════

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
        "apikey": CINETPAY_API_KEY, "site_id": CINETPAY_SITE_ID,
        "transaction_id": cmd.ref, "amount": int(cmd.total_local),
        "currency": "XOF" if cmd.monnaie == "FCFA" else "GNF",
        "description": f"FougahShop — {cmd.nb_articles} article(s) — {cmd.ref}",
        "return_url": f"{APP_URL}/api/paiement/retour?ref={cmd.ref}",
        "notify_url": f"{APP_URL}/api/paiement/webhook",
        "customer_name": cmd.client_nom.split()[0] if cmd.client_nom else "Client",
        "customer_surname": cmd.client_nom.split()[-1] if cmd.client_nom else "",
        "customer_phone_number": cmd.client_tel,
        "customer_address": cmd.client_adresse or "",
        "customer_city": cmd.client_pays, "customer_country": "CI",
        "channels": "ALL", "lang": "fr",
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
    ref = body.get("cpm_trans_id") or body.get("transaction_id")
    statut_cp = body.get("cpm_result") or body.get("status")
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
    import html as html_module
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
    # ✅ Bloqué en production
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
