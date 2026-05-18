from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from database import get_db
import httpx
import re
import json
import os

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])

# ── Config Twilio ─────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WA_NUMBER   = "whatsapp:+14155238886"

FOUGAH_API = "https://fougahshop-backend.onrender.com"

# ── Paliers commission (source unique — import depuis commandes) ──
try:
    from routes.commandes import get_commission
except Exception:
    def get_commission(total_eu: float) -> int:
        if total_eu <= 50:   return 3500
        if total_eu <= 100:  return 5000
        if total_eu <= 200:  return 7000
        if total_eu <= 500:  return 12000
        return 20000

# ── Tous les pays supportés ───────────────────────────────────
PAYS_MAP = {
    # Guinée
    "1": "Guinée", "guinée": "Guinée", "guinee": "Guinée", "guinea": "Guinée",
    # Bénin
    "2": "Bénin", "bénin": "Bénin", "benin": "Bénin",
    # Sénégal
    "3": "Sénégal", "sénégal": "Sénégal", "senegal": "Sénégal",
    # Togo
    "4": "Togo", "togo": "Togo",
    # Mali
    "5": "Mali", "mali": "Mali",
    # Burkina Faso
    "6": "Burkina Faso", "burkina": "Burkina Faso", "burkina faso": "Burkina Faso",
    # Cameroun
    "7": "Cameroun", "cameroun": "Cameroun", "cameroon": "Cameroun",
    # Côte d'Ivoire
    "8": "Côte d'Ivoire", "côte d'ivoire": "Côte d'Ivoire", "cote d'ivoire": "Côte d'Ivoire", "ci": "Côte d'Ivoire",
    # Niger
    "9": "Niger", "niger": "Niger",
    # Congo
    "10": "Congo", "congo": "Congo",
    # Gabon
    "11": "Gabon", "gabon": "Gabon",
}

PAYS_MENU = (
    "1 - 🇬🇳 Guinée\n"
    "2 - 🇧🇯 Bénin\n"
    "3 - 🇸🇳 Sénégal\n"
    "4 - 🇹🇬 Togo\n"
    "5 - 🇲🇱 Mali\n"
    "6 - 🇧🇫 Burkina Faso\n"
    "7 - 🇨🇲 Cameroun\n"
    "8 - 🇨🇮 Côte d'Ivoire\n"
    "9 - 🇳🇪 Niger\n"
    "10 - 🇨🇬 Congo\n"
    "11 - 🇬🇦 Gabon"
)


# ══════════════════════════════════════════════════════════════
# SESSIONS EN BASE
# ══════════════════════════════════════════════════════════════

def get_session(tel: str, db: Session) -> dict:
    row = db.execute(
        text("SELECT data FROM whatsapp_sessions WHERE tel = :t"),
        {"t": tel}
    ).fetchone()
    if row:
        try:
            return json.loads(row[0])
        except Exception:
            pass
    return {"etape": "accueil", "panier": [], "pays": "", "nom": ""}


def save_session(tel: str, session: dict, db: Session):
    data = json.dumps(session, ensure_ascii=False)
    db.execute(text("""
        INSERT INTO whatsapp_sessions (tel, data, updated_at)
        VALUES (:t, :d, NOW())
        ON CONFLICT (tel) DO UPDATE
        SET data = EXCLUDED.data, updated_at = NOW()
    """), {"t": tel, "d": data})
    db.commit()


def reset_session(tel: str, db: Session):
    save_session(tel, {"etape": "accueil", "panier": [], "pays": "", "nom": ""}, db)


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def twiml_response(msg: str) -> PlainTextResponse:
    msg_escaped = (msg
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;'))
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{msg_escaped}</Message>
</Response>"""
    return PlainTextResponse(xml, media_type="application/xml")


def calculer_total(prix_eu: float, pays: str, cfg: dict) -> tuple[str, int]:
    """Retourne (total_affiche, total_local_int)"""
    commission = get_commission(prix_eu)
    if "Guinée" in pays:
        taux = cfg.get("taux_gnf", 9500)
        commission_locale = round(commission * taux / 656)
        total = round((prix_eu * taux) + commission_locale)
        return f"{total:,} GNF".replace(",", " "), total
    else:
        taux = cfg.get("taux_change", 660)
        total = round((prix_eu * taux) + commission)
        return f"{total:,} FCFA".replace(",", " "), total


async def obtenir_config_fougah() -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{FOUGAH_API}/api/config/public")
            return resp.json()
    except Exception:
        return {"taux_change": 660, "commission": 3500, "taux_gnf": 9500}


def get_operateurs_menu(pays: str, cfg: dict) -> tuple[str, dict]:
    """Retourne (menu texte, map choix→operateur)"""
    # Essayer depuis la config admin
    ops_cfg = cfg.get("operateurs_pays", {}) or {}
    ops_pays = ops_cfg.get(pays, [])

    if ops_pays:
        menu = "\n".join([f"{i+1} - {op}" for i, op in enumerate(ops_pays)])
        op_map = {str(i+1): op for i, op in enumerate(ops_pays)}
        for op in ops_pays:
            op_map[op.lower()] = op
        return menu, op_map

    # Fallback par défaut
    defaults = {
        "Guinée":        ["Orange Money"],
        "Bénin":         ["MTN MoMo", "Moov Money"],
        "Sénégal":       ["Orange Money", "Wave"],
        "Togo":          ["Flooz", "TMoney"],
        "Mali":          ["Orange Money", "Mobicash"],
        "Burkina Faso":  ["Orange Money", "Mobicash"],
        "Cameroun":      ["MTN MoMo", "Orange Money"],
        "Côte d'Ivoire": ["Orange Money", "MTN MoMo", "Wave"],
        "Niger":         ["Airtel Money"],
        "Congo":         ["Airtel Money", "MTN MoMo"],
        "Gabon":         ["Airtel Money"],
    }
    ops = defaults.get(pays, ["Mobile Money"])
    menu = "\n".join([f"{i+1} - {op}" for i, op in enumerate(ops)])
    op_map = {str(i+1): op for i, op in enumerate(ops)}
    for op in ops:
        op_map[op.lower()] = op
    return menu, op_map


def get_numero_paiement(operateur: str, cfg: dict) -> str:
    nums = cfg.get("numeros_paiement", {}) or {}
    return nums.get(operateur, "")


# ══════════════════════════════════════════════════════════════
# WEBHOOK PRINCIPAL
# ══════════════════════════════════════════════════════════════

@router.post("/webhook")
async def whatsapp_webhook(
    request: Request,
    Body: str = Form(""),
    From: str = Form(""),
    db: Session = Depends(get_db)
):
    tel     = From.replace("whatsapp:", "").strip()
    msg     = Body.strip()
    msg_low = msg.lower()

    session = get_session(tel, db)

    # ── Commandes globales ────────────────────────────────────
    if msg_low in ["annuler", "cancel", "stop", "menu", "recommencer"]:
        reset_session(tel, db)
        return twiml_response(
            "🔄 Conversation réinitialisée.\n\n"
            "Bonjour ! Je suis l'assistant FougahShop 🛍️\n\n"
            "Envoyez-moi le lien d'un article que vous voulez commander "
            "sur Nike, Zara, Amazon…\n\n"
            "Tapez *aide* pour plus d'infos."
        )

    if msg_low == "aide":
        return twiml_response(
            "📖 *Comment commander via FougahShop :*\n\n"
            "1️⃣ Trouvez l'article sur Nike, Zara, Amazon, Shein…\n"
            "2️⃣ Copiez le lien du produit\n"
            "3️⃣ Envoyez-le moi ici\n"
            "4️⃣ Indiquez le prix et votre pays\n"
            "5️⃣ Payez en Mobile Money — c'est tout !\n\n"
            "Tapez *annuler* à tout moment pour recommencer."
        )

    # ── ÉTAPE : accueil ───────────────────────────────────────
    if session["etape"] == "accueil":
        url_match = re.search(r'https?://[^\s]+', msg)
        if url_match:
            url = url_match.group(0)
            session["url"]   = url
            session["etape"] = "prix_manuel"
            site = next(
                (s for s in ["Zara","Nike","Amazon","H&M","ASOS","Zalando","Shein","Adidas"]
                 if s.lower() in url.lower()), ""
            )
            site_txt = f"*{site}*" if site else "le site"
            save_session(tel, session, db)
            return twiml_response(
                f"✅ Lien {site_txt} reçu !\n\n"
                f"Quel est le *prix de l'article* affiché sur {site_txt} ?\n\n"
                f"Donnez le prix en *euros (€)*.\n"
                f"Exemple : si le site affiche *89,99 €*, tapez *89.99*"
            )
        else:
            return twiml_response(
                "👋 Bonjour ! Je suis l'assistant FougahShop 🛍️\n\n"
                "Je commande vos articles en Europe et vous les livre en Afrique.\n\n"
                "📲 Envoyez-moi le *lien du produit* que vous voulez :\n"
                "Nike · Zara · Amazon · H&M · Shein · Adidas…\n\n"
                "Tapez *aide* pour plus d'infos."
            )

    # ── ÉTAPE : prix ──────────────────────────────────────────
    elif session["etape"] == "prix_manuel":
        prix_str   = msg.replace(',', '.').strip()
        prix_match = re.search(r'[\d]+\.?\d*', prix_str)
        if prix_match:
            try:
                prix = float(prix_match.group(0))
                if prix < 1:
                    return twiml_response(
                        "❌ Prix invalide. Tapez le montant en *euros (€)*.\n"
                        "Exemple : *89.99* ou *150*"
                    )
                session["total_eu"] = prix
                session["panier"]   = [{"nom": "Article", "prix": f"{prix} €", "qty": 1}]
                session["etape"]    = "choix_pays"
                save_session(tel, session, db)
                return twiml_response(
                    f"✅ *{prix:.2f} €* noté.\n\n"
                    f"🌍 *Dans quel pays êtes-vous ?*\n\n"
                    f"{PAYS_MENU}"
                )
            except Exception:
                pass
        return twiml_response(
            "❌ Je n'ai pas compris.\n\n"
            "Tapez le *prix en euros* affiché sur le site.\n"
            "Exemple : *89.99* ou *150*"
        )

    # ── ÉTAPE : choix pays ────────────────────────────────────
    elif session["etape"] == "choix_pays":
        pays = PAYS_MAP.get(msg_low) or PAYS_MAP.get(msg.strip().lower())
        if pays:
            session["pays"]  = pays
            session["etape"] = "confirmation"
            cfg              = await obtenir_config_fougah()
            total_eu         = session.get("total_eu", 0)
            total_aff, total_int = calculer_total(total_eu, pays, cfg)
            session["total_local"]     = total_aff
            session["total_local_int"] = total_int
            save_session(tel, session, db)
            return twiml_response(
                f"💰 *Récapitulatif :*\n\n"
                f"• Prix Europe : {total_eu:.2f} €\n"
                f"• *Total estimé : {total_aff}*\n"
                f"_(commission incluse · port calculé après pesée)_\n\n"
                f"📱 *Votre prénom et numéro Mobile Money ?*\n"
                f"Ex: Aminata · +224 620 000 000"
            )
        else:
            return twiml_response(
                f"❌ Répondez par un chiffre ou le nom du pays :\n\n{PAYS_MENU}"
            )

    # ── ÉTAPE : infos client ──────────────────────────────────
    elif session["etape"] == "confirmation":
        session["nom"]   = msg
        session["etape"] = "operateur"
        pays             = session.get("pays", "")
        cfg              = await obtenir_config_fougah()
        menu_ops, _      = get_operateurs_menu(pays, cfg)
        save_session(tel, session, db)
        return twiml_response(
            f"✅ Noté !\n\n"
            f"📱 *Quel opérateur Mobile Money ?*\n\n{menu_ops}"
        )

    # ── ÉTAPE : opérateur ─────────────────────────────────────
    elif session["etape"] == "operateur":
        pays          = session.get("pays", "")
        cfg           = await obtenir_config_fougah()
        _, op_map     = get_operateurs_menu(pays, cfg)
        operateur     = op_map.get(msg_low) or op_map.get(msg.strip().lower())

        if not operateur:
            # Prendre le premier opérateur par défaut
            operateur = list(op_map.values())[0] if op_map else "Mobile Money"

        session["operateur"] = operateur
        session["etape"]     = "valider"

        num   = get_numero_paiement(operateur, cfg)
        total = session.get("total_local", "—")
        save_session(tel, session, db)

        num_txt = f"\nAu numéro : *{num}*" if num else "\n_(Numéro communiqué par WhatsApp)_"
        return twiml_response(
            f"📲 *Instructions de paiement :*\n\n"
            f"Envoyez *{total}* via {operateur}"
            f"{num_txt}\n\n"
            f"Après paiement, tapez *PAYÉ* et envoyez la capture de votre transaction."
        )

    # ── ÉTAPE : confirmation paiement ─────────────────────────
    elif session["etape"] == "valider":
        if msg_low in ["payé", "paye", "paid", "oui", "yes", "confirmé", "confirme"]:
            try:
                payload = {
                    "client_nom":   session.get("nom", "Client WhatsApp"),
                    "client_tel":   tel,
                    "client_pays":  session.get("pays", ""),
                    "operateur":    session.get("operateur", "Mobile Money"),
                    "client_adresse": "",
                    "client_instructions": f"Commande via WhatsApp | URL: {session.get('url','')}",
                    # ✅ Fix — statut paye puisque le client confirme le paiement
                    "mode_paiement": "whatsapp_confirme",
                    "articles": [{
                        "lien":    session.get("url", ""),
                        "nom":     "Commande WhatsApp",
                        "prix_eu": session.get("total_eu", 0),
                        "poids":   0.5,
                        "qty":     1,
                        "img":     "",
                    }]
                }
                async with httpx.AsyncClient(timeout=30) as client:
                    resp   = await client.post(
                        f"{FOUGAH_API}/api/commandes/",
                        json=payload,
                        headers={"Content-Type": "application/json"}
                    )
                    result = resp.json()
                    ref    = result.get("ref", "—")

                # ✅ Fix — notifier l'admin via l'API statut
                if ref and ref != "—":
                    try:
                        async with httpx.AsyncClient(timeout=10) as client:
                            await client.patch(
                                f"{FOUGAH_API}/api/commandes/{ref}/statut-bot",
                                json={"statut": "en_attente_paiement",
                                      "note_admin": f"[BOT WA] Client a confirmé le paiement via {session.get('operateur','?')}"},
                                headers={"Content-Type": "application/json"}
                            )
                    except Exception:
                        pass

                reset_session(tel, db)
                return twiml_response(
                    f"🎉 *Commande enregistrée !*\n\n"
                    f"Référence : *{ref}*\n\n"
                    f"✅ Notre équipe vérifie votre paiement et traite votre commande.\n"
                    f"📦 Suivez-la sur fougahshop.com → Suivi → *{ref}*\n\n"
                    f"Merci de votre confiance 🙏"
                )
            except Exception:
                reset_session(tel, db)
                return twiml_response(
                    "⚠️ Commande reçue mais erreur système.\n"
                    "Contactez-nous directement — nous traitons votre demande."
                )
        else:
            return twiml_response(
                "En attente de votre confirmation.\n\n"
                "Tapez *PAYÉ* après avoir effectué le paiement.\n"
                "Ou tapez *annuler* pour recommencer."
            )

    # ── Fallback ──────────────────────────────────────────────
    else:
        reset_session(tel, db)
        return twiml_response(
            "👋 Envoyez-moi le lien de l'article que vous voulez commander.\n"
            "Tapez *aide* pour plus d'infos."
        )
