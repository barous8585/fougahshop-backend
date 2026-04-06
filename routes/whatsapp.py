from fastapi import APIRouter, Request, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from database import get_db
from fastapi import Depends
import httpx
import re
import json

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])

# ── Config Twilio ─────────────────────────────────────────────
TWILIO_ACCOUNT_SID = "AC6b3627d25e01c316ad79177595d847bf"
TWILIO_AUTH_TOKEN  = "bab9a0f2f82d9975d99afa112f6b0cd1"  # ← À remplir avec ton Auth Token
TWILIO_WA_NUMBER   = "whatsapp:+14155238886"

# ── Config ZenRows ────────────────────────────────────────────
ZENROWS_API_KEY = "7a92ab21726eae0cb290c90ec704b8a79ee6dad5"
ZENROWS_URL     = "https://api.zenrows.com/v1/"

# ── Config Backend FougahShop ─────────────────────────────────
FOUGAH_API      = "https://fougahshop-backend.onrender.com"

# Sessions en mémoire — garde l'état de la conversation
# { numero_tel: { etape, panier, pays, nom, ... } }
sessions = {}

def twiml_response(msg: str) -> PlainTextResponse:
    """Retourne une réponse TwiML pour Twilio"""
    msg_escaped = msg.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{msg_escaped}</Message>
</Response>"""
    return PlainTextResponse(xml, media_type="application/xml")

async def extraire_articles_zenrows(url: str) -> list:
    """Extrait les articles d'un lien de panier via ZenRows"""
    try:
        params = {
            "apikey":    ZENROWS_API_KEY,
            "url":       url,
            "js_render": "true",
            "wait":      "3000",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(ZENROWS_URL, params=params)

        if resp.status_code != 200:
            return []

        html = resp.text
        articles = []

        # Schema.org JSON-LD
        ld_matches = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.S | re.I
        )
        for ld in ld_matches:
            try:
                data = json.loads(ld)
                if isinstance(data, list): data = data[0]
                if data.get("@type") == "Product":
                    nom = data.get("name","")
                    prix = ""
                    offers = data.get("offers",{})
                    if isinstance(offers,list): offers=offers[0]
                    if offers.get("price"):
                        prix = f"{offers['price']} {offers.get('priceCurrency','€')}"
                    if nom:
                        articles.append({"nom":nom,"prix":prix,"qty":1})
            except Exception:
                pass

        # Open Graph fallback
        if not articles:
            og_title = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
            og_price = re.search(r'<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
            if og_title:
                articles.append({
                    "nom": og_title.group(1).strip()[:80],
                    "prix": og_price.group(1).strip()+" €" if og_price else "",
                    "qty": 1
                })

        return articles[:10]
    except Exception as e:
        print(f"ZenRows error: {e}")
        return []

async def obtenir_config_fougah() -> dict:
    """Récupère la config (taux, commission) depuis FougahShop"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{FOUGAH_API}/api/config/public")
            return resp.json()
    except Exception:
        return {"taux_change": 660, "commission": 3500}

def calculer_total(prix_eu: float, pays: str, cfg: dict) -> str:
    """Calcule le total en monnaie locale"""
    taux = cfg.get("taux_gnf", 9500) if "Guinée" in pays else cfg.get("taux_change", 660)
    commission = cfg.get("commission", 3500)
    if "Guinée" in pays:
        total = round((prix_eu * taux) + (commission * taux / 656))
        return f"{total:,} GNF".replace(",", " ")
    else:
        total = round((prix_eu * 660) + commission)
        return f"{total:,} FCFA".replace(",", " ")

def get_session(tel: str) -> dict:
    if tel not in sessions:
        sessions[tel] = {"etape": "accueil", "panier": [], "pays": "", "nom": ""}
    return sessions[tel]

def reset_session(tel: str):
    sessions[tel] = {"etape": "accueil", "panier": [], "pays": "", "nom": ""}

# ── Webhook principal ─────────────────────────────────────────
@router.post("/webhook")
async def whatsapp_webhook(
    request: Request,
    Body: str = Form(""),
    From: str = Form(""),
    db: Session = Depends(get_db)
):
    tel = From.replace("whatsapp:","").strip()
    msg = Body.strip()
    msg_lower = msg.lower()

    session = get_session(tel)

    # ── Commandes globales ────────────────────────────────────
    if msg_lower in ["annuler", "cancel", "stop", "menu", "recommencer"]:
        reset_session(tel)
        return twiml_response(
            "🔄 Conversation réinitialisée.\n\n"
            "Bonjour ! Je suis l'assistant FougahShop 🛍️\n\n"
            "Envoyez-moi :\n"
            "• Un *lien de panier* (Zara, Nike, Amazon…)\n"
            "• Ou tapez *aide* pour plus d'infos"
        )

    if msg_lower == "aide":
        return twiml_response(
            "📖 *Comment commander via FougahShop :*\n\n"
            "1️⃣ Remplissez votre panier sur Nike, Zara, Amazon…\n"
            "2️⃣ Appuyez sur *Partager le panier* sur le site\n"
            "3️⃣ Envoyez-moi le lien ici\n"
            "4️⃣ Je calcule le total en FCFA/GNF\n"
            "5️⃣ Confirmez et payez en Mobile Money\n\n"
            "Tapez *annuler* pour recommencer"
        )

    # ── Accueil ───────────────────────────────────────────────
    if session["etape"] == "accueil":

        # Détecter un lien dans le message
        url_match = re.search(r'https?://[^\s]+', msg)

        if url_match:
            url = url_match.group(0)
            session["url"] = url
            session["etape"] = "prix_manuel"

            # Détecter le site pour personnaliser le message
            site = ""
            if "zara" in url: site = "Zara"
            elif "nike" in url: site = "Nike"
            elif "amazon" in url: site = "Amazon"
            elif "hm.com" in url: site = "H&M"
            elif "asos" in url: site = "ASOS"
            elif "zalando" in url: site = "Zalando"
            elif "shein" in url: site = "Shein"

            site_txt = f"*{site}*" if site else "le site"

            return twiml_response(
                f"✅ Lien {site_txt} reçu !\n\n"
                f"Quel est le *prix total* de votre panier affiché sur {site_txt} ? _(en €)_\n\n"
                f"Ex: *150* ou *89.99*"
            )
        else:
            # Pas de lien — message de bienvenue
            return twiml_response(
                f"👋 Bonjour ! Je suis l'assistant FougahShop 🛍️\n\n"
                f"J'achète vos articles en Europe et vous les livre en Afrique.\n\n"
                f"📲 *Envoyez-moi le lien de votre panier* depuis :\n"
                f"Nike · Zara · Amazon · H&M · ASOS · Zalando…\n\n"
                f"Tapez *aide* pour plus d'infos"
            )

    # ── Prix manuel ───────────────────────────────────────────
    elif session["etape"] == "prix_manuel":
        prix_match = re.search(r'[\d.,]+', msg.replace(',','.'))
        if prix_match:
            try:
                prix = float(prix_match.group(0))
                session["total_eu"] = prix
                session["panier"] = [{"nom": "Panier", "prix": f"{prix} €", "qty": 1}]
                session["etape"] = "choix_pays"

                return twiml_response(
                    f"💶 Prix reçu : *{prix:.2f} €*\n\n"
                    f"🌍 *Dans quel pays êtes-vous ?*\n"
                    f"1 - 🇬🇳 Guinée Conakry\n"
                    f"2 - 🇧🇯 Bénin\n"
                    f"3 - 🇸🇳 Sénégal"
                )
            except Exception:
                pass
        return twiml_response("❌ Je n'ai pas compris. Envoyez juste le prix en chiffres, ex: *150*")

    # ── Choix pays ────────────────────────────────────────────
    elif session["etape"] == "choix_pays":
        pays_map = {"1": "Guinée", "2": "Bénin", "3": "Sénégal",
                    "guinée": "Guinée", "guinee": "Guinée", "guinea": "Guinée",
                    "bénin": "Bénin", "benin": "Bénin",
                    "sénégal": "Sénégal", "senegal": "Sénégal"}

        pays = pays_map.get(msg_lower) or pays_map.get(msg.strip())

        if pays:
            session["pays"] = pays
            session["etape"] = "confirmation"

            # Calculer le total
            cfg = await obtenir_config_fougah()
            total_eu = session.get("total_eu", 0)
            total_local = calculer_total(total_eu, pays, cfg)
            session["total_local"] = total_local
            session["cfg"] = cfg

            # Récap complet
            recap = f"💰 *Récapitulatif de votre commande :*\n\n"
            for a in session["panier"]:
                recap += f"• {a['nom']}"
                if a.get('prix'): recap += f" — {a['prix']}"
                recap += "\n"
            recap += f"\n💶 Total Europe : {total_eu:.2f} €\n"
            recap += f"💵 *Total à payer : {total_local}*\n"
            recap += f"_(Commission incluse · Port calculé après pesée)_\n\n"
            recap += f"📱 *Quel est votre prénom et numéro de téléphone Mobile Money ?*\n"
            recap += f"Ex: Aminata · +224 620 000 000"

            return twiml_response(recap)
        else:
            return twiml_response(
                "❌ Je n'ai pas compris. Répondez :\n"
                "1 - 🇬🇳 Guinée\n"
                "2 - 🇧🇯 Bénin\n"
                "3 - 🇸🇳 Sénégal"
            )

    # ── Infos client ──────────────────────────────────────────
    elif session["etape"] == "confirmation":
        session["nom"] = msg
        session["etape"] = "operateur"
        pays = session.get("pays","")

        # Opérateurs selon pays
        if "Guinée" in pays:
            ops = "1 - 🟠 Orange Money\n2 - 🟡 MTN MoMo"
        elif "Bénin" in pays:
            ops = "1 - 🟡 MTN MoMo\n2 - 🔵 Moov Money"
        else:
            ops = "1 - 🟠 Orange Money"

        return twiml_response(
            f"✅ Noté !\n\n"
            f"📱 *Quel opérateur Mobile Money utilisez-vous ?*\n\n"
            f"{ops}"
        )

    # ── Opérateur ─────────────────────────────────────────────
    elif session["etape"] == "operateur":
        pays = session.get("pays","")
        op_map = {}
        if "Guinée" in pays:
            op_map = {"1":"Orange Money","2":"MTN MoMo","orange":"Orange Money","mtn":"MTN MoMo"}
        elif "Bénin" in pays:
            op_map = {"1":"MTN MoMo","2":"Moov Money","mtn":"MTN MoMo","moov":"Moov Money"}
        else:
            op_map = {"1":"Orange Money","orange":"Orange Money"}

        operateur = op_map.get(msg_lower) or op_map.get(msg.strip().lower(),"Orange Money")
        session["operateur"] = operateur
        session["etape"] = "valider"

        # Numéros de paiement
        numeros = {
            "Orange Money": "+224 620 762 815",
            "MTN MoMo":     "+229 01 52 26 01 00",
            "Moov Money":   "+229 01 68 93 55 56",
        }
        num = numeros.get(operateur,"")
        total = session.get("total_local","—")

        return twiml_response(
            f"📲 *Instructions de paiement :*\n\n"
            f"Envoyez *{total}* via {operateur}\n"
            f"Au numéro : *{num}*\n\n"
            f"Après paiement, tapez *PAYÉ* et envoyez-moi la capture de votre transaction."
        )

    # ── Confirmation paiement ─────────────────────────────────
    elif session["etape"] == "valider":
        if msg_lower in ["payé","paye","paid","oui","yes","confirmé"]:
            session["etape"] = "termine"

            # Créer la commande dans FougahShop
            try:
                articles = session.get("panier",[])
                payload = {
                    "client_nom":  session.get("nom","Client WA"),
                    "client_tel":  tel,
                    "client_pays": session.get("pays",""),
                    "operateur":   session.get("operateur","Orange Money"),
                    "client_adresse": "",
                    "client_instructions": f"Commande via WhatsApp Bot | URL: {session.get('url','')}",
                    "articles": [{
                        "lien": session.get("url",""),
                        "nom":  a.get("nom","Article"),
                        "prix_eu": session.get("total_eu",0) / max(len(articles),1),
                        "poids": 0.5,
                        "qty":  a.get("qty",1),
                        "img":  "",
                    } for a in articles] or [{
                        "lien": session.get("url",""),
                        "nom":  "Commande WhatsApp",
                        "prix_eu": session.get("total_eu",0),
                        "poids": 0.5,
                        "qty":  1,
                        "img":  "",
                    }]
                }

                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        f"{FOUGAH_API}/api/commandes/",
                        json=payload,
                        headers={"Content-Type":"application/json"}
                    )
                    result = resp.json()
                    ref = result.get("ref","—")

                reset_session(tel)
                return twiml_response(
                    f"🎉 *Commande confirmée !*\n\n"
                    f"Votre référence : *{ref}*\n\n"
                    f"✅ Nous traitons votre commande dès vérification du paiement.\n"
                    f"📦 Vous recevrez des mises à jour ici sur WhatsApp.\n\n"
                    f"Suivez votre commande sur : fougahshop.com\n"
                    f"_(Section Suivi → entrez votre référence {ref})_"
                )
            except Exception as e:
                return twiml_response(
                    f"⚠️ Commande reçue mais erreur système.\n"
                    f"Contactez-nous directement — nous traitons votre demande."
                )
        else:
            return twiml_response(
                "En attente de votre confirmation de paiement.\n\n"
                "Tapez *PAYÉ* après avoir effectué le paiement.\n"
                "Ou tapez *annuler* pour recommencer."
            )

    # ── Par défaut ────────────────────────────────────────────
    else:
        reset_session(tel)
        return twiml_response(
            "👋 Bonjour ! Envoyez-moi le lien de votre panier pour commencer.\n"
            "Tapez *aide* pour plus d'infos."
        )
