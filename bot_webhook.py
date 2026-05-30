"""
FougahShop — Bot IA
Webhook FastAPI qui alimente :
  - Le chat intégré sur fougahshop.com
  - WhatsApp Business API (quand tu auras l'accès Meta)

Dépendances :  pip install fastapi anthropic httpx
Lancer :       uvicorn bot_webhook:app --reload --port 8001
"""

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import anthropic, httpx, json, os, re
from typing import Optional

app = FastAPI(title="FougahShop Bot IA")

# ─── CORS (autorise fougahshop.com + dev local) ──────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # restreindre à ton domaine en prod
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Config ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
FOUGAHSHOP_API_URL  = os.getenv("FOUGAHSHOP_API_URL", "https://fougahshop.com")

# Taux de change par défaut (mis à jour depuis ta config)
TAUX_GNF_DEFAULT  = 9500
TAUX_FCFA_DEFAULT = 660

# Paliers commission (miroir de commandes.py)
PALIERS = [
    {"max": 50,    "comm": 3500},
    {"max": 100,   "comm": 5000},
    {"max": 200,   "comm": 7000},
    {"max": 500,   "comm": 12000},
    {"max": 99999, "comm": 20000},
]

MONNAIES = {
    "Guinée": {"symbole": "GNF",  "taux": TAUX_GNF_DEFAULT},
    "Sénégal": {"symbole": "FCFA", "taux": TAUX_FCFA_DEFAULT},
    "Mali":    {"symbole": "FCFA", "taux": TAUX_FCFA_DEFAULT},
    "Bénin":   {"symbole": "FCFA", "taux": TAUX_FCFA_DEFAULT},
    "Côte d'Ivoire": {"symbole": "FCFA", "taux": TAUX_FCFA_DEFAULT},
    "Burkina Faso":  {"symbole": "FCFA", "taux": TAUX_FCFA_DEFAULT},
    "Togo":    {"symbole": "FCFA", "taux": TAUX_FCFA_DEFAULT},
    "Niger":   {"symbole": "FCFA", "taux": TAUX_FCFA_DEFAULT},
    "Congo":   {"symbole": "FCFA", "taux": TAUX_FCFA_DEFAULT},
    "Gabon":   {"symbole": "FCFA", "taux": TAUX_FCFA_DEFAULT},
    "Cameroun":{"symbole": "FCFA", "taux": TAUX_FCFA_DEFAULT},
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

# ─── System prompt ───────────────────────────────────────────────────────────
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
- Livraison express disponible dans certains pays

=== SUIVI DE COMMANDE ===
Le client peut suivre sa commande sur fougahshop.com (onglet Suivi) avec sa référence CMD-XXXX-XXXX et son numéro de téléphone. Il reçoit aussi des notifications WhatsApp à chaque étape.

=== PARRAINAGE ===
Chaque client qui a récupéré une commande obtient un code de parrainage FGxxxxxx. Quand quelqu'un l'utilise, le parrain reçoit une réduction sur sa prochaine commande.

=== GARANTIES ===
- Articles 100% authentiques (achetés sur les vrais sites officiels)
- Remboursement intégral si article en rupture de stock ou non livré
- Suivi en temps réel

=== CE QUE TU PEUX FAIRE ===
- Répondre à toutes les questions sur FougahShop
- Calculer le prix total d'un article (utilise l'outil calculer_prix)
- Vérifier le statut d'une commande (utilise l'outil suivi_commande) — demande la ref ET le tel si tu ne les as pas
- Expliquer la procédure de commande étape par étape
- Aider avec les problèmes courants

=== CE QUE TU NE FAIS PAS ===
- Tu ne crées pas de commandes toi-même (le client doit remplir le formulaire)
- Tu ne traites pas les paiements
- Si une question dépasse tes capacités, dis au client de contacter le support sur WhatsApp

=== STYLE ===
- Réponds de façon concise et claire
- Utilise des emojis avec modération
- Si le client parle en wolof, bambara ou autre langue locale, essaie de t'adapter
- Termine toujours par proposer une aide supplémentaire si besoin
"""

# ─── Définition des tools ────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "suivi_commande",
        "description": "Récupère le statut en temps réel d'une commande FougahShop. Nécessite la référence (ex: CMD-2026-0048) ET le numéro de téléphone du client.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Référence de la commande (ex: CMD-2026-0048)"},
                "tel": {"type": "string", "description": "Numéro de téléphone du client"}
            },
            "required": ["ref", "tel"]
        }
    },
    {
        "name": "calculer_prix",
        "description": "Calcule le prix total d'un article en monnaie locale (GNF ou FCFA) incluant la commission FougahShop. Si le pays n'est pas précisé, demande au client.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prix_euros": {"type": "number", "description": "Prix de l'article en euros"},
                "pays": {"type": "string", "description": "Pays du client (Guinée, Sénégal, Mali, etc.)"},
                "qty": {"type": "integer", "description": "Quantité (défaut: 1)", "default": 1}
            },
            "required": ["prix_euros", "pays"]
        }
    }
]

# ─── Implémentation des tools ─────────────────────────────────────────────────
async def exec_suivi_commande(ref: str, tel: str) -> str:
    """Appelle l'API FougahShop pour récupérer le statut d'une commande."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{FOUGAHSHOP_API_URL}/api/commandes/suivi/{ref.upper().strip()}",
                params={"tel": tel.strip()}
            )
        if resp.status_code == 404:
            return f"❌ Aucune commande trouvée avec la référence {ref}. Vérifie bien la référence (format CMD-XXXX-XXXX)."
        if resp.status_code == 403:
            return "❌ Le numéro de téléphone ne correspond pas à cette commande. Vérifie ton numéro."
        if resp.status_code != 200:
            return "⚠️ Je n'arrive pas à récupérer les informations de ta commande pour le moment. Réessaie dans quelques instants."

        data = resp.json()
        statut = STATUTS_FR.get(data.get("statut", ""), data.get("statut", "inconnu"))
        nom    = data.get("client_nom", "Client")
        total  = data.get("total_local", 0)
        monnaie = data.get("monnaie", "FCFA")

        result = f"📦 **Commande {data['ref']}**\n"
        result += f"👤 {nom}\n"
        result += f"📊 Statut : {statut}\n"
        if total:
            result += f"💰 Total : {int(total):,} {monnaie}\n".replace(",", " ")

        suivi_num = data.get("suivi_num")
        if suivi_num:
            result += f"🔍 N° de suivi transporteur : {suivi_num}\n"

        return result

    except httpx.TimeoutException:
        return "⚠️ Le serveur met du temps à répondre. Réessaie dans quelques secondes."
    except Exception as e:
        return f"⚠️ Erreur lors de la récupération de la commande : {str(e)}"


def exec_calculer_prix(prix_euros: float, pays: str, qty: int = 1) -> str:
    """Calcule le prix total en monnaie locale avec commission."""
    m = MONNAIES.get(pays)
    if not m:
        # Cherche approximativement
        for k in MONNAIES:
            if k.lower() in pays.lower() or pays.lower() in k.lower():
                m = MONNAIES[k]
                pays = k
                break
    if not m:
        pays_list = ", ".join(MONNAIES.keys())
        return f"Je ne connais pas ce pays. Les pays disponibles sont : {pays_list}."

    total_eu = prix_euros * qty

    # Commission
    comm_fcfa = 20000
    for p in PALIERS:
        if total_eu <= p["max"]:
            comm_fcfa = p["comm"]
            break

    symbole = m["symbole"]
    taux    = m["taux"]

    if symbole == "GNF":
        taux_conv    = taux / 656
        article_local = round(total_eu * taux)
        comm_local    = round(comm_fcfa * taux_conv)
    else:
        taux_conv    = 1.0
        article_local = round(total_eu * taux)
        comm_local    = comm_fcfa

    total_local = article_local + comm_local

    result  = f"💰 **Calcul pour {qty}x article à {prix_euros}€** ({pays})\n\n"
    result += f"• Prix article(s) : {article_local:,} {symbole}\n".replace(",", " ")
    result += f"• Commission FougahShop : {comm_local:,} {symbole}\n".replace(",", " ")
    result += f"• **Total à payer : {total_local:,} {symbole}**\n".replace(",", " ")
    result += f"\n_(+ frais de livraison locale selon ton adresse)_"

    return result


# ─── Moteur de conversation ──────────────────────────────────────────────────
async def run_bot(messages: list) -> str:
    """Lance un tour de conversation avec Claude + gestion des tool_use."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Premier appel
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=messages
    )

    # Boucle tool_use → résultat → réponse finale
    while resp.stop_reason == "tool_use":
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                tid  = block.id
                name = block.name
                inp  = block.input

                if name == "suivi_commande":
                    result = await exec_suivi_commande(inp["ref"], inp["tel"])
                elif name == "calculer_prix":
                    result = exec_calculer_prix(
                        inp["prix_euros"], inp["pays"], inp.get("qty", 1)
                    )
                else:
                    result = "Outil inconnu."

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": result
                })

        # Ajouter la réponse assistant + les résultats dans l'historique
        messages = messages + [
            {"role": "assistant", "content": resp.content},
            {"role": "user",      "content": tool_results}
        ]

        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

    # Extraire le texte final
    for block in resp.content:
        if hasattr(block, "text"):
            return block.text

    return "Désolé, je n'ai pas pu générer une réponse."


# ─── Route chat (site web) ────────────────────────────────────────────────────
@app.post("/bot/chat")
async def chat(request: Request):
    """
    Corps attendu :
    {
      "message": "Quel est le prix d'un iPhone 15 à 899€ en Guinée ?",
      "history": [  // optionnel — liste de {role, content}
        {"role": "user",      "content": "Bonjour"},
        {"role": "assistant", "content": "Bonjour ! Comment puis-je t'aider ?"}
      ]
    }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Corps JSON invalide")

    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "Message vide")

    history = body.get("history") or []

    # Construire les messages pour Claude
    messages = list(history) + [{"role": "user", "content": message}]

    reply = await run_bot(messages)
    return {"reply": reply}


# ─── Route webhook WhatsApp (Meta) ───────────────────────────────────────────
WA_TOKEN      = os.getenv("WA_TOKEN", "")
WA_PHONE_ID   = os.getenv("WA_PHONE_ID", "")
WA_VERIFY_TOK = os.getenv("WA_VERIFY_TOKEN", "fougahshop_verify")

# Stockage en mémoire des conversations WA (remplacer par Redis en prod)
wa_sessions: dict[str, list] = {}

@app.get("/bot/whatsapp")
async def wa_verify(
    hub_mode: Optional[str]  = None,
    hub_challenge: Optional[str] = None,
    hub_verify_token: Optional[str] = None,
    request: Request = None
):
    """Vérification du webhook par Meta."""
    params = dict(request.query_params)
    mode      = params.get("hub.mode")
    challenge = params.get("hub.challenge")
    token     = params.get("hub.verify_token")

    if mode == "subscribe" and token == WA_VERIFY_TOK:
        return int(challenge)
    raise HTTPException(403, "Vérification échouée")


@app.post("/bot/whatsapp")
async def wa_webhook(request: Request):
    """Reçoit les messages WhatsApp et répond via l'API Meta."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "ok"})

    try:
        entry   = body["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]
        msg     = value["messages"][0]
    except (KeyError, IndexError):
        return JSONResponse({"status": "ok"})

    if msg.get("type") != "text":
        return JSONResponse({"status": "ok"})

    from_tel = msg["from"]
    text     = msg["text"]["body"].strip()

    # Récupérer / créer la session
    history = wa_sessions.get(from_tel, [])

    # Limiter l'historique à 10 tours (mémoire)
    if len(history) > 20:
        history = history[-20:]

    messages = history + [{"role": "user", "content": text}]
    reply    = await run_bot(messages)

    # Sauvegarder la session
    wa_sessions[from_tel] = messages + [{"role": "assistant", "content": reply}]

    # Envoyer la réponse via l'API Meta
    if WA_TOKEN and WA_PHONE_ID:
        await _send_wa_message(from_tel, reply)

    return JSONResponse({"status": "ok"})


async def _send_wa_message(to: str, text: str):
    """Envoie un message via l'API WhatsApp Cloud."""
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4096]}
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, headers=headers, json=payload)
    except Exception as e:
        print(f"[WA] Erreur envoi message: {e}")


# ─── Health check ─────────────────────────────────────────────────────────────
@app.get("/bot/health")
def health():
    return {"status": "ok", "service": "FougahShop Bot IA"}
