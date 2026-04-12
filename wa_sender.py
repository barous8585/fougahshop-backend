"""
wa_sender.py — envoi de messages WhatsApp via Twilio
Importé par admin.py et commandes.py
"""
import os, httpx
from sqlalchemy.orm import Session
from sqlalchemy import text

TWILIO_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN",  "")
TWILIO_FROM  = "whatsapp:+14155238886"

def _get_wa_number(db: Session) -> str:
    """Récupère le numéro WA admin depuis la base."""
    try:
        from models import Config
        cfg = db.query(Config).first()
        return cfg.wa_number or "" if cfg else ""
    except Exception:
        return ""

def envoyer_whatsapp(to_tel: str, message: str) -> bool:
    """
    Envoie un message WhatsApp via Twilio.
    to_tel : numéro du destinataire (ex: "224620762815" ou "+224 620 762 815")
    Retourne True si succès.
    """
    if not TWILIO_SID or not TWILIO_TOKEN:
        print(f"[WA] Twilio non configuré — message non envoyé à {to_tel}")
        return False

    # Normaliser le numéro
    tel = to_tel.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not tel.startswith("+"):
        tel = "+" + tel

    try:
        resp = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data={
                "From": TWILIO_FROM,
                "To":   f"whatsapp:{tel}",
                "Body": message,
            },
            timeout=10,
        )
        ok = resp.status_code in (200, 201)
        if not ok:
            print(f"[WA] Erreur Twilio {resp.status_code}: {resp.text[:200]}")
        return ok
    except Exception as e:
        print(f"[WA] Exception: {e}")
        return False


# ── Messages par statut ───────────────────────────────────────
def message_statut(ref: str, statut: str, date_estimee: str = "",
                   suivi_num: str = "", motif: str = "") -> str:
    """Génère le message WhatsApp client selon le statut."""
    msgs = {
        "paye": (
            f"✅ *Paiement confirmé !*\n\n"
            f"Bonjour, nous avons bien reçu votre paiement pour la commande *{ref}*.\n"
            f"Nous allons maintenant acheter votre article sur le site officiel.\n\n"
            f"Vous serez notifié dès l'expédition. Merci de votre confiance 🙏"
        ),
        "achete": (
            f"🛍️ *Article acheté !*\n\n"
            f"Votre commande *{ref}* a été passée sur le site officiel.\n"
            f"Votre colis sera bientôt expédié vers l'Afrique.\n\n"
            f"{"📅 Livraison estimée : *" + date_estimee + "*" + chr(10) if date_estimee else ""}"
            f"Nous vous notifierons dès l'expédition."
        ),
        "expedie": (
            f"✈️ *Votre colis est en route !*\n\n"
            f"Commande *{ref}* expédiée depuis l'Europe.\n"
            f"{"📦 Numéro de suivi : *" + suivi_num + "*" + chr(10) if suivi_num else ""}"
            f"{"📅 Arrivée estimée : *" + date_estimee + "*" + chr(10) if date_estimee else ""}\n"
            f"Vous serez notifié à l'arrivée en Afrique."
        ),
        "arrive": (
            f"📦 *Votre colis est arrivé !*\n\n"
            f"Commande *{ref}* est arrivée en Afrique et prête pour récupération.\n\n"
            f"Contactez-nous sur WhatsApp pour organiser la livraison finale.\n"
            f"Merci de votre patience ! 🎉"
        ),
        "paiement_refuse": (
            f"❌ *Paiement non confirmé*\n\n"
            f"Nous n'avons pas pu valider le paiement de la commande *{ref}*.\n"
            f"{"Motif : " + motif + chr(10) if motif else ""}\n"
            f"Merci de nous contacter sur WhatsApp pour régulariser."
        ),
        "annulee": (
            f"🔴 *Commande annulée*\n\n"
            f"Votre commande *{ref}* a été annulée.\n"
            f"{"Motif : " + motif + chr(10) if motif else ""}\n"
            f"Contactez-nous si vous avez des questions."
        ),
    }
    return msgs.get(statut, "")
