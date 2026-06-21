from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from typing import Dict, Any
import httpx, os, hmac, hashlib, json as _json, html as _html
from urllib.parse import quote as _urlquote
from database import get_db
from models import Commande
from routes.auth import require_patron

try:
    from routes.notifs import notifier_patron
except Exception:
    def notifier_patron(*a, **kw): pass

try:
    from routes.notifs import notifier_patron
except Exception:
    def notifier_patron(*a, **kw): pass

router = APIRouter(prefix="/api/paiement", tags=["paiement"])

# ── CinetPay ──────────────────────────────────────────────────────────────────
CINETPAY_SITE_ID = os.environ.get("CINETPAY_SITE_ID", "")
CINETPAY_API_KEY = os.environ.get("CINETPAY_API_KEY", "")
CINETPAY_URL     = "https://api-checkout.cinetpay.com/v2/payment"
CINETPAY_CHECK_URL = "https://api-checkout.cinetpay.com/v2/payment/check"

# ── Genius Pay ────────────────────────────────────────────────────────────────
# ✅ FIX SÉCURITÉ : plus de clé codée en dur — si la variable d'environnement
# n'est pas définie sur Render, on préfère un échec explicite (clé vide) plutôt
# qu'une fausse clé sandbox publique servant de filet silencieux.
GENIUSPAY_API_KEY    = os.environ.get("GENIUSPAY_API_KEY", "")
GENIUSPAY_API_SECRET = os.environ.get("GENIUSPAY_API_SECRET", "")
# ✅ Secret DÉDIÉ aux webhooks (format whsec_...), distinct de la clé API secrète.
# Genius Pay le génère séparément à la création du webhook (visible une seule fois
# dans le dashboard → Webhooks → création/édition). Si non configuré, on retombe
# sur GENIUSPAY_API_SECRET par sécurité, mais ça ne fonctionnera que si Genius Pay
# accepte effectivement la clé API secrète pour signer — à vérifier avec un vrai test.
GENIUSPAY_WEBHOOK_SECRET = os.environ.get("GENIUSPAY_WEBHOOK_SECRET", "") or GENIUSPAY_API_SECRET
GENIUSPAY_URL        = "https://geniuspay.ci/api/v1/merchant/payments"

if not GENIUSPAY_API_KEY or not GENIUSPAY_API_SECRET:
    print("⚠️  GENIUSPAY_API_KEY / GENIUSPAY_API_SECRET non définies dans l'environnement Render.")

# ── Config générale ───────────────────────────────────────────────────────────
# APP_URL = URL du BACKEND (sert à construire les routes /retour et /webhook
# appelées par les prestataires de paiement).
APP_URL = os.environ.get("APP_URL", "http://localhost:8000")
# FRONTEND_URL = URL du VRAI site (fougahshop.com) — c'est là, et uniquement là,
# que le client doit atterrir après paiement. Distinct de APP_URL car le backend
# (Render) et le frontend (Netlify) sont sur des domaines différents.
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://fougahshop.com")



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
        try:
            data = r.json()
        except Exception:
            print(f"[geniuspay] Réponse non-JSON — HTTP {r.status_code} : {r.text[:500]}")
            raise HTTPException(400, f"Erreur Genius Pay : réponse invalide (HTTP {r.status_code})")

    # ✅ FIX diagnostic : on logge TOUJOURS la réponse complète côté serveur
    # (consultable dans les logs Render) pour pouvoir identifier la vraie cause
    # même quand le message d'erreur affiché au client reste générique.
    print(f"[geniuspay] HTTP {r.status_code} — réponse : {data}")

    if not data.get("success"):
        # Genius Pay peut utiliser différents noms de champ selon le type d'erreur
        msg = (
            data.get("message")
            or data.get("error")
            or data.get("detail")
            or (data.get("errors")[0] if isinstance(data.get("errors"), list) and data.get("errors") else None)
            or f"HTTP {r.status_code} — voir logs serveur pour le détail"
        )
        raise HTTPException(400, f"Erreur Genius Pay : {msg}")

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
def _verify_geniuspay_signature(raw_body: bytes, timestamp: str, signature: str) -> bool:
    """
    Vérifie la signature HMAC-SHA256 d'un webhook Genius Pay.
    Format documenté : signature = HMAC-SHA256(timestamp + "." + json_payload, secret)
    """
    if not timestamp or not signature or not GENIUSPAY_WEBHOOK_SECRET:
        return False
    try:
        data_to_sign = f"{timestamp}.{raw_body.decode('utf-8')}"
        expected = hmac.new(
            GENIUSPAY_WEBHOOK_SECRET.encode(), data_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False


@router.post("/geniuspay/webhook")
async def webhook_geniuspay(request: Request, db: Session = Depends(get_db)):
    raw_body  = await request.body()
    signature = request.headers.get("X-Webhook-Signature", "")
    timestamp = request.headers.get("X-Webhook-Timestamp", "")

    # ✅ FIX SÉCURITÉ : on rejette tout webhook dont la signature ne correspond pas —
    # avant cela, n'importe qui pouvait POSTer un faux paiement "réussi" pour
    # n'importe quelle commande.
    if not _verify_geniuspay_signature(raw_body, timestamp, signature):
        print(f"🚨 Webhook Genius Pay rejeté — signature invalide ou absente")
        raise HTTPException(401, "Signature invalide")

    try:
        body = _json.loads(raw_body)
    except Exception:
        return {"ok": False, "reason": "json invalide"}

    # ✅ FIX : Genius Pay envoie le résultat dans un champ "event"
    # (payment.success / payment.failed / payment.cancelled / payment.expired /
    # payment.refunded) — confirmé par la doc officielle ET la liste d'événements
    # de ton dashboard webhooks. Le code précédent lisait un champ "status" qui
    # ne correspond pas à leur format réel.
    event = body.get("event", "")

    # Les données de la transaction peuvent être à la racine du payload ou
    # imbriquées sous "data" selon l'endpoint — on gère les deux cas pour être robuste.
    data = body.get("data") if isinstance(body.get("data"), dict) else body

    # Genius Pay renvoie la référence dans metadata.order_id
    ref = None
    if isinstance(data.get("metadata"), dict):
        ref = data["metadata"].get("order_id")
    if not ref and isinstance(body.get("metadata"), dict):
        ref = body["metadata"].get("order_id")
    if not ref:
        ref = data.get("reference") or body.get("reference")

    if not ref:
        return {"ok": False, "reason": "ref manquante"}

    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        return {"ok": False, "reason": "commande introuvable"}

    cmd.paiement_statut = event
    if event == "payment.success":
        cmd.statut = "paye"
    elif event in ("payment.failed", "payment.cancelled", "payment.expired"):
        cmd.statut = "paiement_refuse"
    # payment.initiated / payment.refunded : pas de changement automatique de statut,
    # juste tracé + notification ci-dessous (le remboursement nécessite ton intervention manuelle)

    db.commit()

    # ✅ FIX : l'admin était jamais notifié quand un paiement Genius Pay arrivait —
    # le webhook mettait bien à jour la base, mais en silence.
    try:
        if event == "payment.success":
            notifier_patron(
                db, "✅ Paiement Genius Pay confirmé",
                f"{cmd.ref} · {cmd.client_nom} · "
                f"{round(cmd.total_local or 0):,} {cmd.monnaie or 'FCFA'}",
                cmd.ref
            )
        elif event in ("payment.failed", "payment.cancelled", "payment.expired"):
            notifier_patron(
                db, "❌ Paiement Genius Pay échoué",
                f"{cmd.ref} · {cmd.client_nom}",
                cmd.ref
            )
        elif event == "payment.refunded":
            notifier_patron(
                db, "↩️ Paiement Genius Pay remboursé",
                f"{cmd.ref} · {cmd.client_nom} — à vérifier manuellement",
                cmd.ref
            )
    except Exception as e:
        print(f"[notif] Erreur (non bloquant): {e}")

    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# WEBHOOK CINETPAY
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/webhook")
async def webhook_cinetpay(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    ref = body.get("cpm_trans_id") or body.get("transaction_id")

    if not ref:
        return {"ok": False}

    cmd = db.query(Commande).filter(Commande.ref == ref).first()
    if not cmd:
        return {"ok": False}

    # ✅ FIX SÉCURITÉ : on ne fait plus jamais confiance au contenu du webhook —
    # CinetPay recommande explicitement de rappeler leur API de vérification,
    # car le webhook peut être forgé par n'importe qui (man-in-the-middle).
    if not CINETPAY_SITE_ID or not CINETPAY_API_KEY:
        print(f"🚨 Webhook CinetPay reçu mais CINETPAY_API_KEY/SITE_ID absents — impossible de vérifier {ref}")
        return {"ok": False, "reason": "config_manquante"}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                CINETPAY_CHECK_URL,
                json={
                    "transaction_id": ref,
                    "site_id":        CINETPAY_SITE_ID,
                    "apikey":         CINETPAY_API_KEY,
                },
            )
            verif = r.json()
    except Exception as e:
        print(f"🚨 Erreur appel vérification CinetPay pour {ref}: {e}")
        return {"ok": False, "reason": "verification_impossible"}

    if verif.get("code") != "00":
        print(f"⚠️  Vérification CinetPay {ref} non confirmée : {verif.get('message')}")
        return {"ok": False, "reason": "non_confirme"}

    data_verif   = verif.get("data", {}) or {}
    statut_reel  = data_verif.get("status", "")
    montant_reel = data_verif.get("amount")

    # ✅ Vérification supplémentaire du montant — le montant réellement payé
    # (confirmé par CinetPay) doit correspondre au montant attendu de la commande.
    if montant_reel is not None and cmd.total_local:
        try:
            if abs(float(montant_reel) - float(cmd.total_local)) > 1:
                print(f"🚨 Montant CinetPay incohérent pour {ref} : payé={montant_reel} / attendu={cmd.total_local}")
                return {"ok": False, "reason": "montant_incoherent"}
        except (TypeError, ValueError):
            pass

    cmd.paiement_statut = statut_reel
    if statut_reel == "ACCEPTED":
        cmd.statut = "paye"
    elif statut_reel in ("REFUSED", "CANCELLED"):
        cmd.statut = "paiement_refuse"

    db.commit()

    # ✅ FIX : même chose côté CinetPay — l'admin n'était jamais notifié.
    try:
        if statut_reel == "ACCEPTED":
            notifier_patron(
                db, "✅ Paiement CinetPay confirmé",
                f"{cmd.ref} · {cmd.client_nom} · "
                f"{round(cmd.total_local or 0):,} {cmd.monnaie or 'FCFA'}",
                cmd.ref
            )
        elif statut_reel in ("REFUSED", "CANCELLED"):
            notifier_patron(
                db, "❌ Paiement CinetPay échoué",
                f"{cmd.ref} · {cmd.client_nom}",
                cmd.ref
            )
    except Exception as e:
        print(f"[notif] Erreur (non bloquant): {e}")

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

    ref_safe = _html.escape(ref)

    if cmd.statut == "paye":
        html = (
            f'<html><head><meta charset="UTF-8"/>'
            f'<meta http-equiv="refresh" content="3;url=/?paye={ref_safe}"/></head>'
            f'<body style="font-family:sans-serif;text-align:center;padding:40px">'
            f'<div style="font-size:48px">✅</div>'
            f'<h2>Paiement confirmé !</h2>'
            f'<p>Référence : <b>{ref_safe}</b></p></body></html>'
        )
    else:
        html = (
            f'<html><head><meta charset="UTF-8"/>'
            f'<meta http-equiv="refresh" content="3;url=/?echec={ref_safe}"/></head>'
            f'<body style="font-family:sans-serif;text-align:center;padding:40px">'
            f'<div style="font-size:48px">❌</div>'
            f'<h2>Paiement non abouti</h2>'
            f'<p>Référence : <b>{ref_safe}</b></p></body></html>'
        )
    return HTMLResponse(html)
