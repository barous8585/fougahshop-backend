"""
routes/bot.py — Router FastAPI pour le bot IA FougahShop (Fougah)

CORRECTIONS :
  - _wa_sessions : persistance légère via fichier JSON (survit aux redémarrages Render)
  - exec_get_config : calcul GNF/FCFA cohérent sans division hardcodée par 656
  - run_bot : retry sur erreur transitoire API Anthropic + timeout explicite
  - Meilleure gestion des erreurs HTTP (log structuré)
  - get_commission : formule progressive (5000 FCFA base + 3300/tranche de 50€), synchronisée
    avec routes/commandes.py et routes/admin.py — remplace l'ancienne grille à paliers fixes
  - Outils réels disponibles : get_config, suivi_commande, calculer_prix, estimer_poids, info_boutique

AMÉLIORATIONS :
  - Politique de remboursement précise et nuancée selon les vraies CGV (plus de "remboursement
    intégral" générique — chaque cas a sa propre règle : rupture stock, annulation avant/après
    achat, article non conforme, colis perdu, refus de payer le port, droits de douane)
  - Nouvel outil info_boutique : base de 30+ boutiques partenaires avec URL et conseils
    spécifiques pour les plus demandées (Nike, Apple, Sephora, IKEA...), recherche tolérante
    aux variantes d'écriture
  - Compréhension du langage élargie : davantage d'expressions courantes en français
    d'Afrique de l'Ouest, abréviations de villes, messages courts type "ok"/"merci"
  - Sections ajoutées : litiges et réclamations (délais de réponse), protection des données
  - Client dit "j'ai payé mais rien reçu" → guide précis sur les étapes
  - Client dit "trop cher" → explication valeur + comparaison
  - Client dit "combien de temps" → TOUJOURS utiliser get_config (jamais estimer)
  - Client dit "c'est une arnaque" → réponse rassurante avec preuves, sans survendre la garantie
  - Client veut annuler → vérifie d'abord le statut réel avant de promettre un remboursement
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

# ── Commission progressive : 5000 FCFA de base, +3300 FCFA par tranche de 50€ entamée ──
# Identique à routes/commandes.py et routes/admin.py — synchronisation obligatoire entre les trois.
# Règle de borne : un montant exactement égal à un multiple de 50€ reste dans la tranche inférieure.
COMMISSION_BASE          = 5000
COMMISSION_PALIER_EUROS  = 50
COMMISSION_PALIER_AJOUT  = 3300


def get_commission(total_euros: float) -> float:
    import math
    depasse     = max(0.0, total_euros - COMMISSION_PALIER_EUROS)
    nb_tranches = math.ceil(depasse / COMMISSION_PALIER_EUROS)
    return COMMISSION_BASE + nb_tranches * COMMISSION_PALIER_AJOUT

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

# ── Boutiques partenaires : URL + conseils pour les plus demandées ──
# Source : liste officielle FougahShop (CGV + page d'accueil), 37+ boutiques partenaires.
# Les boutiques avec "conseil" ont une fiche enrichie ; les autres ont juste leur URL.
BOUTIQUES = {
    "nike": {
        "url": "https://www.nike.com",
        "categorie": "Sport / Sneakers",
        "conseil": "Les tailles Nike sont souvent en US — vérifie le tableau de conversion EU sur la fiche produit avant de commander. Les Air Max et Air Force 1 sont les plus demandées."
    },
    "adidas": {
        "url": "https://www.adidas.fr",
        "categorie": "Sport / Sneakers",
        "conseil": "Les Yeezy et Samba sont en édition limitée — elles partent vite en rupture. Si tu vois le modèle disponible, commande rapidement."
    },
    "zara": {
        "url": "https://www.zara.com",
        "categorie": "Mode",
        "conseil": "Zara taille souvent petit comparé aux marques africaines courantes — prends une taille au-dessus si tu hésites."
    },
    "shein": {
        "url": "https://www.shein.com",
        "categorie": "Mode / Petit budget",
        "conseil": "Prix très bas mais qualité variable selon l'article — regarde bien les avis clients sur la fiche produit avant de valider."
    },
    "asos": {
        "url": "https://www.asos.com",
        "categorie": "Mode",
        "conseil": None
    },
    "zalando": {
        "url": "https://www.zalando.fr",
        "categorie": "Mode / Chaussures",
        "conseil": None
    },
    "h&m": {
        "url": "https://www2.hm.com",
        "categorie": "Mode",
        "conseil": None
    },
    "mango": {
        "url": "https://shop.mango.com",
        "categorie": "Mode",
        "conseil": None
    },
    "apple": {
        "url": "https://www.apple.com/fr",
        "categorie": "Tech",
        "conseil": "Pour un iPhone, précise toujours la capacité de stockage (128 Go, 256 Go...) et la couleur — le prix varie beaucoup selon ces deux critères."
    },
    "samsung": {
        "url": "https://www.samsung.com/fr",
        "categorie": "Tech",
        "conseil": None
    },
    "amazon": {
        "url": "https://www.amazon.fr",
        "categorie": "Généraliste",
        "conseil": "Vérifie bien que le vendeur est 'Amazon' ou 'Expédié par Amazon' pour garantir l'authenticité, certains vendeurs tiers sur Amazon ne sont pas fiables."
    },
    "sephora": {
        "url": "https://www.sephora.fr",
        "categorie": "Beauté / Parfum",
        "conseil": "Pour les parfums, vérifie bien le format (50ml, 100ml...) — c'est ce qui fait varier le prix le plus."
    },
    "nocibé": {
        "url": "https://www.nocibe.fr",
        "categorie": "Beauté / Parfum",
        "conseil": None
    },
    "yves rocher": {
        "url": "https://www.yves-rocher.fr",
        "categorie": "Beauté",
        "conseil": None
    },
    "decathlon": {
        "url": "https://www.decathlon.fr",
        "categorie": "Sport",
        "conseil": "Bon rapport qualité-prix pour l'équipement sportif — souvent moins cher que les grandes marques pour un usage similaire."
    },
    "puma": {
        "url": "https://fr.puma.com",
        "categorie": "Sport / Sneakers",
        "conseil": None
    },
    "new balance": {
        "url": "https://www.newbalance.fr",
        "categorie": "Sport / Sneakers",
        "conseil": None
    },
    "supreme": {
        "url": "https://www.supremenewyork.com",
        "categorie": "Streetwear",
        "conseil": "Les drops Supreme partent en quelques minutes — si l'article t'intéresse, envoie-nous le lien dès que possible."
    },
    "carhartt": {
        "url": "https://www.carhartt-wip.com",
        "categorie": "Streetwear",
        "conseil": None
    },
    "lululemon": {
        "url": "https://www.lululemon.fr",
        "categorie": "Sport",
        "conseil": None
    },
    "ralph lauren": {
        "url": "https://www.ralphlauren.fr",
        "categorie": "Mode",
        "conseil": None
    },
    "tommy hilfiger": {
        "url": "https://fr.tommy.com",
        "categorie": "Mode",
        "conseil": None
    },
    "lacoste": {
        "url": "https://www.lacoste.com",
        "categorie": "Mode",
        "conseil": None
    },
    "calvin klein": {
        "url": "https://www.calvinklein.fr",
        "categorie": "Mode",
        "conseil": None
    },
    "foot locker": {
        "url": "https://www.footlocker.fr",
        "categorie": "Sneakers",
        "conseil": None
    },
    "jd sports": {
        "url": "https://www.jdsports.fr",
        "categorie": "Sneakers",
        "conseil": None
    },
    "fnac": {
        "url": "https://www.fnac.com",
        "categorie": "Tech / Culture",
        "conseil": None
    },
    "ikea": {
        "url": "https://www.ikea.com/fr",
        "categorie": "Maison",
        "conseil": "Les meubles IKEA sont souvent lourds et volumineux — les frais de port peuvent être élevés, demande toujours une estimation avant de commander."
    },
    "la redoute": {
        "url": "https://www.laredoute.fr",
        "categorie": "Maison / Mode",
        "conseil": None
    },
    "modanisa": {
        "url": "https://www.modanisa.com",
        "categorie": "Mode modeste",
        "conseil": None
    },
    "fashion nova": {
        "url": "https://www.fashionnova.com",
        "categorie": "Mode",
        "conseil": None
    },
    "douglas": {
        "url": "https://www.douglas.fr",
        "categorie": "Beauté / Parfum",
        "conseil": None
    },
}

SYSTEM_PROMPT = """Tu es Fougah, l'assistant IA de FougahShop — un service proxy shopping qui permet aux clients en Afrique de commander sur les boutiques européennes et de payer en Mobile Money (Orange Money, Wave, MTN MoMo, etc.).

=== QUI TU ES ===
Tu t'appelles Fougah. Tu es chaleureux, patient, honnête et très adaptable.
Tu parles principalement à des clients en Guinée, Sénégal, Mali, Côte d'Ivoire, Burkina Faso.
Tes clients écrivent souvent avec des fautes, des abréviations, du franglais, du soussou, du wolof, du bambara ou du dioula mélangés au français.

=== RÈGLES DE COMPRÉHENSION — TRÈS IMPORTANT ===
- Cherche TOUJOURS l'intention même si le message est mal écrit
- "coman sa march" / "comen ça marche" → comment ça marche
- "moi vouloir chaussure nike" / "je veux des nike" → veut commander des Nike
- "prix iphone" / "combien sa coute iphone" → veut savoir combien coûte la commande d'un iPhone
- "mo commande" / "ma commande" / "mon colis" / "ou est mon colis" → veut suivre sa commande
- "c koi" / "c quoi" / "ki lé" / "kes ke c" → question sur FougahShop
- "jpay dja" / "jai deja pay" / "jai envoyé largent" → il a déjà payé, veut un suivi
- "tro cher" / "c chr sa" / "ya pa moins cher" → trouve ça trop cher, besoin de justification
- "arnaque" / "escroc" / "voleur" / "ont va me voler mon argent" → méfiance, besoin de réassurance forte
- "annuler" / "rembours" / "je veux plus" / "laisse tomber la commande" → veut annuler ou être remboursé
- "stp" / "svp" / "abrège" / "vite vite" → urgence ou politesse, adapte le ton sans ignorer la demande
- "dkr" / "abj" / "cky" → abréviations de villes (Dakar, Abidjan, Conakry) — utilise le contexte
- "1 momen" / "atend" / "wait" → demande de patienter, répondre brièvement puis attendre
- "g pa compri" / "javai pa compri" → n'a pas compris la réponse précédente, reformule plus simplement
- Message en soussou/wolof/bambara/dioula → réponds en français simple et clair, pas de jargon
- Message en anglais → réponds en anglais
- Messages très courts type "ok" / "merci" / "daccord" → réponse brève, ne pas relancer une explication complète
- Ne dis JAMAIS "je ne comprends pas" — interprète et réponds toujours, demande une précision si vraiment ambigu plutôt que de bloquer

=== COMMENT ÇA MARCHE ===
1. Le client va sur fougahshop.com onglet "Commander"
2. Il crée un ou plusieurs paniers (un panier = un site)
3. Il remplit ses infos (nom, téléphone, pays, adresse)
4. Il paie l'article + la commission en Mobile Money pour valider la commande
5. FougahShop achète tout en Europe
6. Une fois le colis pesé, le client paie les frais d'expédition séparément
7. Les articles arrivent en Afrique ; en Guinée, livraison à domicile optionnelle (paiement séparé) ou retrait gratuit en point relais
8. Le client récupère sa commande

=== STRUCTURE DE PAIEMENT — TRÈS IMPORTANT, À CLARIFIER PROACTIVEMENT ===
Le paiement n'est JAMAIS en une seule fois. C'est une source fréquente de confusion client —
explique-le clairement dès qu'un client demande "combien ça coûte", même sans qu'il se plaigne.
Il y a 2 à 3 paiements séparés dans le temps, jamais demandés en bloc :
1️⃣ À la commande : prix de l'article + commission FougahShop (calculés avec calculer_prix)
2️⃣ Après pesée réelle en Europe : frais d'expédition (montant exact donné par WhatsApp avant envoi)
3️⃣ Optionnel, Guinée uniquement : livraison à domicile (sinon retrait gratuit en point relais)
Chaque montant est confirmé par WhatsApp avant que le client n'ait à payer quoi que ce soit —
aucune surprise, aucun prélèvement caché.

=== COMMISSION FougahShop (FCFA/GNF selon pays) ===
La commission est progressive : 5 000 FCFA de base pour un panier jusqu'à 50€,
puis +3 300 FCFA pour chaque tranche de 50€ supplémentaire entamée, sans limite.
Exemples : 30€ → 5 000 FCFA · 75€ → 8 300 FCFA · 120€ → 11 600 FCFA · 300€ → 24 800 FCFA.
Ne calcule JAMAIS ce montant de tête — utilise TOUJOURS l'outil calculer_prix, qui applique
la formule exacte, gère la conversion GNF pour la Guinée, et présente déjà la structure en
plusieurs paiements séparés de façon claire.

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
→ Reconnaître : "Je comprends, ça peut sembler beaucoup d'un coup."
→ Clarifier d'abord que ce n'est PAS payé en une seule fois : "En fait tu paies en plusieurs petits paiements séparés, pas tout d'un coup — d'abord l'article + notre commission, puis le transport une fois le colis pesé, et éventuellement la livraison chez toi si tu la choisis."
→ Valoriser : "Tu économises le billet d'avion, les taxes de douane personnelles, et tu paies en Mobile Money sans carte bancaire."
→ Proposer : calculer le détail exact avec calculer_prix, qui montre bien chaque étape séparément.

** "C'est une arnaque / je fais pas confiance" **
→ Répondre avec calme et preuves : "Je comprends ta méfiance — c'est normal sur internet."
→ "FougahShop existe depuis 2023, des centaines de clients ont déjà reçu leurs commandes."
→ "Tu peux voir les photos de livraisons réelles sur fougahshop.com onglet 'Avis & photos'."
→ "On achète sur les vrais sites officiels (Nike.com, Zara.com...) avec notre carte bancaire."
→ "Si jamais l'article est en rupture de stock avant qu'on l'achète, remboursement intégral sous 48h, garanti."

=== POLITIQUE DE REMBOURSEMENT — RÈGLES EXACTES DES CGV ===
Ne dis JAMAIS simplement "remboursement intégral" sans préciser le cas. Chaque situation a sa propre règle :

• Rupture de stock AVANT achat → remboursement intégral sous 48h
• Annulation AVANT achat (statut "en attente" ou "payé") → remboursement intégral
• Annulation APRÈS achat → remboursement PARTIEL seulement (déduction des frais déjà engagés : achat, frais de retour vendeur le cas échéant)
• Article non conforme à la commande reçue → remboursement OU renvoi après vérification, selon les possibilités du vendeur (pas automatique, ça dépend du cas)
• Colis perdu en transit → on lance une procédure de recherche d'abord ; si le colis est déclaré perdu, remboursement partiel ou total étudié selon les circonstances (pas garanti à 100%)
• Refus de payer les frais de port → AUCUN remboursement de la commission déjà engagée pour l'achat
• Droits de douane et taxes à l'importation → entièrement à la charge du client, FougahShop ne les prend jamais en charge et n'est pas responsable des blocages douaniers

** "Je veux annuler ma commande" **
→ D'abord demander : "Est-ce qu'on a déjà acheté ton article, ou pas encore ?" (vérifier le statut avec suivi_commande si la personne a sa référence)
→ Si statut "en attente" ou "payé" (pas encore acheté) : "Tu peux annuler maintenant, remboursement intégral sur ton Mobile Money."
→ Si statut "acheté" ou plus avancé : "L'article est déjà acheté — l'annulation reste possible mais le remboursement sera partiel, après déduction de ce qu'on a déjà engagé."
→ Toujours : "Contacte-nous sur WhatsApp avec ta référence CMD-XXXX-XXXX pour qu'on traite ça rapidement."

** "L'article n'est pas disponible sur le site" **
→ "Dis-moi exactement ce que tu cherches — je vais te dire si on a une alternative chez un autre partenaire."
→ Utiliser l'outil info_boutique si le client demande des détails sur une boutique précise.

** "Vous livrez où exactement ?" **
→ Utiliser get_config pour les pays actifs et les modes de livraison.
→ À Conakry : livraison à domicile disponible.
→ Autres villes : point de retrait.

** "Comment je sais que c'est authentique ?" **
→ "On achète directement sur les sites officiels : Nike.com, Zara.com, Amazon.fr..."
→ "Jamais sur AliExpress ou des sites de copie."
→ "Tu reçois une confirmation d'achat avec le lien de la vraie commande."

** "Mon colis a disparu / je ne le trouve plus" **
→ Ne JAMAIS promettre un remboursement automatique — c'est étudié au cas par cas.
→ "On va d'abord lancer une recherche auprès du transporteur. Donne-moi ta référence CMD-XXXX-XXXX."
→ "Si le colis est officiellement déclaré perdu, on regarde ensemble la solution adaptée à ta situation."

** "Je n'ai pas payé les frais de port, qu'est-ce qui se passe ?" **
→ "Tu as 7 jours après réception du montant pour régler les frais de port."
→ "Sans paiement dans ce délai, on doit retourner l'article au vendeur — les frais de retour seront à ta charge."
→ "Et la commission déjà payée pour l'achat n'est pas remboursable dans ce cas."

** "Y'a des taxes de douane à payer ?" **
→ "Oui, possible — les droits de douane et taxes d'importation de ton pays sont entièrement à ta charge, FougahShop ne les gère pas."
→ "Si jamais ton colis est bloqué en douane, on t'accompagne dans les démarches mais on ne peut pas payer les frais à ta place."

=== BOUTIQUES PARTENAIRES (37+) ===
On a des dizaines de boutiques partenaires couvrant mode, sport, tech, beauté, maison.
Quand un client demande une boutique précise (Nike, Zara, Apple, Sephora...) ou veut savoir
où acheter un type d'article, utilise TOUJOURS l'outil info_boutique pour donner le lien exact
et les conseils spécifiques à jour — ne donne jamais un lien de mémoire, il pourrait être obsolète.
Si la boutique n'est pas dans notre liste, dis-le honnêtement et propose une alternative similaire.

=== SUIVI COMMANDE ===
Référence CMD-XXXX-XXXX + numéro de téléphone → onglet "Mon colis" sur fougahshop.com
Ou donner ref + tel pour que je vérifie avec l'outil suivi_commande.

=== PARRAINAGE ===
Code FGxxxxxx après première commande récupérée.
Réduction pour l'ami qui commande + gain pour le parrain.

=== GARANTIES ===
- Articles 100% authentiques achetés sur les sites officiels
- Remboursement intégral si rupture de stock avant achat (sous 48h)
- Paiement sécurisé Mobile Money
- Voir la section "POLITIQUE DE REMBOURSEMENT" ci-dessus pour les autres cas — ne jamais généraliser à "remboursement garanti" sans préciser la situation

=== LITIGES ET RÉCLAMATIONS ===
Si un client n'est pas satisfait après une première réponse : "On s'engage à répondre sous 24h ouvrées et à proposer une solution dans un délai de 5 jours ouvrés. Contacte-nous sur WhatsApp si ce n'est pas déjà fait."

=== PROTECTION DES DONNÉES ===
Si un client demande ce qu'on fait de ses données : "On utilise ton nom, numéro et adresse uniquement pour traiter ta commande — jamais vendues ni partagées à des fins commerciales. Tu peux demander l'accès, la modification ou la suppression de tes données à tout moment sur WhatsApp."

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
    {
        "name": "info_boutique",
        "description": "Donne l'URL exacte et les conseils spécifiques pour une boutique partenaire (Nike, Zara, Apple, Sephora...). Utiliser dès que le client mentionne une boutique précise ou demande où acheter un type d'article. Ne jamais donner un lien de mémoire — toujours passer par cet outil.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nom_boutique": {
                    "type": "string",
                    "description": "Nom de la boutique recherchée, ex: 'nike', 'zara', 'apple'. Insensible à la casse."
                }
            },
            "required": ["nom_boutique"]
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
    comm_fcfa  = get_commission(total_eu)

    # FIX : conversion GNF cohérente avec le vrai taux (pas division par 656 hardcodé)
    if monnaie == "GNF":
        comm_local = round(comm_fcfa * taux_gnf / taux_fcfa)
    else:
        comm_local = comm_fcfa

    panier_local = round(total_eu * taux)
    total_local  = panier_local + comm_local

    # Palier label — généré dynamiquement selon la tranche de 50€ réelle
    import math
    depasse_eu     = max(0.0, total_eu - COMMISSION_PALIER_EUROS)
    nb_tranches    = math.ceil(depasse_eu / COMMISSION_PALIER_EUROS)
    bas_tranche    = nb_tranches * COMMISSION_PALIER_EUROS
    haut_tranche   = bas_tranche + COMMISSION_PALIER_EUROS
    palier_label   = f"{bas_tranche}–{haut_tranche}€"

    r  = f"💰 **Estimation pour {qty}× article(s) à {prix_euros}€** ({pays_affiche})\n\n"
    is_guinee = "guin" in pays_lower
    nb_etapes = "2 à 3 paiements séparés" if is_guinee else "2 paiements séparés"
    r += f"Ta commande se règle en **{nb_etapes}**, pas en une seule fois — voici le détail :\n\n"
    r += f"**1️⃣ À la commande, maintenant :**\n"
    r += f"• Article : {panier_local:,} {monnaie}\n".replace(",", " ")
    r += f"• Commission FougahShop (panier {palier_label}) : {comm_local:,} {monnaie}\n".replace(",", " ")
    r += f"• **Total à payer pour commander : {total_local:,} {monnaie}**\n\n".replace(",", " ")
    r += f"**2️⃣ Après réception et pesée réelle en Europe :**\n"
    r += f"• Frais d'expédition — calculés sur le poids exact, on te donne le montant précis par WhatsApp avant de l'envoyer\n\n"
    if is_guinee:
        r += f"**3️⃣ Si tu choisis la livraison à domicile (optionnel, Guinée uniquement) :**\n"
        r += f"• Un dernier petit montant pour la livraison chez toi — sinon retrait gratuit en point relais\n\n"
    r += "_Chaque montant t'est confirmé par WhatsApp avant que tu aies à payer quoi que ce soit. Rien n'est surprise._"
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


def exec_info_boutique(nom_boutique: str) -> str:
    recherche = nom_boutique.lower().strip()
    # Normaliser quelques variantes d'écriture courantes
    recherche = recherche.replace("&", "&").replace(" et ", "&").replace("-", " ")

    # Correspondance directe : utiliser le nom propre du dictionnaire pour l'affichage
    info = BOUTIQUES.get(recherche)
    if info:
        nom_boutique = recherche

    # Correspondance partielle si pas de match direct (ex: "h et m" -> "h&m")
    if not info:
        for nom, data in BOUTIQUES.items():
            if recherche in nom or nom in recherche:
                info = data
                nom_boutique = nom
                break

    if not info:
        noms_disponibles = ", ".join(sorted(b.title() for b in BOUTIQUES.keys()))
        return (
            f"❌ Je ne trouve pas '{nom_boutique}' dans nos boutiques partenaires actuelles.\n\n"
            f"Voici nos boutiques disponibles : {noms_disponibles}.\n\n"
            f"Si tu cherches un type d'article précis (chaussures, parfum, téléphone...), dis-le-moi et je te propose une boutique adaptée."
        )

    r  = f"🛍️ **{nom_boutique.title()}** — {info['categorie']}\n\n"
    r += f"🔗 {info['url']}\n\n"
    if info.get("conseil"):
        r += f"💡 {info['conseil']}\n\n"
    r += "Copie le lien du produit exact que tu veux et envoie-le-nous sur WhatsApp pour qu'on confirme le prix."
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
                    elif block.name == "info_boutique":
                        res = exec_info_boutique(str(inp.get("nom_boutique", "")))
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
