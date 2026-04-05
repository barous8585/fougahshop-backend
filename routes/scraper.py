from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx
import re
import json

router = APIRouter(prefix="/api/scraper", tags=["scraper"])

ZENROWS_API_KEY = "7a92ab21726eae0cb290c90ec704b8a79ee6dad5"
ZENROWS_URL     = "https://api.zenrows.com/v1/"

# ── Règles d'extraction par site ─────────────────────────────
SITE_RULES = {
    "zara.com": {
        "nom":    ['h1[class*="product-detail-info__header-name"]', 'h1[class*="product-name"]', 'h1'],
        "prix":   ['span[class*="price__amount"]', 'span[class*="money-amount"]', '[class*="price"]'],
        "image":  ['img[class*="media-image"]', 'picture img', 'img[class*="product"]'],
        "js":     True,
    },
    "amazon.fr": {
        "nom":    ['#productTitle', 'h1#title', 'h1'],
        "prix":   ['.a-price .a-offscreen', '#priceblock_ourprice', '.a-price-whole'],
        "image":  ['#landingImage', '#imgBlkFront', 'img[class*="product"]'],
        "js":     True,
    },
    "amazon.com": {
        "nom":    ['#productTitle', 'h1#title', 'h1'],
        "prix":   ['.a-price .a-offscreen', '#priceblock_ourprice', '.a-price-whole'],
        "image":  ['#landingImage', '#imgBlkFront', 'img[class*="product"]'],
        "js":     True,
    },
    "nike.com": {
        "nom":    ['h1[class*="headline"]', 'h1[data-test*="product-title"]', 'h1'],
        "prix":   ['[data-test="product-price"]', '[class*="product-price"]', '[class*="price"]'],
        "image":  ['img[class*="responsive-image"]', 'img[class*="product"]', 'img'],
        "js":     True,
    },
    "hm.com": {
        "nom":    ['h1[class*="product-title"]', 'h1[class*="name"]', 'h1'],
        "prix":   ['[class*="price-value"]', '[class*="product-price"]', '[class*="price"]'],
        "image":  ['img[class*="product-image"]', 'img[class*="main"]', 'img'],
        "js":     True,
    },
    "asos.com": {
        "nom":    ['h1[class*="product-hero"]', '[data-testid="product-title"]', 'h1'],
        "prix":   ['[class*="current-price"]', '[data-testid*="price"]', '[class*="price"]'],
        "image":  ['img[class*="product-photo"]', 'img[class*="main-image"]', 'img'],
        "js":     True,
    },
    "zalando.fr": {
        "nom":    ['h1[class*="title"]', 'span[class*="title"]', 'h1'],
        "prix":   ['span[class*="price"]', '[class*="price-original"]', '[class*="price"]'],
        "image":  ['img[class*="article"]', 'img[class*="product"]', 'img'],
        "js":     True,
    },
    "shein.com": {
        "nom":    ['h1[class*="product-intro__head-name"]', 'h1', '.product-intro__head-name'],
        "prix":   ['.product-intro__head-price', '[class*="from-price"]', '[class*="price"]'],
        "image":  ['img[class*="product-intro__main-img"]', 'img[class*="crop-image"]', 'img'],
        "js":     True,
    },
    "decathlon.fr": {
        "nom":    ['h1[class*="product-header"]', 'h1[itemprop="name"]', 'h1'],
        "prix":   ['[class*="product-price"]', '[itemprop="price"]', '[class*="price"]'],
        "image":  ['img[class*="product-header"]', 'img[class*="product"]', 'img'],
        "js":     False,
    },
    "fnac.com": {
        "nom":    ['h1[class*="f-title"]', 'h1[itemprop="name"]', 'h1'],
        "prix":   ['span[class*="userPrice"]', '[itemprop="price"]', '[class*="price"]'],
        "image":  ['img[class*="product-img"]', 'img[itemprop="image"]', 'img'],
        "js":     False,
    },
}

def get_site_rules(url: str):
    """Retourne les règles d'extraction pour le site donné"""
    for domain, rules in SITE_RULES.items():
        if domain in url:
            return rules
    # Règles génériques pour les sites non listés
    return {
        "nom":   ['h1', '[class*="product-title"]', '[class*="product-name"]', 'title'],
        "prix":  ['[class*="price"]', '[itemprop="price"]', '[class*="amount"]'],
        "image": ['img[class*="product"]', 'img[class*="main"]', 'meta[property="og:image"]'],
        "js":    True,
    }

def extraire_depuis_html(html: str, rules: dict) -> dict:
    """Extraction simple par regex depuis le HTML retourné"""
    result = {"nom": None, "prix": None, "image": None}

    # Essayer d'extraire les Open Graph meta tags (universels)
    og_title = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    og_image = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    og_price = re.search(r'<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)

    if og_title: result["nom"]   = og_title.group(1).strip()
    if og_image: result["image"] = og_image.group(1).strip()
    if og_price: result["prix"]  = og_price.group(1).strip() + " €"

    # Schema.org JSON-LD (très fiable)
    ld_matches = re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I)
    for ld in ld_matches:
        try:
            data = json.loads(ld)
            if isinstance(data, list): data = data[0]
            if data.get("@type") in ("Product", "IndividualProduct"):
                if data.get("name"):    result["nom"]   = data["name"]
                if data.get("image"):
                    img = data["image"]
                    result["image"] = img[0] if isinstance(img, list) else img
                offers = data.get("offers", {})
                if isinstance(offers, list): offers = offers[0]
                if offers.get("price"):
                    currency = offers.get("priceCurrency", "€")
                    result["prix"] = f"{offers['price']} {currency}"
        except Exception:
            pass

    # Fallback : title tag
    if not result["nom"]:
        title_match = re.search(r'<title[^>]*>([^<]+)</title>', html, re.I)
        if title_match:
            result["nom"] = title_match.group(1).strip().split('|')[0].strip()

    # Nettoyage
    if result["nom"]: result["nom"] = re.sub(r'\s+', ' ', result["nom"]).strip()[:120]
    if result["prix"]: result["prix"] = result["prix"].strip()[:30]

    return result

class ScrapeRequest(BaseModel):
    url: str

@router.post("/produit")
async def scraper_produit(body: ScrapeRequest):
    url = body.url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "URL invalide")

    rules = get_site_rules(url)

    # Paramètres ZenRows
    params = {
        "apikey":      ZENROWS_API_KEY,
        "url":         url,
        "js_render":   "true" if rules.get("js") else "false",
        "premium_proxy": "false",
        "wait":        "2000",   # attendre 2s pour le JS
    }

    # Ajouter les sélecteurs CSS si disponibles
    css_extractors = {}
    if rules.get("nom"):   css_extractors["nom"]   = rules["nom"][0]
    if rules.get("prix"):  css_extractors["prix"]  = rules["prix"][0]
    if rules.get("image"): css_extractors["image"] = rules["image"][0]

    if css_extractors:
        params["css_extractor"] = json.dumps(css_extractors)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(ZENROWS_URL, params=params)

        if resp.status_code == 422:
            raise HTTPException(422, "Site protégé — impossible d'extraire")

        if resp.status_code != 200:
            raise HTTPException(resp.status_code, f"Erreur ZenRows: {resp.text[:200]}")

        # Essayer de parser le JSON si css_extractor utilisé
        data = {}
        try:
            data = resp.json()
        except Exception:
            # Fallback : analyser le HTML brut
            data = extraire_depuis_html(resp.text, rules)

        # Normaliser le résultat
        nom   = data.get("nom")   or data.get("title")   or ""
        prix  = data.get("prix")  or data.get("price")   or ""
        image = data.get("image") or data.get("img")     or ""

        # Nettoyer le prix — extraire uniquement les chiffres + devise
        if prix and isinstance(prix, str):
            prix_match = re.search(r'[\d\s.,]+\s*[€$£]|[€$£]\s*[\d\s.,]+', prix)
            if prix_match: prix = prix_match.group(0).strip()

        # Si image est une liste, prendre le premier
        if isinstance(image, list): image = image[0] if image else ""

        if not nom and not prix:
            raise HTTPException(404, "Aucune information trouvée sur cette page")

        return {
            "ok":    True,
            "nom":   nom.strip()  if nom   else "",
            "prix":  prix.strip() if prix  else "",
            "image": image.strip() if image else "",
            "url":   url,
            "site":  next((d for d in SITE_RULES if d in url), "autre"),
        }

    except httpx.TimeoutException:
        raise HTTPException(408, "Délai d'attente dépassé — site trop lent")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erreur scraping: {str(e)[:100]}")
