from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx
import re
import json
import random

router = APIRouter(prefix="/api/scraper", tags=["scraper"])

# ── User-Agents réalistes ─────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

HEADERS_BASE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# ── Microlink ─────────────────────────────────────────────────
MICROLINK_URL = "https://api.microlink.io/"

# ── ZenRows (fallback Amazon/Nike uniquement) ─────────────────
ZENROWS_API_KEY = "7a92ab21726eae0cb290c90ec704b8a79ee6dad5"
ZENROWS_URL     = "https://api.zenrows.com/v1/"

# Sites qui bloquent le fetch direct — on va direct Microlink
SITES_ANTI_BOT = ["amazon.", "nike.com", "adidas.com", "apple.com", "ikea.com"]

def get_site_name(url: str) -> str:
    try:
        host = re.sub(r'^www\.', '', re.search(r'https?://([^/]+)', url).group(1))
        return host.split('.')[0].capitalize()
    except:
        return ""

def extraire_og_et_schema(html: str) -> dict:
    """
    Extraction universelle via Open Graph + Schema.org JSON-LD.
    Fonctionne sur ~60% des sites e-commerce sans proxy.
    """
    result = {"nom": None, "prix": None, "image": None}

    # ── 1. Schema.org JSON-LD (le plus fiable) ────────────────
    ld_matches = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.S | re.I
    )
    for ld_raw in ld_matches:
        try:
            ld = json.loads(ld_raw)
            items = ld if isinstance(ld, list) else [ld]
            for item in items:
                if item.get("@type") in ("Product", "IndividualProduct"):
                    if item.get("name"):
                        result["nom"] = str(item["name"]).strip()
                    img = item.get("image")
                    if img:
                        result["image"] = (img[0] if isinstance(img, list) else
                                           img.get("url") if isinstance(img, dict) else img)
                    offers = item.get("offers", {})
                    if isinstance(offers, list): offers = offers[0] if offers else {}
                    price = offers.get("price") or offers.get("lowPrice")
                    currency = offers.get("priceCurrency", "EUR")
                    symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency, currency)
                    if price:
                        result["prix"] = f"{price} {symbol}"
                    if result["nom"]:
                        break
        except Exception:
            pass
        if result["nom"] and result["prix"]:
            break

    # ── 2. Open Graph meta tags ───────────────────────────────
    def og(prop):
        m = re.search(
            r'<meta[^>]+(?:property|name)=["\']' + prop + r'["\'][^>]+content=["\']([^"\']+)["\']',
            html, re.I
        )
        if not m:
            m = re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']' + prop + r'["\']',
                html, re.I
            )
        return m.group(1).strip() if m else None

    if not result["nom"]:
        result["nom"] = og("og:title") or og("twitter:title")
    if not result["image"]:
        result["image"] = og("og:image") or og("twitter:image")
    if not result["prix"]:
        # Meta prix (Shein, certains sites)
        result["prix"] = (
            og("product:price:amount") and
            (og("product:price:amount") + " " + (og("product:price:currency") or "€"))
        ) or og("og:price:amount")

    # ── 3. Fallback title tag ─────────────────────────────────
    if not result["nom"]:
        m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.I)
        if m:
            title = m.group(1).strip()
            # Nettoyer le nom du site du titre
            title = re.split(r'\s*[\|–\-]\s*', title)[0].strip()
            result["nom"] = title

    # ── Nettoyage final ───────────────────────────────────────
    if result["nom"]:
        result["nom"] = re.sub(r'\s+', ' ', result["nom"]).strip()[:120]
        # Décoder les entités HTML basiques
        result["nom"] = (result["nom"]
            .replace("&amp;", "&").replace("&quot;", '"')
            .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    if result["prix"]:
        result["prix"] = re.sub(r'\s+', ' ', str(result["prix"])).strip()[:30]
    if result["image"] and not result["image"].startswith("http"):
        result["image"] = ""

    return result


async def fetch_direct(url: str) -> dict | None:
    """Fetch direct avec User-Agent mobile réaliste."""
    headers = {**HEADERS_BASE, "User-Agent": random.choice(USER_AGENTS)}
    try:
        async with httpx.AsyncClient(
            timeout=12,
            follow_redirects=True,
            headers=headers
        ) as client:
            resp = await client.get(url)
        if resp.status_code == 200 and len(resp.text) > 1000:
            data = extraire_og_et_schema(resp.text)
            if data["nom"]:
                return data
    except Exception:
        pass
    return None


async def fetch_microlink(url: str) -> dict | None:
    """Microlink API — headless Chrome gratuit, 1000 req/mois."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(MICROLINK_URL, params={
                "url": url,
                "meta": "true",
                "screenshot": "false",
            })
        if resp.status_code == 200:
            d = resp.json()
            if d.get("status") == "success" and d.get("data"):
                dd = d["data"]
                nom = dd.get("title") or dd.get("og:title") or ""
                img = (dd.get("image") or {}).get("url") or dd.get("og:image") or ""
                desc = dd.get("description") or ""
                # Extraire prix depuis description si présent
                prix = None
                pm = re.search(r'[\d\s,\.]+\s*[€£$]|[€£$]\s*[\d\s,\.]+', desc)
                if pm: prix = pm.group(0).strip()
                if dd.get("price"):
                    p = dd["price"]
                    prix = f"{p.get('amount', '')} {p.get('currency', '€')}".strip()
                # Nettoyer nom
                nom = re.split(r'\s*[\|–\-]\s*[A-Z]', nom)[0].strip()[:120]
                if nom:
                    return {"nom": nom, "prix": prix, "image": img}
    except Exception:
        pass
    return None


async def fetch_zenrows(url: str) -> dict | None:
    """ZenRows avec premium_proxy — uniquement pour Amazon/Nike."""
    try:
        params = {
            "apikey": ZENROWS_API_KEY,
            "url": url,
            "js_render": "true",
            "premium_proxy": "true",
            "wait": "3000",
        }
        async with httpx.AsyncClient(timeout=35) as client:
            resp = await client.get(ZENROWS_URL, params=params)
        if resp.status_code == 200 and len(resp.text) > 1000:
            data = extraire_og_et_schema(resp.text)
            if data["nom"]:
                return data
    except Exception:
        pass
    return None


class ScrapeRequest(BaseModel):
    url: str


@router.post("/produit")
async def scraper_produit(body: ScrapeRequest):
    url = body.url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "URL invalide")

    site_name = get_site_name(url)
    needs_proxy = any(s in url for s in SITES_ANTI_BOT)

    result = None

    if needs_proxy:
        # Amazon, Nike, Adidas → ZenRows premium d'abord, Microlink en fallback
        result = await fetch_zenrows(url)
        if not result:
            result = await fetch_microlink(url)
    else:
        # Zara, H&M, ASOS, Zalando, Shein, Decathlon, Fnac, etc.
        # → Fetch direct d'abord (gratuit), Microlink en fallback
        result = await fetch_direct(url)
        if not result:
            result = await fetch_microlink(url)

    if not result or not result.get("nom"):
        raise HTTPException(404, "Aucune information trouvée — le site bloque l'extraction")

    return {
        "ok":    True,
        "nom":   result.get("nom", ""),
        "prix":  result.get("prix") or "",
        "image": result.get("image") or "",
        "img":   result.get("image") or "",  # alias pour le frontend
        "url":   url,
        "site":  site_name,
    }


# ══════════════════════════════════════════════════════════════
# SCRAPER PANIER — inchangé
# ══════════════════════════════════════════════════════════════

PANIER_RULES = {
    "zara.com":    {"js": True,  "wait": "3000"},
    "hm.com":      {"js": True,  "wait": "3000"},
    "asos.com":    {"js": True,  "wait": "3000"},
    "zalando.fr":  {"js": True,  "wait": "3000"},
}

class PanierRequest(BaseModel):
    url: str

@router.post("/panier")
async def scraper_panier(body: PanierRequest):
    url = body.url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "URL invalide")

    site = next((s for s in PANIER_RULES if s in url), None)
    rules = PANIER_RULES.get(site, {"js": True, "wait": "3000"})

    params = {
        "apikey":        ZENROWS_API_KEY,
        "url":           url,
        "js_render":     "true",
        "wait":          rules.get("wait", "3000"),
        "premium_proxy": "false",
    }

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.get(ZENROWS_URL, params=params)

        if resp.status_code != 200:
            raise HTTPException(resp.status_code, f"Erreur scraping: {resp.text[:200]}")

        html_content = resp.text
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
        raise HTTPException(408, "Délai dépassé")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erreur: {str(e)[:100]}")


def find_cart_items(data, depth=0):
    if depth > 8: return []
    results = []
    if isinstance(data, dict):
        for key in ['items','cartItems','lineItems','products','entries',
                    'bagItems','cart_items','articles','orderItems']:
            if key in data and isinstance(data[key], list):
                results.extend(data[key])
        for v in data.values():
            if isinstance(v, (dict, list)):
                results.extend(find_cart_items(v, depth+1))
    elif isinstance(data, list):
        for item in data:
            results.extend(find_cart_items(item, depth+1))
    return results[:20]

def normaliser_article(item, site):
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
        qty = int(item.get("quantity") or item.get("qty") or 1)
        return {
            "nom":    str(nom)[:120],
            "prix":   str(prix)[:30],
            "img":    str(img)[:500],
            "lien":   str(lien)[:500],
            "taille": str(item.get("size") or item.get("selectedSize") or "")[:30],
            "couleur":str(item.get("color") or item.get("colour") or "")[:30],
            "qty":    qty,
        }
    except Exception:
        return None

def extraire_panier_generique(html, url):
    articles = []
    ld_matches = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.S | re.I
    )
    for ld in ld_matches:
        try:
            data = json.loads(ld)
            items_list = data if isinstance(data, list) else [data]
            for item in items_list:
                art = normaliser_article(item, url)
                if art: articles.append(art)
        except Exception:
            pass
    return articles
