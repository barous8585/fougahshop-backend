"""
routes/bot.py — Router FastAPI pour le bot IA FougahShop (Fougah)
"""

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
import anthropic, httpx, os, json
from typing import Optional

router = APIRouter(prefix="/bot", tags=["bot"])

# ─── Config ──────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
FOUGAHSHOP_API_URL = os.getenv("FOUGAHSHOP_API_URL", "https://fougahshop-backend.onrender.com")
WA_TOKEN           = os.getenv("WA_TOKEN", "")
WA_PHONE_ID        = os.getenv("WA_PHONE_ID", "")
WA_VERIFY_TOKEN    = os.getenv("WA_VERIFY_TOKEN", "fougahshop_verify")

ALLOWED_ORIGINS = {"https://fougahshop.com", "https://www.fougahshop.com"}

MAX_MESSAGE_LENGTH = 1000
MAX_HISTORY_TURNS  = 10

PALIERS = [
    {"max": 50,    "comm": 3500},
    {"max": 100,   "comm": 5000},
    {"max": 200,   "comm": 7000},
    {"max": 500,   "comm": 12000},
    {"max": 99999, "comm": 20000},
]

STATUTS_FR = {
    "en_attente_paiement": "⏳ En attente de paiement",
    "paye":      "💛 Paiement reçu — on passe commande",
    "achete":    "🛒 Commande achetée en Europe",
    "expedie":   "✈️ En route vers l'Afrique",
    "arrive":    "📦 Arrivé — en attente de récupération",
    "recupere":  "✅ Récupéré par le client",
    "annulee":   "❌ Annulée",
    "paiement_refuse": "🚫 Paiement refusé",
}

SYSTEM_PROMPT = """Tu es Fougah, l'assistant IA de FougahShop — un service proxy shopping qui permet aux clients en Afrique de commander sur les boutiques européennes, américaines et asiatiques et de payer en Mobile Money.

=== QUI TU ES ===
Tu t'appelles Fougah. Tu es chaleureux, patient et très adaptable.
Tu parles à des clients africains qui écrivent souvent avec des fautes, des abréviations, du franglais, du wolof, du bambara, du soussou ou d'autres langues locales mélangées au français.
Tu dois TOUJOURS comprendre l'intention du client même si le message est mal écrit ou incomplet.

=== RÈGLES DE COMPRÉHENSION — TRÈS IMPORTANT ===
- Si le message est mal écrit, cherche l'intention et réponds comme si tu avais bien compris
- Si quelqu'un écrit "coman sa march" → il veut savoir comment ça marche
- Si quelqu'un écrit "moi vouloir chaussure nike" → il veut commander des Nike
- Si quelqu'un écrit "prix iphone" → il veut savoir combien coûte un iPhone
- Si quelqu'un écrit "mo commande" ou "ma commande" → il veut suivre sa commande
- Si quelqu'un écrit "ki lé" ou "c koi" ou "c quoi" → il pose une question sur FougahShop
- Si quelqu'un écrit juste "bonjour" ou "allô" → accueille-le chaleureusement et demande comment tu peux l'aider
- Si le message est en anglais → réponds en anglais
- Si le message mélange français et langue locale → réponds en français simple
- Ne dis JAMAIS "je ne comprends pas" — trouve toujours une interprétation et réponds
- Si tu n'es vraiment pas sûr, pose UNE SEULE question courte pour clarifier

=== COMMENT ÇA MARCHE ===
1. Le client va sur fougahshop.com onglet "Ajouter"
2. Il crée un ou plusieurs paniers — chaque panier correspond à un site (ex: un panier Nike, un panier Zara)
3. Dans chaque panier il met le lien ou la description de ce qu'il veut, le prix et la livraison boutique
4. Il remplit ses infos (nom, téléphone, pays, adresse)
5. Il paie UNE SEULE FOIS en Mobile Money le montant total affiché
6. FougahShop achète tout pour lui en Europe
7. Les articles arrivent en Afrique, le client récupère sa commande

=== PANIER — IMPORTANT ===
- Le client peut commander sur PLUSIEURS sites en même temps (plusieurs paniers)
- Chaque panier = un site différent (ex: Nike + Zara + Amazon dans la même commande)
- On paie UNE SEULE FOIS pour tous les paniers ensemble
- Le montant total inclut TOUT : prix paniers + commission FougahShop + frais de livraison
- Pas de frais cachés, pas de paiement supplémentaire après
- Modes de paiement : Orange Money, Wave, MTN MoMo, Moov Money, Free Money

=== COMMISSION FougahShop (valeurs exactes en FCFA) ===
La commission est calculée sur le total de tous les paniers en euros :
- Total ≤ 50€   → 3 500 FCFA
- Total ≤ 100€  → 5 000 FCFA
- Total ≤ 200€  → 7 000 FCFA
- Total ≤ 500€  → 12 000 FCFA
- Total > 500€  → 20 000 FCFA
Pour la Guinée, ces montants sont convertis en GNF selon le taux en vigueur.
Pour le montant exact converti, utilise TOUJOURS l'outil calculer_prix.

=== FRAIS DE PORT ET DÉLAIS ===
RÈGLE ABSOLUE : Ne JAMAIS inventer ou estimer les frais de port ni les délais de livraison.
TOUJOURS utiliser l'outil get_config pour obtenir les vraies valeurs en temps réel.
Ne pas dire "2-3 semaines" ou tout autre délai de ta propre initiative.

=== BOUTIQUES ===
65+ boutiques : Nike, Apple, Amazon, Adidas, Shein, Zara, H&M, ASOS, Zalando, Sephora, Decathlon, Fnac, La Redoute, AliExpress, Lululemon, New Balance, Foot Locker, JD Sports, IKEA, Mango, Ralph Lauren, Tommy Hilfiger, Lacoste, Calvin Klein, Puma, Supreme, Carhartt WIP, et bien d'autres.

=== SUIVI COMMANDE ===
Référence CMD-XXXX-XXXX + numéro de téléphone → fougahshop.com onglet Suivi
Notifications WhatsApp automatiques à chaque étape.

=== PARRAINAGE ===
Code FGxxxxxx obtenu après première commande récupérée. Donne une réduction à l'utilisateur du code et un gain au parrain.

=== GARANTIES ===
- Articles 100% authentiques achetés sur les vrais sites officiels
- Remboursement intégral si rupture de stock ou non livraison

=== EXEMPLES DE QUESTIONS FRÉQUENTES ET LEURS RÉPONSES ===
Q: "c possible de commander depuis guinée ?"
R: Oui bien sûr ! La Guinée est notre marché principal. Tu paies en Orange Money.

Q: "combien sa coute de vous envoyer un truc ?"
R: Le prix dépend du total de ton panier + une commission + les frais de port au kilo. Dis-moi ce que tu veux commander et je calcule le total exact.

Q: "j'ai payé mais toujours rien"
R: Je vais vérifier ta commande. Donne-moi ta référence CMD-XXXX-XXXX et ton numéro de téléphone.

Q: "vous livrez à la maison ?"
R: Oui, la livraison à domicile est disponible à Conakry. Utilise l'outil get_config pour les détails.

Q: "c authentique ou copie ?"
R: 100% authentique. On achète directement sur les sites officiels en Europe avec notre carte bancaire.

Q: "je peux mettre plusieurs boutiques dans ma commande ?"
R: Oui ! Tu peux créer plusieurs paniers dans la même commande — un panier par site. Tu paies tout en une seule fois.

Q: "c'est combien le kilo ?" ou "délai livraison ?" ou "frais de port ?"
R: [Appelle get_config et donne les vraies valeurs — ne jamais estimer]

=== STYLE DE RÉPONSE ===
- Phrases courtes et simples — tes clients lisent sur mobile
- Emojis avec modération (1-2 max par réponse)
- Toujours terminer en proposant d'aider davantage
- Ne pas être trop formel — parle comme un ami qui aide
- Si le client semble frustré, commence par le rassurer
"""

TOOLS = [
    {
        "name": "get_config",
        "description": "Récupère la configuration en temps réel depuis l'admin FougahShop : taux de change GNF, frais de port par pays, opérateurs de paiement, numéros de paiement, délais de livraison, etc.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "suivi_commande",
        "description": "Récupère le statut en temps réel d'une commande FougahShop. Nécessite la référence (ex: CMD-2026-0048) ET le numéro de téléphone du client.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Référence de la commande ex: CMD-2026-0048"},
                "tel": {"type": "string", "description": "Numéro de téléphone du client"}
            },
            "required": ["ref", "tel"]
        }
    },
    {
        "name": "calculer_prix",
        "description": "Calcule le prix total d'un ou plusieurs paniers en monnaie locale (GNF ou FCFA) incluant la commission FougahShop. Utilise les taux en temps réel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prix_euros": {"type": "number", "description": "Prix total du ou des paniers en euros"},
                "pays": {"type": "string", "description": "Pays du client (Guinée, Sénégal, Mali...)"},
                "qty": {"type": "integer", "description": "Quantité", "default": 1}
            },
            "required": ["prix_euros", "pays"]
        }
    }
]

_wa_sessions: dict = {}


def _cors_headers(origin: str) -> dict:
    allowed = origin if origin in ALLOWED_ORIGINS else "https://fougahshop.com"
    return {
        "Access-Control-Allow-Origin":  allowed,
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
    }


# ─── Tools ───────────────────────────────────────────────────
async def exec_get_config() -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{FOUGAHSHOP_API_URL}/api/config/public")
        if resp.status_code != 200:
            return "⚠️ Impossible de récupérer la configuration."

        cfg       = resp.json()
        taux_gnf  = cfg.get("taux_gnf", 9500)
        taux_fcfa = cfg.get("taux_change", 660)
        port_kg   = cfg.get("port_kg", {})
        operateurs = cfg.get("operateurs_pays", {})
        numeros   = cfg.get("numeros_paiement", {})
        livdom    = cfg.get("livraison_domicile", {})

        result  = "📊 **Configuration FougahShop (en temps réel)**\n\n"
        result += f"💱 **Taux de change**\n• 1€ = {taux_gnf:,.0f} GNF\n• 1€ = {taux_fcfa:,.0f} FCFA\n\n".replace(",", " ")

        pays_actifs   = {k: v for k, v in port_kg.items() if v.get("actif")}
        pays_inactifs = {k: v for k, v in port_kg.items() if not v.get("actif")}

        if pays_actifs:
            result += "🚚 **Frais de livraison (pays actifs)**\n"
            for pays, info in pays_actifs.items():
                # FIX: Afficher le prix dans la bonne monnaie selon le pays
                is_gnf = pays == "Guinée"
                prix_brut = int(info['prix'])
                if is_gnf:
                    prix_aff = round(prix_brut * taux_gnf / 656)
                    sym = "GNF"
                else:
                    prix_aff = prix_brut
                    sym = "FCFA"
                result += f"• {pays} : {prix_aff:,} {sym}/kg — {info['delai']}\n".replace(",", " ")
            result += "\n"

        if pays_inactifs:
            result += "🔜 **Pays bientôt disponibles**\n"
            result += ", ".join(pays_inactifs.keys()) + "\n\n"

        if operateurs:
            result += "📱 **Opérateurs de paiement**\n"
            for pays, ops in operateurs.items():
                result += f"• {pays} : {', '.join(ops)}\n"
            result += "\n"

        if numeros:
            result += "📞 **Numéros de paiement**\n"
            for op, num in numeros.items():
                result += f"• {op} : {num}\n"
            result += "\n"

        if livdom and livdom.get("retrait"):
            result += "🏠 **Livraison à domicile**\n"
            result += f"• Zone : {livdom.get('zones', 'N/A')}\n"
            result += f"• Prix : {int(livdom.get('prix', 0)):,} GNF\n".replace(",", " ")
            result += f"• Délai : {livdom.get('delai', 'N/A')}\n"
            if livdom.get("adresse"):
                result += f"• Adresse : {livdom.get('adresse')}\n"

        return result

    except Exception:
        return "⚠️ Impossible de récupérer la configuration pour le moment."


async def exec_suivi_commande(ref: str, tel: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{FOUGAHSHOP_API_URL}/api/commandes/suivi/{ref.upper().strip()}",
                params={"tel": tel.strip()}
            )
        if resp.status_code == 404:
            return f"❌ Aucune commande trouvée avec la référence {ref}."
        if resp.status_code == 403:
            return "❌ Le numéro de téléphone ne correspond pas à cette commande."
        if resp.status_code != 200:
            return "⚠️ Je n'arrive pas à récupérer les informations. Réessaie dans quelques instants."

        data    = resp.json()
        statut  = STATUTS_FR.get(data.get("statut", ""), data.get("statut", "inconnu"))
        nom     = data.get("client_nom", "Client")
        total   = data.get("total_local", 0)
        monnaie = data.get("monnaie", "FCFA")
        result  = f"📦 **Commande {data['ref']}**\n👤 {nom}\n📊 Statut : {statut}\n"
        if total:
            result += f"💰 Total : {int(total):,} {monnaie}\n".replace(",", " ")
        suivi_num = data.get("suivi_num")
        if suivi_num:
            result += f"🔍 N° suivi transporteur : {suivi_num}\n"
        return result

    except Exception:
        return "⚠️ Erreur lors de la récupération de la commande."


async def exec_calculer_prix(prix_euros: float, pays: str, qty: int = 1) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{FOUGAHSHOP_API_URL}/api/config/public")
        cfg = resp.json() if resp.status_code == 200 else {}
    except Exception:
        cfg = {}

    taux_gnf  = cfg.get("taux_gnf", 9500)
    taux_fcfa = cfg.get("taux_change", 660)

    pays_lower = pays.lower().strip()
    monnaie = "FCFA"
    taux    = taux_fcfa

    if "guin" in pays_lower:
        monnaie = "GNF"; taux = taux_gnf; pays = "Guinée"
    elif "s" in pays_lower and "n" in pays_lower and "gal" in pays_lower:
        pays = "Sénégal"
    elif "cote" in pays_lower or "ivoire" in pays_lower:
        pays = "Côte d'Ivoire"
    elif "burkina" in pays_lower:
        pays = "Burkina Faso"

    total_eu      = prix_euros * qty
    comm_fcfa     = next((p["comm"] for p in PALIERS if total_eu <= p["max"]), 20000)
    comm_local    = round(comm_fcfa * (taux / 656)) if monnaie == "GNF" else comm_fcfa
    panier_local  = round(total_eu * taux)
    total_local   = panier_local + comm_local

    r  = f"💰 **Calcul pour {qty}× panier à {prix_euros}€** ({pays})\n\n"
    r += f"• Prix panier(s) : {panier_local:,} {monnaie}\n".replace(",", " ")
    r += f"• Commission FougahShop : {comm_local:,} {monnaie}\n".replace(",", " ")
    r += f"• **Total : {total_local:,} {monnaie}**\n\n".replace(",", " ")
    r += "_(+ frais de livraison selon le poids — visible à la commande)_"
    return r


# ─── Moteur Claude ────────────────────────────────────────────
async def run_bot(messages: list, pays_client: str = "") -> str:
    if not ANTHROPIC_API_KEY:
        return "⚠️ Le bot n'est pas encore configuré."

    if len(messages) > MAX_HISTORY_TURNS * 2 + 1:
        messages = messages[-(MAX_HISTORY_TURNS * 2):]

    system = SYSTEM_PROMPT
    if pays_client:
        monnaie = "GNF" if "guin" in pays_client.lower() else "FCFA"
        system += f"\n\n=== CONTEXTE CLIENT ===\nLe client est en {pays_client}. Affiche toujours les montants en {monnaie} en priorité sans lui redemander son pays sauf si nécessaire."

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        system=system,
        tools=TOOLS,
        messages=messages
    )

    while resp.stop_reason == "tool_use":
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                inp = block.input
                if block.name == "get_config":
                    res = await exec_get_config()
                elif block.name == "suivi_commande":
                    res = await exec_suivi_commande(inp["ref"], inp["tel"])
                elif block.name == "calculer_prix":
                    res = await exec_calculer_prix(
                        inp["prix_euros"], inp["pays"], inp.get("qty", 1)
                    )
                else:
                    res = "Outil inconnu."
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": res})

        messages = messages + [
            {"role": "assistant", "content": resp.content},
            {"role": "user",      "content": results}
        ]
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            system=system,
            tools=TOOLS,
            messages=messages
        )

    for block in resp.content:
        if hasattr(block, "text"):
            return block.text
    return "Désolé, je n'ai pas pu générer une réponse."


# ─── Routes ───────────────────────────────────────────────────

@router.options("/chat")
async def chat_options(request: Request):
    origin = request.headers.get("origin", "")
    return JSONResponse({}, headers=_cors_headers(origin))


@router.post("/chat")
async def chat(request: Request):
    origin = request.headers.get("origin", "")
    headers = _cors_headers(origin)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Corps JSON invalide"}, status_code=400, headers=headers)

    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "Message vide"}, status_code=400, headers=headers)
    if len(message) > MAX_MESSAGE_LENGTH:
        return JSONResponse({"error": "Message trop long."}, status_code=400, headers=headers)

    pays_client = (body.get("pays") or "").strip()

    raw_history = body.get("history") or []
    if len(raw_history) > MAX_HISTORY_TURNS * 2:
        raw_history = raw_history[-(MAX_HISTORY_TURNS * 2):]
    messages = list(raw_history) + [{"role": "user", "content": message}]

    try:
        reply = await run_bot(messages, pays_client=pays_client)
    except Exception:
        return JSONResponse(
            {"error": "Une erreur est survenue. Réessayez dans quelques instants."},
            status_code=500,
            headers=headers
        )

    return JSONResponse({"reply": reply}, headers=headers)


@router.get("/whatsapp")
async def wa_verify(request: Request):
    params    = dict(request.query_params)
    mode      = params.get("hub.mode")
    challenge = params.get("hub.challenge")
    token     = params.get("hub.verify_token")
    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        return int(challenge)
    raise HTTPException(403, "Vérification échouée")


@router.post("/whatsapp")
async def wa_webhook(request: Request):
    try:
        body  = await request.json()
        entry = body["entry"][0]
        msg   = entry["changes"][0]["value"]["messages"][0]
    except (KeyError, IndexError):
        return JSONResponse({"status": "ok"})

    if msg.get("type") != "text":
        return JSONResponse({"status": "ok"})

    from_tel = msg["from"]
    text     = msg["text"]["body"].strip()[:MAX_MESSAGE_LENGTH]
    history  = _wa_sessions.get(from_tel, [])
    if len(history) > MAX_HISTORY_TURNS * 2:
        history = history[-(MAX_HISTORY_TURNS * 2):]

    messages = history + [{"role": "user", "content": text}]
    reply    = await run_bot(messages)
    _wa_sessions[from_tel] = messages + [{"role": "assistant", "content": reply}]

    if WA_TOKEN and WA_PHONE_ID:
        await _send_wa_message(from_tel, reply)
    return JSONResponse({"status": "ok"})


async def _send_wa_message(to: str, text: str):
    url     = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4096]}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, headers=headers, json=payload)
    except Exception as e:
        print(f"[WA] Erreur envoi: {e}")


@router.get("/health")
def health():
    return {"status": "ok"}
