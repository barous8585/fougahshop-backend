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

# ══════════════════════════════════════════════════════════════
# EXTRACTION PANIER COMPLET
# ══════════════════════════════════════════════════════════════

PANIER_RULES = {
    "zara.com": {
        "js": True,
        "wait": "3000",
        "articles_selector": "[class*='shop-cart-item']",
    },
    "hm.com": {
        "js": True,
        "wait": "3000",
        "articles_selector": "[class*='product-item']",
    },
    "asos.com": {
        "js": True,
        "wait": "3000",
        "articles_selector": "[class*='item-details']",
    },
    "zalando.fr": {
        "js": True,
        "wait": "3000",
        "articles_selector": "[class*='article']",
    },
}

class PanierRequest(BaseModel):
    url: str

@router.post("/panier")
async def scraper_panier(body: PanierRequest):
    url = body.url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "URL invalide")

    # Identifier le site
    site = next((s for s in PANIER_RULES if s in url), None)
    rules = PANIER_RULES.get(site, {"js": True, "wait": "3000"})

    # Paramètres ZenRows — JS rendering obligatoire pour les paniers
    params = {
        "apikey":    ZENROWS_API_KEY,
        "url":       url,
        "js_render": "true",
        "wait":      rules.get("wait", "3000"),
        "premium_proxy": "false",
    }

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.get(ZENROWS_URL, params=params)

        if resp.status_code != 200:
            raise HTTPException(resp.status_code, f"Erreur ZenRows: {resp.text[:200]}")

        html_content = resp.text
        articles = []

        # ── ZARA ─────────────────────────────────────────────
        if "zara.com" in url:
            articles = extraire_panier_zara(html_content)

        # ── H&M ──────────────────────────────────────────────
        elif "hm.com" in url:
            articles = extraire_panier_hm(html_content)

        # ── ASOS ─────────────────────────────────────────────
        elif "asos.com" in url:
            articles = extraire_panier_asos(html_content)

        # ── ZALANDO ──────────────────────────────────────────
        elif "zalando" in url:
            articles = extraire_panier_zalando(html_content)

        # ── Générique — Open Graph + Schema.org ──────────────
        else:
            articles = extraire_panier_generique(html_content, url)

        if not articles:
            # Fallback : essayer l'extraction générique
            articles = extraire_panier_generique(html_content, url)

        if not articles:
            raise HTTPException(404, "Aucun article trouvé dans ce panier")

        return {
            "ok":       True,
            "site":     site or "autre",
            "nb":       len(articles),
            "articles": articles,
        }

    except httpx.TimeoutException:
        raise HTTPException(408, "Délai dépassé — le site met trop de temps à répondre")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erreur: {str(e)[:100]}")


def extraire_panier_zara(html: str) -> list:
    """Extraction panier Zara"""
    articles = []
    try:
        # JSON dans les scripts next.js / data
        json_matches = re.findall(r'__NEXT_DATA__[^>]*>([^<]+)</script>', html)
        for jm in json_matches:
            try:
                data = json.loads(jm)
                # Chercher les items du panier dans le JSON
                items = find_cart_items(data)
                if items:
                    for item in items:
                        art = normaliser_article(item, "zara.com")
                        if art: articles.append(art)
                    return articles
            except Exception:
                pass

        # Fallback regex HTML
        # Chercher les blocs produit dans le HTML
        blocs = re.findall(
            r'class="[^"]*shop-cart-item[^"]*"[^>]*>(.*?)</li>',
            html, re.S | re.I
        )
        for bloc in blocs[:10]:
            art = extraire_article_html(bloc)
            if art: articles.append(art)

    except Exception as e:
        print(f"Erreur Zara: {e}")

    return articles


def extraire_panier_hm(html: str) -> list:
    """Extraction panier H&M"""
    articles = []
    try:
        # H&M utilise souvent Redux store dans window.__data
        data_match = re.search(r'window\.__data\s*=\s*({.*?});', html, re.S)
        if data_match:
            data = json.loads(data_match.group(1))
            items = find_cart_items(data)
            for item in items:
                art = normaliser_article(item, "hm.com")
                if art: articles.append(art)
            if articles: return articles

        # Fallback HTML
        blocs = re.findall(
            r'class="[^"]*product-item[^"]*"[^>]*>(.*?)</article>',
            html, re.S | re.I
        )
        for bloc in blocs[:10]:
            art = extraire_article_html(bloc)
            if art: articles.append(art)

    except Exception as e:
        print(f"Erreur H&M: {e}")

    return articles


def extraire_panier_asos(html: str) -> list:
    """Extraction panier ASOS"""
    articles = []
    try:
        # ASOS stocke le panier en JSON dans window.asos
        data_match = re.search(r'window\.asos\s*=\s*({.*?});\s*\n', html, re.S)
        if data_match:
            data = json.loads(data_match.group(1))
            items = find_cart_items(data)
            for item in items:
                art = normaliser_article(item, "asos.com")
                if art: articles.append(art)
            if articles: return articles

        # Fallback HTML
        blocs = re.findall(
            r'data-testid="[^"]*bag-item[^"]*"[^>]*>(.*?)</article>',
            html, re.S | re.I
        )
        for bloc in blocs[:10]:
            art = extraire_article_html(bloc)
            if art: articles.append(art)

    except Exception as e:
        print(f"Erreur ASOS: {e}")

    return articles


def extraire_panier_zalando(html: str) -> list:
    """Extraction panier Zalando"""
    articles = []
    try:
        data_match = re.search(r'<script[^>]*type="application/json"[^>]*>({.*?})</script>', html, re.S)
        if data_match:
            data = json.loads(data_match.group(1))
            items = find_cart_items(data)
            for item in items:
                art = normaliser_article(item, "zalando.fr")
                if art: articles.append(art)
            if articles: return articles

        blocs = re.findall(
            r'class="[^"]*article[^"]*"[^>]*>(.*?)</article>',
            html, re.S | re.I
        )
        for bloc in blocs[:10]:
            art = extraire_article_html(bloc)
            if art: articles.append(art)

    except Exception as e:
        print(f"Erreur Zalando: {e}")

    return articles


def extraire_panier_generique(html: str, url: str) -> list:
    """Extraction générique via Schema.org et Open Graph"""
    articles = []
    try:
        # Schema.org ItemList ou Product
        ld_matches = re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.S | re.I
        )
        for ld in ld_matches:
            try:
                data = json.loads(ld)
                if isinstance(data, list):
                    for item in data:
                        art = normaliser_article(item, url)
                        if art: articles.append(art)
                elif data.get("@type") in ("ItemList", "ShoppingCart"):
                    for item in data.get("itemListElement", []):
                        art = normaliser_article(item, url)
                        if art: articles.append(art)
                elif data.get("@type") == "Product":
                    art = normaliser_article(data, url)
                    if art: articles.append(art)
            except Exception:
                pass

    except Exception as e:
        print(f"Erreur générique: {e}")

    return articles


def find_cart_items(data: dict, depth: int = 0) -> list:
    """Cherche récursivement les items du panier dans un dict JSON"""
    if depth > 8: return []
    results = []
    if isinstance(data, dict):
        # Clés communes pour les items de panier
        for key in ['items', 'cartItems', 'lineItems', 'products', 'entries',
                    'bagItems', 'cart_items', 'articles', 'orderItems']:
            if key in data and isinstance(data[key], list):
                results.extend(data[key])
        # Récursion
        for v in data.values():
            if isinstance(v, (dict, list)):
                results.extend(find_cart_items(v, depth+1))
    elif isinstance(data, list):
        for item in data:
            results.extend(find_cart_items(item, depth+1))
    return results[:20]  # Max 20 articles


def normaliser_article(item: dict, site: str) -> dict:
    """Normalise un article extrait en format FougahShop"""
    if not isinstance(item, dict): return None
    try:
        nom = (item.get("name") or item.get("productName") or
               item.get("title") or item.get("displayName") or "")
        if not nom: return None

        prix_raw = (item.get("price") or item.get("currentPrice") or
                    item.get("salePrice") or item.get("amount") or 0)

        if isinstance(prix_raw, dict):
            prix_raw = prix_raw.get("value") or prix_raw.get("amount") or 0

        prix = f"{prix_raw} €" if prix_raw else ""

        img = (item.get("image") or item.get("imageUrl") or
               item.get("img") or item.get("thumbnail") or "")
        if isinstance(img, list): img = img[0] if img else ""
        if isinstance(img, dict): img = img.get("url") or img.get("src") or ""

        lien = (item.get("url") or item.get("productUrl") or
                item.get("href") or item.get("link") or "")
        if lien and not lien.startswith("http"):
            domain = next((s for s in PANIER_RULES if s in site), "")
            if domain: lien = f"https://www.{domain}{lien}"

        taille = (item.get("size") or item.get("selectedSize") or
                  item.get("variant") or "")
        couleur = (item.get("color") or item.get("colour") or
                   item.get("selectedColor") or "")
        qty = int(item.get("quantity") or item.get("qty") or 1)

        return {
            "nom":    str(nom)[:120],
            "prix":   str(prix)[:30],
            "img":    str(img)[:500],
            "lien":   str(lien)[:500],
            "taille": str(taille)[:30],
            "couleur":str(couleur)[:30],
            "qty":    qty,
        }
    except Exception:
        return None


def extraire_article_html(bloc: str) -> dict:
    """Extrait un article depuis un bloc HTML brut"""
    try:
        nom_m = re.search(r'<(?:h[1-6]|span|p)[^>]*class="[^"]*(?:name|title|product)[^"]*"[^>]*>([^<]+)', bloc, re.I)
        prix_m = re.search(r'[\d\s]+[.,]\d{2}\s*€|€\s*[\d\s]+[.,]\d{2}', bloc)
        img_m  = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', bloc, re.I)
        lien_m = re.search(r'<a[^>]+href=["\']([^"\']+)["\']', bloc, re.I)

        nom = nom_m.group(1).strip() if nom_m else ""
        if not nom: return None

        return {
            "nom":    nom[:120],
            "prix":   prix_m.group(0).strip() if prix_m else "",
            "img":    img_m.group(1) if img_m else "",
            "lien":   lien_m.group(1) if lien_m else "",
            "taille": "",
            "couleur": "",
            "qty":    1,
        }
    except Exception:
        return None
