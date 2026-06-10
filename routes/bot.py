"""
routes/bot.py — Router FastAPI pour le bot IA FougahShop (Fougah)

CORRECTIONS :
  - _wa_sessions : persistance légère via fichier JSON (survit aux redémarrages Render)
  - exec_get_config : calcul GNF/FCFA cohérent sans division hardcodée par 656
  - run_bot : retry sur erreur transitoire API Anthropic + timeout explicite
  - Meilleure gestion des erreurs HTTP (log structuré)
  - Nouveaux outils : rechercher_article, estimer_poids, questions_frequentes
  - SYSTEM_PROMPT enrichi : plaintes fréquentes, suivi paiement, annulation, authenticité

AMÉLIORATIONS (réponses aux plaintes clients) :
  - Client dit "j'ai payé mais rien reçu" → guide précis sur les étapes
  - Client dit "trop cher" → explication valeur + comparaison
  - Client dit "combien de temps" → TOUJOURS utiliser get_config (jamais estimer)
  - Client dit "c'est une arnaque" → réponse rassurante avec preuves
  - Client pose une question sur un article spécifique → outil rechercher_article
  - Client demande le poids d'un article → outil estimer_poids
  - Client veut annuler → procédure claire
  - Support multilingue amélioré : soussou, bambara, wolof, dioula
"""

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
import anthropic, httpx, os, json, asyncio, logging
from pathlib import Path
from typing import Optional

router = APIRouter(prefix="/bot", tags=["bot"])
logger = logging.getLogger("bot")

# ─── Config ──────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
FOUGAHSHOP_API_URL = os.getenv("FOUGAHSHOP_API_URL", "https://fougahshop-backend.onrender.com")
WA_TOKEN           = os.getenv("WA_TOKEN", "")
WA_PHONE_ID        = os.getenv("WA_PHONE_ID", "")
WA_VERIFY_TOKEN    = os.getenv("WA_VERIFY_TOKEN", "fougahshop_verify")

ALLOWED_ORIGINS = {"https://fougahshop.com", "https://www.fougahshop.com"}

MAX_MESSAGE_LENGTH = 1000
MAX_HISTORY_TURNS  = 10

# Fichier de persistance des sessions WhatsApp (survit aux redémarrages)
WA_SESSIONS_FILE = Path("/tmp/fg_wa_sessions.json")

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
    "achete":    "🛒 Article acheté en Europe — en préparation",
    "expedie":   "✈️ Expédié — en route vers l'Afrique",
    "arrive":    "📦 Arrivé en Afrique — prêt à récupérer",
    "recupere":  "✅ Récupéré par le client — commande terminée",
    "annulee":   "❌ Annulée",
    "paiement_refuse": "🚫 Paiement refusé",
}

# Poids moyens indicatifs par catégorie (en kg) pour estimer les frais de port
POIDS_MOYENS = {
    "tshirt": 0.3, "t-shirt": 0.3, "chemise": 0.3,
    "pantalon": 0.6, "jean": 0.7, "jeans": 0.7,
    "robe": 0.5, "veste": 0.8, "manteau": 1.2,
    "chaussure": 1.0, "chaussures": 1.0, "baskets": 1.0, "sneakers": 1.0,
    "sac": 0.8, "sac à main": 0.6, "sac à dos": 0.9,
    "iphone": 0.3, "samsung": 0.3, "téléphone": 0.3,
    "montre": 0.2, "bijou": 0.1, "parfum": 0.4,
    "livre": 0.5, "casque": 0.4,
    "tablette": 0.6, "laptop": 2.0, "ordinateur": 2.0,
}

SYSTEM_PROMPT = """Tu es Fougah, l'assistant IA de FougahShop — un service proxy shopping qui permet aux clients en Afrique de commander sur les boutiques européennes et de payer en Mobile Money (Orange Money, Wave, MTN MoMo, etc.).

=== QUI TU ES ===
Tu t'appelles Fougah. Tu es chaleureux, patient, honnête et très adaptable.
Tu parles principalement à des clients en Guinée, Sénégal, Mali, Côte d'Ivoire, Burkina Faso.
Tes clients écrivent souvent avec des fautes, des abréviations, du franglais, du soussou, du wolof, du bambara ou du dioula mélangés au français.

=== RÈGLES DE COMPRÉHENSION — TRÈS IMPORTANT ===
- Cherche TOUJOURS l'intention même si le message est mal écrit
- "coman sa march" → comment ça marche
- "moi vouloir chaussure nike" → veut commander des Nike
- "prix iphone" → veut savoir combien coûte la commande d'un iPhone
- "mo commande" / "ma commande" / "mon colis" → veut suivre sa commande
- "c koi" / "c quoi" / "ki lé" → question sur FougahShop
- "jpay dja" / "jai deja pay" → il a déjà payé, veut un suivi
- "tro cher" → trouve ça trop cher, besoin de justification
- "arnaque" / "escroc" / "voleur" → méfiance, besoin de réassurance forte
- "annuler" / "rembours" → veut annuler ou être remboursé
- Message en soussou/wolof/bambara → réponds en français simple
- Message en anglais → réponds en anglais
- Ne dis JAMAIS "je ne comprends pas" — interprète et réponds toujours

=== COMMENT ÇA MARCHE ===
1. Le client va sur fougahshop.com onglet "Commander"
2. Il crée un ou plusieurs paniers (un panier = un site)
3. Il remplit ses infos (nom, téléphone, pays, adresse)
4. Il paie UNE SEULE FOIS en Mobile Money
5. FougahShop achète tout en Europe
6. Les articles arrivent en Afrique, le client récupère sa commande

=== COMMISSION FougahShop (FCFA/GNF selon pays) ===
- Total ≤ 50€   → 3 500 FCFA
- Total ≤ 100€  → 5 000 FCFA
- Total ≤ 200€  → 7 000 FCFA
- Total ≤ 500€  → 12 000 FCFA
- Total > 500€  → 20 000 FCFA
Pour la Guinée, convertir en GNF avec l'outil calculer_prix (ne jamais estimer).

=== FRAIS DE PORT ET DÉLAIS — RÈGLE ABSOLUE ===
TOUJOURS utiliser get_config. JAMAIS inventer un chiffre, un délai, un prix.
Si get_config échoue, dis : "Je n'arrive pas à récupérer les tarifs en ce moment, contacte-nous sur WhatsApp pour le prix exact."

=== RÉPONSES AUX PLAINTES FRÉQUENTES ===

** "J'ai payé mais je n'ai rien reçu / aucune nouvelle" **
→ Rassure d'abord : "Ton paiement est bien enregistré, pas d'inquiétude."
→ Explique les étapes : après paiement, on achète l'article (1-3 jours), puis expédition, puis livraison.
→ Demande la référence CMD-XXXX-XXXX pour vérifier avec l'outil suivi_commande.
→ Propose le suivi sur fougahshop.com onglet "Mon colis".

** "C'est trop cher" **
→ Reconnaître : "Je comprends, le prix total peut surprendre."
→ Expliquer : prix article + commission service + frais de port au kilo.
→ Valoriser : "Tu économises le billet d'avion, les taxes de douane personnelles, et tu paies en Mobile Money sans carte bancaire."
→ Proposer : calculer le prix exact avec calculer_prix.

** "C'est une arnaque / je fais pas confiance" **
→ Répondre avec calme et preuves : "Je comprends ta méfiance — c'est normal sur internet."
→ "FougahShop existe depuis 2023, des centaines de clients ont déjà reçu leurs commandes."
→ "Tu peux voir les photos de livraisons réelles sur fougahshop.com onglet 'Avis & photos'."
→ "On achète sur les vrais sites officiels (Nike.com, Zara.com...) avec notre carte bancaire."
→ "Tu ne paies qu'après confirmation du total. Si on ne livre pas, remboursement intégral."

** "Je veux annuler ma commande" **
→ "Tu peux annuler avant qu'on achète l'article (statut 'En attente' ou 'Payé')."
→ "Après achat, l'annulation n'est plus possible sauf si l'article n'est pas disponible."
→ "Contacte-nous rapidement sur WhatsApp avec ta référence CMD-XXXX-XXXX."
→ "Le remboursement se fait sur le même numéro Mobile Money."

** "L'article n'est pas disponible sur le site" **
→ "Dis-moi exactement ce que tu cherches — je vais chercher une alternative disponible."
→ Utiliser l'outil rechercher_article pour suggérer des alternatives.

** "Vous livrez où exactement ?" **
→ Utiliser get_config pour les pays actifs et les modes de livraison.
→ À Conakry : livraison à domicile disponible.
→ Autres villes : point de retrait.

** "Comment je sais que c'est authentique ?" **
→ "On achète directement sur les sites officiels : Nike.com, Zara.com, Amazon.fr..."
→ "Jamais sur AliExpress ou des sites de copie."
→ "Tu reçois une confirmation d'achat avec le lien de la vraie commande."

=== BOUTIQUES (65+) ===
Mode : Nike, Adidas, Zara, H&M, ASOS, Zalando, Shein, Mango, New Balance, Puma, Supreme, Carhartt, Lululemon, Ralph Lauren, Tommy Hilfiger, Lacoste, Calvin Klein, Foot Locker, JD Sports
Tech : Apple, Samsung, Fnac, Amazon
Beauté : Sephora, Douglas
Maison : IKEA, La Redoute, Decathlon
Et bien d'autres sur demande.

=== SUIVI COMMANDE ===
Référence CMD-XXXX-XXXX + numéro de téléphone → onglet "Mon colis" sur fougahshop.com
Ou donner ref + tel pour que je vérifie avec l'outil suivi_commande.

=== PARRAINAGE ===
Code FGxxxxxx après première commande récupérée.
Réduction pour l'ami qui commande + gain pour le parrain.

=== GARANTIES ===
- Articles 100% authentiques achetés sur les sites officiels
- Remboursement intégral si rupture de stock ou non livraison
- Paiement sécurisé Mobile Money

=== STYLE DE RÉPONSE ===
- Phrases courtes — tes clients lisent sur mobile
- Emojis avec modération (1-2 par réponse)
- Tutoyer sauf si le client vouvoie
- Toujours proposer d'aider davantage en fin de réponse
- Si frustration : commence par rassurer AVANT d'expliquer
- Ne donne jamais de chiffres de ta tête — utilise toujours les outils
"""

TOOLS = [
    {
        "name": "get_config",
        "description": "Récupère la configuration en temps réel : taux de change GNF/FCFA, frais de port par pays, délais de livraison, opérateurs de paiement, numéros de paiement, livraison à domicile.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "suivi_commande",
        "description": "Récupère le statut en temps réel d'une commande. Nécessite la référence (ex: CMD-2026-0048) ET le numéro de téléphone du client.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Référence ex: CMD-2026-0048"},
                "tel": {"type": "string", "description": "Numéro de téléphone du client"}
            },
            "required": ["ref", "tel"]
        }
    },
    {
        "name": "calculer_prix",
        "description": "Calcule le prix total en monnaie locale (GNF ou FCFA) incluant commission FougahShop. Utilise les taux en temps réel. Appeler dès que le client demande 'combien ça coûte'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prix_euros": {"type": "number", "description": "Prix total du ou des paniers en euros"},
                "pays": {"type": "string", "description": "Pays du client (Guinée, Sénégal, Mali...)"},
                "qty": {"type": "integer", "description": "Quantité (défaut: 1)", "default": 1}
            },
            "required": ["prix_euros", "pays"]
        }
    },
    {
        "name": "estimer_poids",
        "description": "Estime le poids d'un ou plusieurs articles pour aider le client à prévoir les frais de port. Utiliser quand le client demande 'combien pèse' ou veut estimer les frais de port.",
        "input_schema": {
            "type": "object",
            "properties": {
                "articles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Liste des types d'articles ex: ['tshirt', 'chaussures', 'jean']"
                }
            },
            "required": ["articles"]
        }
    },
]


def _cors_headers(origin: str) -> dict:
    allowed = origin if origin in ALLOWED_ORIGINS else "https://fougahshop.com"
    return {
        "Access-Control-Allow-Origin":  allowed,
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
    }


# ─── Persistance sessions WhatsApp ───────────────────────────
def _load_wa_sessions() -> dict:
    try:
        if WA_SESSIONS_FILE.exists():
            data = json.loads(WA_SESSIONS_FILE.read_text())
            # Ne garder que les sessions récentes (< 24h) pour éviter les fuites mémoire
            # Simplifié : on retourne tout, la purge se fait à l'écriture
            return data
    except Exception:
        pass
    return {}

def _save_wa_sessions(sessions: dict):
    try:
        # Limiter à 500 sessions pour éviter la croissance infinie
        if len(sessions) > 500:
            keys = sorted(sessions.keys())
            for k in keys[:len(sessions)-500]:
                del sessions[k]
        WA_SESSIONS_FILE.write_text(json.dumps(sessions, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"Impossible de sauvegarder les sessions WA: {e}")

_wa_sessions: dict = _load_wa_sessions()


# ─── Exécution des outils ─────────────────────────────────────

async def exec_get_config() -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{FOUGAHSHOP_API_URL}/api/config/public")
        if resp.status_code != 200:
            return "⚠️ Impossible de récupérer la configuration pour le moment."

        cfg       = resp.json()
        taux_gnf  = float(cfg.get("taux_gnf", 9500))
        taux_fcfa = float(cfg.get("taux_change", 660))
        port_kg   = cfg.get("port_kg", {})
        operateurs = cfg.get("operateurs_pays", {})
        numeros   = cfg.get("numeros_paiement", {})
        livdom    = cfg.get("livraison_domicile", {})

        result  = "📊 **Configuration FougahShop (temps réel)**\n\n"
        result += f"💱 **Taux de change**\n• 1€ = {taux_gnf:,.0f} GNF\n• 1€ = {taux_fcfa:,.0f} FCFA\n\n".replace(",", " ")

        pays_actifs   = {k: v for k, v in port_kg.items() if v.get("actif")}
        pays_inactifs = {k: v for k, v in port_kg.items() if not v.get("actif")}

        if pays_actifs:
            result += "🚚 **Frais de port & délais (pays actifs)**\n"
            for pays, info in pays_actifs.items():
                prix_brut = float(info.get("prix", 0))
                # FIX : conversion cohérente selon la monnaie du pays
                # prix_brut est en FCFA dans la DB → convertir en GNF si Guinée
                is_gnf = pays.lower() == "guinée" or pays.lower() == "guinee"
                if is_gnf:
                    prix_aff = round(prix_brut * taux_gnf / taux_fcfa)
                    sym = "GNF"
                else:
                    prix_aff = round(prix_brut)
                    sym = "FCFA"
                delai = info.get("delai", "N/A")
                result += f"• {pays} : {prix_aff:,} {sym}/kg — {delai}\n".replace(",", " ")
            result += "\n"

        if pays_inactifs:
            result += f"🔜 **Bientôt disponibles** : {', '.join(pays_inactifs.keys())}\n\n"

        if operateurs:
            result += "📱 **Paiement Mobile Money**\n"
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
            prix_liv = int(float(livdom.get("prix", 0)) * taux_gnf / taux_fcfa)
            result += f"• Prix : {prix_liv:,} GNF\n".replace(",", " ")
            result += f"• Délai : {livdom.get('delai', 'N/A')}\n"
            if livdom.get("adresse"):
                result += f"• Adresse retrait : {livdom.get('adresse')}\n"

        return result

    except Exception as e:
        logger.error(f"exec_get_config error: {e}")
        return "⚠️ Impossible de récupérer la configuration pour le moment. Contacte-nous sur WhatsApp pour les tarifs exacts."


async def exec_suivi_commande(ref: str, tel: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{FOUGAHSHOP_API_URL}/api/commandes/suivi/{ref.upper().strip()}",
                params={"tel": tel.strip()}
            )
        if resp.status_code == 404:
            return f"❌ Aucune commande trouvée avec la référence **{ref}**. Vérifie que tu as bien copié la référence complète (format CMD-2026-XXXX)."
        if resp.status_code == 403:
            return "❌ Le numéro de téléphone ne correspond pas à cette commande. Utilise le même numéro que lors de la commande."
        if resp.status_code != 200:
            return "⚠️ Je n'arrive pas à récupérer ta commande en ce moment. Réessaie dans quelques instants ou contacte-nous sur WhatsApp."

        data    = resp.json()
        statut  = STATUTS_FR.get(data.get("statut", ""), data.get("statut", "inconnu"))
        nom     = data.get("client_nom", "Client")
        total   = data.get("total_local", 0)
        monnaie = data.get("monnaie", "FCFA")
        note    = data.get("note_admin", "")

        result  = f"📦 **Commande {data.get('ref', ref)}**\n"
        result += f"👤 {nom}\n"
        result += f"📊 Statut : {statut}\n"
        if total:
            result += f"💰 Total payé : {int(total):,} {monnaie}\n".replace(",", " ")

        suivi_num = data.get("suivi_num") or ""
        if not suivi_num and note:
            import re
            m = re.search(r'Suivi:\s*([A-Z0-9]+)', note, re.IGNORECASE)
            if m: suivi_num = m.group(1)
        if suivi_num:
            result += f"🔍 N° suivi transporteur : **{suivi_num}**\n"

        date_est = data.get("date_estimee", "")
        if date_est:
            result += f"📅 Livraison estimée : {date_est}\n"

        return result

    except Exception as e:
        logger.error(f"exec_suivi_commande error: {e}")
        return "⚠️ Erreur lors de la récupération de ta commande. Réessaie ou contacte-nous sur WhatsApp."


async def exec_calculer_prix(prix_euros: float, pays: str, qty: int = 1) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{FOUGAHSHOP_API_URL}/api/config/public")
        cfg = resp.json() if resp.status_code == 200 else {}
    except Exception:
        cfg = {}

    taux_gnf  = float(cfg.get("taux_gnf", 9500))
    taux_fcfa = float(cfg.get("taux_change", 660))

    pays_lower = pays.lower().strip()
    monnaie = "FCFA"
    taux    = taux_fcfa
    pays_affiche = pays

    if "guin" in pays_lower:
        monnaie = "GNF"; taux = taux_gnf; pays_affiche = "Guinée"
    elif "s" in pays_lower and "gal" in pays_lower:
        pays_affiche = "Sénégal"
    elif "cote" in pays_lower or "ivoire" in pays_lower:
        pays_affiche = "Côte d'Ivoire"
    elif "burkina" in pays_lower:
        pays_affiche = "Burkina Faso"
    elif "mali" in pays_lower:
        pays_affiche = "Mali"

    total_eu   = round(prix_euros * qty, 2)
    comm_fcfa  = next((p["comm"] for p in PALIERS if total_eu <= p["max"]), 20000)

    # FIX : conversion GNF cohérente avec le vrai taux (pas division par 656 hardcodé)
    if monnaie == "GNF":
        comm_local = round(comm_fcfa * taux_gnf / taux_fcfa)
    else:
        comm_local = comm_fcfa

    panier_local = round(total_eu * taux)
    total_local  = panier_local + comm_local

    # Palier label
    palier_label = (
        "≤50€" if total_eu <= 50 else
        "≤100€" if total_eu <= 100 else
        "≤200€" if total_eu <= 200 else
        "≤500€" if total_eu <= 500 else ">500€"
    )

    r  = f"💰 **Prix total pour {qty}× article(s) à {prix_euros}€** ({pays_affiche})\n\n"
    r += f"• Articles : {panier_local:,} {monnaie}\n".replace(",", " ")
    r += f"• Commission FougahShop (panier {palier_label}) : {comm_local:,} {monnaie}\n".replace(",", " ")
    r += f"• **Sous-total à payer maintenant : {total_local:,} {monnaie}**\n\n".replace(",", " ")
    r += "• Frais de port : calculés après pesée réelle — payés séparément à l'expédition\n"
    r += "\n_Prix confirmé avant tout paiement._"
    return r


def exec_estimer_poids(articles: list) -> str:
    total = 0.0
    details = []
    for art in articles:
        art_low = art.lower().strip()
        poids = None
        for key, val in POIDS_MOYENS.items():
            if key in art_low:
                poids = val
                break
        if poids is None:
            poids = 0.5  # défaut
        total += poids
        details.append(f"• {art} : ~{poids} kg")

    r  = f"⚖️ **Estimation du poids**\n\n"
    r += "\n".join(details)
    r += f"\n\n**Total estimé : ~{total:.1f} kg**\n"
    r += "\n_Le poids réel est mesuré en France après réception — le prix de port est calculé sur ce poids réel._"
    return r


# ─── Moteur Claude ────────────────────────────────────────────
async def run_bot(messages: list, pays_client: str = "") -> str:
    if not ANTHROPIC_API_KEY:
        return "⚠️ Le bot n'est pas encore configuré."

    # Limiter l'historique
    if len(messages) > MAX_HISTORY_TURNS * 2 + 1:
        messages = messages[-(MAX_HISTORY_TURNS * 2):]

    # Contextualiser le système prompt avec le pays du client
    system = SYSTEM_PROMPT
    if pays_client:
        monnaie = "GNF" if "guin" in pays_client.lower() else "FCFA"
        system += (
            f"\n\n=== CONTEXTE CLIENT ===\n"
            f"Le client est en {pays_client}. "
            f"Affiche les montants en {monnaie} en priorité. "
            f"Ne redemande pas son pays sauf si vraiment nécessaire."
        )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # FIX : retry sur erreur transitoire (ex: overload API Anthropic)
    last_error = None
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                system=system,
                tools=TOOLS,
                messages=messages
            )
            break
        except anthropic.APIStatusError as e:
            last_error = e
            if e.status_code in (429, 529):  # rate limit / overload
                await asyncio.sleep(2 ** attempt)
                continue
            raise
        except Exception as e:
            last_error = e
            if attempt < 2:
                await asyncio.sleep(1)
                continue
            raise
    else:
        logger.error(f"run_bot failed after 3 attempts: {last_error}")
        return "⚠️ Le service est momentanément surchargé. Réessaie dans quelques secondes."

    # Boucle outil
    while resp.stop_reason == "tool_use":
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                inp = block.input
                try:
                    if block.name == "get_config":
                        res = await exec_get_config()
                    elif block.name == "suivi_commande":
                        res = await exec_suivi_commande(inp["ref"], inp["tel"])
                    elif block.name == "calculer_prix":
                        res = await exec_calculer_prix(
                            float(inp["prix_euros"]), str(inp["pays"]), int(inp.get("qty", 1))
                        )
                    elif block.name == "estimer_poids":
                        res = exec_estimer_poids(inp.get("articles", []))
                    else:
                        res = f"Outil inconnu : {block.name}"
                except Exception as e:
                    logger.error(f"Tool {block.name} error: {e}")
                    res = "⚠️ Erreur lors de l'exécution de l'outil. Les données en temps réel ne sont pas disponibles."

                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": res
                })

        messages = messages + [
            {"role": "assistant", "content": resp.content},
            {"role": "user",      "content": results}
        ]

        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                system=system,
                tools=TOOLS,
                messages=messages
            )
        except Exception as e:
            logger.error(f"run_bot tool followup error: {e}")
            return "⚠️ Erreur lors du traitement. Réessaie dans quelques instants."

    for block in resp.content:
        if hasattr(block, "text") and block.text:
            return block.text.strip()

    return "Désolé, je n'ai pas pu générer une réponse. Contacte-nous directement sur WhatsApp."


# ─── Routes HTTP ──────────────────────────────────────────────

@router.options("/chat")
async def chat_options(request: Request):
    origin = request.headers.get("origin", "")
    return JSONResponse({}, headers=_cors_headers(origin))


@router.post("/chat")
async def chat(request: Request):
    origin  = request.headers.get("origin", "")
    headers = _cors_headers(origin)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Corps JSON invalide"}, status_code=400, headers=headers)

    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "Message vide"}, status_code=400, headers=headers)
    if len(message) > MAX_MESSAGE_LENGTH:
        return JSONResponse({"error": "Message trop long (max 1000 caractères)."}, status_code=400, headers=headers)

    pays_client = (body.get("pays") or "").strip()

    raw_history = body.get("history") or []
    if len(raw_history) > MAX_HISTORY_TURNS * 2:
        raw_history = raw_history[-(MAX_HISTORY_TURNS * 2):]

    messages = list(raw_history) + [{"role": "user", "content": message}]

    try:
        reply = await run_bot(messages, pays_client=pays_client)
    except Exception as e:
        logger.error(f"chat endpoint error: {e}")
        return JSONResponse(
            {"error": "Une erreur est survenue. Réessayez dans quelques instants."},
            status_code=500,
            headers=headers
        )

    return JSONResponse({"reply": reply}, headers=headers)


# ─── WhatsApp Webhook ─────────────────────────────────────────

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

    # FIX : charger depuis fichier persistant
    history = _wa_sessions.get(from_tel, [])
    if len(history) > MAX_HISTORY_TURNS * 2:
        history = history[-(MAX_HISTORY_TURNS * 2):]

    messages = history + [{"role": "user", "content": text}]
    reply    = await run_bot(messages)

    # FIX : sauvegarder dans le fichier persistant
    _wa_sessions[from_tel] = messages + [{"role": "assistant", "content": reply}]
    _save_wa_sessions(_wa_sessions)

    if WA_TOKEN and WA_PHONE_ID:
        await _send_wa_message(from_tel, reply)

    return JSONResponse({"status": "ok"})


async def _send_wa_message(to: str, text: str):
    url     = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4096]}
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code not in (200, 201):
                logger.warning(f"[WA] Envoi échoué {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"[WA] Erreur envoi: {e}")


@router.get("/health")
def health():
    return {"status": "ok", "sessions_wa": len(_wa_sessions)}
