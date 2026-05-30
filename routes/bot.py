"""
routes/bot.py — Router FastAPI pour le bot IA FougahShop
À placer dans : routes/bot.py
"""

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
import anthropic, httpx, os
from typing import Optional

router = APIRouter(prefix="/bot", tags=["bot"])

# ─── Config ──────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
FOUGAHSHOP_API_URL = os.getenv("FOUGAHSHOP_API_URL", "https://fougahshop-backend.onrender.com")
WA_TOKEN           = os.getenv("WA_TOKEN", "")
WA_PHONE_ID        = os.getenv("WA_PHONE_ID", "")
WA_VERIFY_TOKEN    = os.getenv("WA_VERIFY_TOKEN", "fougahshop_verify")

# Paliers commission (miroir de routes/commandes.py)
PALIERS = [
    {"max": 50,    "comm": 3500},
    {"max": 100,   "comm": 5000},
    {"max": 200,   "comm": 7000},
    {"max": 500,   "comm": 12000},
    {"max": 99999, "comm": 20000},
]

MONNAIES = {
    "Guinée":        {"symbole": "GNF",  "taux": 9500},
    "Sénégal":       {"symbole": "FCFA", "taux": 660},
    "Mali":          {"symbole": "FCFA", "taux": 660},
    "Bénin":         {"symbole": "FCFA", "taux": 660},
    "Côte d'Ivoire": {"symbole": "FCFA", "taux": 660},
    "Burkina Faso":  {"symbole": "FCFA", "taux": 660},
    "Togo":          {"symbole": "FCFA", "taux": 660},
    "Niger":         {"symbole": "FCFA", "taux": 660},
    "Congo":         {"symbole": "FCFA", "taux": 660},
    "Gabon":         {"symbole": "FCFA", "taux": 660},
    "Cameroun":      {"symbole": "FCFA", "taux": 660},
}

STATUTS_FR = {
    "en_attente_paiement": "⏳ En attente de paiement",
    "paye":      "💛 Paiement reçu — on passe commande",
    "achete":    "🛒 Article acheté en Europe",
    "expedie":   "✈️ En route vers l'Afrique",
    "arrive":    "📦 Arrivé — en attente de récupération",
    "recupere":  "✅ Récupéré par le client",
    "annulee":   "❌ Annulée",
    "paiement_refuse": "🚫 Paiement refusé",
}

# ─── System prompt ───────────────────────────────────────────
SYSTEM_PROMPT = """Tu es l'assistant IA de FougahShop, un service de proxy shopping qui permet aux clients en Afrique de commander sur les boutiques européennes, américaines et asiatiques et de payer en Mobile Money (Orange Money, Wave, etc.).

=== QUI TU ES ===
Tu t'appelles Fouga. Tu es amical, direct et efficace. Tu réponds en français (ou dans la langue du client).
Tu dois répondre à N'IMPORTE QUELLE question sur FougahShop, quelle que soit la formulation.

=== COMMENT ÇA MARCHE ===
1. Le client choisit un article sur un site comme Nike, Apple, Shein, Amazon, Zara, etc.
2. Il nous envoie le lien ou une description de l'article
3. Il remplit le formulaire sur fougahshop.com (onglet "Ajouter")
4. Il paie en Mobile Money (Orange Money, Wave, MTN, etc.)
5. On achète l'article pour lui en Europe avec notre carte bancaire
6. L'article arrive en Afrique, le client récupère sa commande

=== PAYS DESSERVIS ===
Guinée (GNF), Sénégal, Mali, Bénin, Burkina Faso, Togo, Niger, Congo, Gabon, Cameroun (FCFA = 656 XOF/EUR)

=== TAUX DE CHANGE (approximatif) ===
1€ ≈ 9 500 GNF (Guinée)
1€ ≈ 660 FCFA (zone FCFA)

=== COMMISSION (frais de service) ===
- Article ≤ 50€ : +3 500 FCFA (~54 000 GNF)
- Article 51–100€ : +5 000 FCFA (~77 000 GNF)
- Article 101–200€ : +7 000 FCFA (~108 000 GNF)
- Article 201–500€ : +12 000 FCFA (~185 000 GNF)
- Article > 500€ : +20 000 FCFA (~308 000 GNF)
(+ frais de livraison locale selon le pays et le poids)

=== BOUTIQUES DISPONIBLES ===
Nike, Apple, Amazon, Adidas, Shein, Zara, H&M, ASOS, Zalando, Sephora, Decathlon, Fnac, La Redoute, Yves Rocher, PLT, Boohoo, AliExpress, Alibaba, Lululemon, New Balance, Foot Locker, JD Sports, IKEA, Mango, Ralph Lauren, Tommy Hilfiger, Lacoste, Calvin Klein, Puma, Supreme, Carhartt WIP, et bien d'autres (65+ boutiques).

=== MODES DE PAIEMENT ===
Orange Money, Wave, MTN Mobile Money, Moov Money, Free Money, et autres Mobile Money locaux.

=== DÉLAIS ===
- Après paiement : on achète l'article le jour même ou le lendemain
- Livraison Europe → Afrique : 15 à 30 jours selon le pays

=== SUIVI DE COMMANDE ===
Le client peut suivre sa commande sur fougahshop.com (onglet Suivi) avec sa référence CMD-XXXX-XXXX et son numéro de téléphone. Il reçoit aussi des notifications WhatsApp à chaque étape.

=== PARRAINAGE ===
Chaque client qui a récupéré une commande obtient un code de parrainage FGxxxxxx. Quand quelqu'un l'utilise, le parrain reçoit une réduction.

=== GARANTIES ===
- Articles 100% authentiques (achetés sur les vrais sites officiels)
- Remboursement intégral si article en rupture de stock ou non livré
- Suivi en temps réel

=== CE QUE TU PEUX FAIRE ===
- Répondre à toutes les questions sur FougahShop
- Calculer le prix total d'un article (utilise l'outil calculer_prix)
- Vérifier le statut d'une commande (utilise l'outil suivi_commande)
- Expliquer la procédure de commande étape par étape

=== STYLE ===
- Réponds de façon concise et claire
- Utilise des emojis avec modération
- Si le client parle en wolof, bambara ou autre langue locale, essaie de t'adapter
- Termine toujours par proposer une aide supplémentaire si besoin
"""

TOOLS = [
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
        "description": "Calcule le prix total d'un article en monnaie locale incluant la commission FougahShop.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prix_euros": {"type": "number", "description": "Prix de l'article en euros"},
                "pays": {"type": "string", "description": "Pays du client (Guinée, Sénégal, Mali...)"},
                "qty": {"type": "integer", "description": "Quantité", "default": 1}
            },
            "required": ["prix_euros", "pays"]
        }
    }
]

# Mémoire sessions WhatsApp (en mémoire — suffit pour commencer)
_wa_sessions: dict = {}


# ─── Tools implémentation ─────────────────────────────────────
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
            return "⚠️ Je n'arrive pas à récupérer les informations de ta commande. Réessaie dans quelques instants."

        data = resp.json()
        statut  = STATUTS_FR.get(data.get("statut", ""), data.get("statut", "inconnu"))
        nom     = data.get("client_nom", "Client")
        total   = data.get("total_local", 0)
        monnaie = data.get("monnaie", "FCFA")

        result = f"📦 **Commande {data['ref']}**\n👤 {nom}\n📊 Statut : {statut}\n"
        if total:
            result += f"💰 Total : {int(total):,} {monnaie}\n".replace(",", " ")
        suivi_num = data.get("suivi_num")
        if suivi_num:
            result += f"🔍 N° de suivi transporteur : {suivi_num}\n"
        return result
    except Exception as e:
        return f"⚠️ Erreur lors de la récupération : {str(e)}"


def exec_calculer_prix(prix_euros: float, pays: str, qty: int = 1) -> str:
    m = MONNAIES.get(pays)
    if not m:
        for k in MONNAIES:
            if k.lower() in pays.lower() or pays.lower() in k.lower():
                m = MONNAIES[k]; pays = k; break
    if not m:
        return f"Pays non reconnu. Pays disponibles : {', '.join(MONNAIES.keys())}."

    total_eu  = prix_euros * qty
    comm_fcfa = next((p["comm"] for p in PALIERS if total_eu <= p["max"]), 20000)
    symbole   = m["symbole"]
    taux      = m["taux"]

    article_local = round(total_eu * taux)
    comm_local    = round(comm_fcfa * (taux / 656)) if symbole == "GNF" else comm_fcfa
    total_local   = article_local + comm_local

    r  = f"💰 **Calcul pour {qty}× article à {prix_euros}€** ({pays})\n\n"
    r += f"• Prix article(s) : {article_local:,} {symbole}\n".replace(",", " ")
    r += f"• Commission FougahShop : {comm_local:,} {symbole}\n".replace(",", " ")
    r += f"• **Total à payer : {total_local:,} {symbole}**\n".replace(",", " ")
    r += "\n_(+ frais de livraison locale selon ton adresse)_"
    return r


# ─── Moteur Claude ────────────────────────────────────────────
async def run_bot(messages: list) -> str:
    if not ANTHROPIC_API_KEY:
        return "⚠️ Le bot n'est pas encore configuré (ANTHROPIC_API_KEY manquante)."

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=messages
    )

    while resp.stop_reason == "tool_use":
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                inp = block.input
                if block.name == "suivi_commande":
                    res = await exec_suivi_commande(inp["ref"], inp["tel"])
                elif block.name == "calculer_prix":
                    res = exec_calculer_prix(inp["prix_euros"], inp["pays"], inp.get("qty", 1))
                else:
                    res = "Outil inconnu."
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": res})

        messages = messages + [
            {"role": "assistant", "content": resp.content},
            {"role": "user",      "content": results}
        ]
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

    for block in resp.content:
        if hasattr(block, "text"):
            return block.text
    return "Désolé, je n'ai pas pu générer une réponse."


# ─── Routes ───────────────────────────────────────────────────
@router.post("/chat")
async def chat(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Corps JSON invalide")

    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "Message vide")

    history  = body.get("history") or []
    messages = list(history) + [{"role": "user", "content": message}]
    reply    = await run_bot(messages)
    return {"reply": reply}


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
        body = await request.json()
        entry = body["entry"][0]
        msg   = entry["changes"][0]["value"]["messages"][0]
    except (KeyError, IndexError):
        return JSONResponse({"status": "ok"})

    if msg.get("type") != "text":
        return JSONResponse({"status": "ok"})

    from_tel = msg["from"]
    text     = msg["text"]["body"].strip()
    history  = _wa_sessions.get(from_tel, [])
    if len(history) > 20:
        history = history[-20:]

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
    return {"status": "ok", "bot": "Fouga — FougahShop IA"}
