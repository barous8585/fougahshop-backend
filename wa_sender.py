"""
wa_sender.py — envoi de messages WhatsApp via Twilio
Importé par admin.py et commandes.py
"""
import os
import httpx
from sqlalchemy.orm import Session

TWILIO_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN",  "")
# ✅ TWILIO_FROM depuis variable d'env — fallback sandbox pour les tests
TWILIO_FROM  = os.environ.get("TWILIO_FROM", "whatsapp:+14155238886")


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
    """
    Génère le message WhatsApp client selon le statut.
    ✅ CORRIGÉ — f-strings imbriquées remplacées par variables pré-calculées
    (compatibilité Python 3.10 et 3.11 sur Render)
    """

    if statut == "paye":
        return (
            f"✅ *Paiement confirmé !*\n\n"
            f"Bonjour, nous avons bien reçu votre paiement pour la commande *{ref}*.\n"
            f"Nous allons maintenant acheter votre article sur le site officiel.\n\n"
            f"Vous serez notifié dès l'expédition. Merci de votre confiance 🙏"
        )

    if statut == "achete":
        # ✅ Variables pré-calculées — pas de f-string imbriquée
        ligne_date = f"📅 Livraison estimée : *{date_estimee}*\n" if date_estimee else ""
        return (
            f"🛍️ *Article acheté !*\n\n"
            f"Votre commande *{ref}* a été passée sur le site officiel.\n"
            f"Votre colis sera bientôt expédié vers l'Afrique.\n\n"
            f"{ligne_date}"
            f"Nous vous notifierons dès l'expédition."
        )

    if statut == "expedie":
        ligne_suivi = f"📦 Numéro de suivi : *{suivi_num}*\n" if suivi_num else ""
        ligne_date  = f"📅 Arrivée estimée : *{date_estimee}*\n" if date_estimee else ""
        return (
            f"✈️ *Votre colis est en route !*\n\n"
            f"Commande *{ref}* expédiée depuis l'Europe.\n"
            f"{ligne_suivi}"
            f"{ligne_date}\n"
            f"Vous serez notifié à l'arrivée en Afrique."
        )

    if statut == "arrive":
        return (
            f"📦 *Votre colis est arrivé !*\n\n"
            f"Commande *{ref}* est arrivée en Afrique et prête pour récupération.\n\n"
            f"Contactez-nous sur WhatsApp pour organiser la livraison finale.\n"
            f"Merci de votre patience ! 🎉"
        )

    if statut == "paiement_refuse":
        ligne_motif = f"Motif : {motif}\n" if motif else ""
        return (
            f"❌ *Paiement non confirmé*\n\n"
            f"Nous n'avons pas pu valider le paiement de la commande *{ref}*.\n"
            f"{ligne_motif}\n"
            f"Merci de nous contacter sur WhatsApp pour régulariser."
        )

    if statut == "annulee":
        ligne_motif = f"Motif : {motif}\n" if motif else ""
        return (
            f"🔴 *Commande annulée*\n\n"
            f"Votre commande *{ref}* a été annulée.\n"
            f"{ligne_motif}\n"
            f"Contactez-nous si vous avez des questions."
        )

    return ""
