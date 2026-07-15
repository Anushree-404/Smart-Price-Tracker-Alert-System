"""
Smart Price Tracker & Alert System
===================================
Flask backend with SQLite storage, BeautifulSoup scrapers for Amazon/Flipkart,
APScheduler background price checks, and Gmail SMTP email alerts.
"""

import os
import re
import sqlite3
import smtplib
import loggING
import random
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from contextlib import contextmanager

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

# curl_cffi impersonates a real Chrome TLS fingerprint — used for Ajio (Cloudflare-protected)
try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False
    logger_placeholder = None  # logger not yet defined here; warning issued later

# ── Load environment variables from .env ──────────────────────────────────────
load_dotenv()

# ── Flask app setup ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")
CORS(app)  # Allow cross-origin requests during development

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
DATABASE = os.path.join(os.path.dirname(__file__), "price_tracker.db")
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD", "")
CHECK_INTERVAL_HOURS = int(os.getenv("CHECK_INTERVAL_HOURS", 6))

# Rotating User-Agent pool — helps avoid 403s from e-commerce sites
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
    "Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]


# ── Database helpers ──────────────────────────────────────────────────────────

@contextmanager
def get_db():
    """Context manager yielding a SQLite connection with row_factory."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row  # Rows accessible as dicts
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables if they don't already exist."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                url             TEXT    NOT NULL UNIQUE,
                name            TEXT    NOT NULL,
                website         TEXT    NOT NULL,   -- 'amazon' | 'flipkart' | 'other'
                original_price  REAL    NOT NULL,
                current_price   REAL    NOT NULL,
                image_url       TEXT    DEFAULT '',
                added_at        TEXT    NOT NULL,
                last_checked    TEXT
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id  INTEGER NOT NULL,
                price       REAL    NOT NULL,
                checked_at  TEXT    NOT NULL,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id      INTEGER NOT NULL,
                email           TEXT    NOT NULL,
                threshold_pct   REAL    NOT NULL,  -- alert when price drops by this %
                is_active       INTEGER DEFAULT 1,
                created_at      TEXT    NOT NULL,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
            );
        """)
    logger.info("Database initialised at %s", DATABASE)


# ── Scraper utilities ─────────────────────────────────────────────────────────

def get_headers():
    """Return request headers with a randomly chosen User-Agent."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def clean_price(raw: str) -> float:
    """
    Strip currency symbols, commas, and whitespace from a price string,
    then convert to float.  Returns 0.0 on failure.
    """
    if not raw:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def scrape_amazon(url: str) -> dict:
    """
    Scrape product name and price from an Amazon India product page.

    Selector notes:
      - #productTitle        : Main product title element
      - .a-price-whole       : Integer part of the displayed price
      - .a-price .a-offscreen: Hidden full price string (most reliable)
      - #corePriceDisplay_desktop_feature_div: Price container for most listings
    """
    try:
        # Add a small delay to be polite to the server
        time.sleep(random.uniform(1, 3))
        resp = requests.get(url, headers=get_headers(), timeout=15)
        resp.raise_for_status()
        # html.parser is Python's built-in HTML parser (no C extension needed)
        soup = BeautifulSoup(resp.content, "html.parser")

        # ── Product name ──────────────────────────────────────────────────────
        name_tag = soup.find("span", id="productTitle")
        name = name_tag.get_text(strip=True) if name_tag else "Unknown Product"

        # ── Price: try multiple selectors in priority order ───────────────────
        price = 0.0

        # 1. Hidden offscreen span holds the full formatted price (e.g. "₹1,299")
        offscreen = soup.select_one(".a-price .a-offscreen")
        if offscreen:
            price = clean_price(offscreen.get_text())

        # 2. Fallback: integer-only price-whole span
        if not price:
            whole = soup.select_one(".a-price-whole")
            if whole:
                price = clean_price(whole.get_text())

        # 3. Fallback: core price display block
        if not price:
            core = soup.select_one("#corePriceDisplay_desktop_feature_div .a-offscreen")
            if core:
                price = clean_price(core.get_text())

        # ── Product image ─────────────────────────────────────────────────────
        img_tag = soup.find("img", id="landingImage") or soup.find("img", id="imgBlkFront")
        image_url = img_tag["src"] if img_tag and img_tag.get("src") else ""

        if not price:
            raise ValueError("Could not extract price — Amazon may have changed its layout")

        return {"name": name, "price": price, "image_url": image_url, "website": "amazon"}

    except requests.RequestException as exc:
        raise RuntimeError(f"Network error scraping Amazon: {exc}") from exc


def scrape_flipkart(url: str) -> dict:
    """
    Scrape product name and price from a Flipkart product page.

    Selector notes:
      - .B_NuCI               : Product title (most product types)
      - .title-1              : Alternative title selector (older pages)
      - ._30jeq3._16Jk6d      : Final/discounted price (strong match)
      - ._30jeq3              : Generic price class used across product types
    """
    try:
        time.sleep(random.uniform(1, 3))
        resp = requests.get(url, headers=get_headers(), timeout=15)
        resp.raise_for_status()
        # html.parser is Python's built-in HTML parser (no C extension needed)
        soup = BeautifulSoup(resp.content, "html.parser")

        # ── Product name ──────────────────────────────────────────────────────
        name_tag = (
            soup.find("span", class_="B_NuCI")         # Electronics / most items
            or soup.find("h1", class_="title-1")        # Some fashion items
            or soup.find("span", class_="_35KyD6")      # Alternate selector
        )
        name = name_tag.get_text(strip=True) if name_tag else "Unknown Product"

        # ── Price ─────────────────────────────────────────────────────────────
        price = 0.0

        # 1. Discounted final price (has two classes applied)
        price_tag = soup.select_one("._30jeq3._16Jk6d")
        if price_tag:
            price = clean_price(price_tag.get_text())

        # 2. Generic price class
        if not price:
            price_tag = soup.select_one("._30jeq3")
            if price_tag:
                price = clean_price(price_tag.get_text())

        # 3. Div-based price container
        if not price:
            price_tag = soup.select_one("div._25b18c ._30jeq3")
            if price_tag:
                price = clean_price(price_tag.get_text())

        # ── Product image ─────────────────────────────────────────────────────
        img_tag = soup.select_one("._396cs4") or soup.select_one("img._2r_T1I")
        image_url = img_tag["src"] if img_tag and img_tag.get("src") else ""

        if not price:
            raise ValueError("Could not extract price — Flipkart may have changed its layout")

        return {"name": name, "price": price, "image_url": image_url, "website": "flipkart"}

    except requests.RequestException as exc:
        raise RuntimeError(f"Network error scraping Flipkart: {exc}") from exc


def detect_website(url: str) -> str:
    """Detect which supported website a URL belongs to."""
    url_lower = url.lower()
    if "amazon" in url_lower:
        return "amazon"
    if "flipkart" in url_lower:
        return "flipkart"
    if "myntra" in url_lower:
        return "myntra"
    if "meesho" in url_lower:
        return "meesho"
    if "ajio" in url_lower:
        return "ajio"
    if "nykaa" in url_lower:
        return "nykaa"
    if "pantaloons" in url_lower:
        return "pantaloons"
    return "other"


# ── Shared helper: extract data from JSON-LD <script> tags ───────────────────

def _extract_jsonld(soup: BeautifulSoup) -> dict:
    """
    Parse all <script type="application/ld+json"> blocks on the page and
    return the first one that looks like a Product schema.
    Returns {} if none found.

    JSON-LD Product schema spec:
      https://schema.org/Product
    Key fields used: name, offers.price, offers.lowPrice, image
    """
    import json as _json
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = _json.loads(tag.string or "")
        except (_json.JSONDecodeError, TypeError):
            continue
        # Could be a single object or a list
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            # Match @type = Product (or array containing Product)
            types = item.get("@type", "")
            if isinstance(types, list):
                types = " ".join(types)
            if "product" not in types.lower():
                continue
            offers = item.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = clean_price(str(
                offers.get("price") or offers.get("lowPrice") or 0
            ))
            name  = item.get("name", "")
            image = item.get("image", "")
            if isinstance(image, list):
                image = image[0] if image else ""
            if price or name:
                return {"name": name, "price": price, "image_url": image}
    return {}


def _extract_next_data(soup: BeautifulSoup, price_keys: list, name_keys: list) -> dict:
    """
    Many Next.js / React sites embed their full page state as JSON inside
    <script id="__NEXT_DATA__">.  Walk the nested dict looking for any of
    the given key names and return the first match for price and name.

    price_keys / name_keys: ordered list of keys to search for.
    """
    import json as _json

    script = soup.find("script", id="__NEXT_DATA__")
    if not script:
        return {}
    try:
        data = _json.loads(script.string or "")
    except (_json.JSONDecodeError, TypeError):
        return {}

    def _walk(node, keys):
        """DFS search through a nested dict/list for the first matching key."""
        if isinstance(node, dict):
            for k in keys:
                if k in node and node[k]:
                    return str(node[k])
            for v in node.values():
                result = _walk(v, keys)
                if result:
                    return result
        elif isinstance(node, list):
            for item in node:
                result = _walk(item, keys)
                if result:
                    return result
        return None

    price_raw = _walk(data, price_keys)
    name_raw  = _walk(data, name_keys)
    return {
        "price": clean_price(price_raw or "0"),
        "name":  name_raw or "",
        "image_url": "",
    }


# ── Myntra scraper ────────────────────────────────────────────────────────────

def scrape_myntra(url: str) -> dict:
    """
    Scrape Myntra product page.

    Strategy (in order):
      1. JSON-LD <script type="application/ld+json"> — most reliable
      2. window.__myx_app_state__ JSON blob in a <script> tag
      3. CSS selectors:
         - h1.pdp-name           : product title
         - span.pdp-price strong : discounted price
         - span.pdp-mrp  s       : original MRP (used as fallback)
    """
    try:
        time.sleep(random.uniform(1, 3))
        resp = requests.get(url, headers=get_headers(), timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")

        # 1. JSON-LD
        jld = _extract_jsonld(soup)
        if jld.get("price") and jld.get("name"):
            return {**jld, "website": "myntra"}

        # 2. Embedded window state JSON (Myntra inlines product data as JS var)
        import json as _json
        for script in soup.find_all("script"):
            text = script.string or ""
            # Look for "pdpData" key which contains price info
            match = re.search(r'"pdpData"\s*:\s*(\{.*?"mrp"\s*:\s*\d+.*?\})', text, re.S)
            if match:
                try:
                    fragment = match.group(1)
                    price_m  = re.search(r'"price"\s*:\s*(\d+)', fragment)
                    name_m   = re.search(r'"name"\s*:\s*"([^"]+)"', fragment)
                    if price_m:
                        return {
                            "name":      name_m.group(1) if name_m else "Myntra Product",
                            "price":     float(price_m.group(1)),
                            "image_url": "",
                            "website":   "myntra",
                        }
                except Exception:
                    pass

        # 3. CSS selectors
        name_tag  = soup.find("h1", class_="pdp-name") or soup.find("h1", class_="pdp-title")
        name      = name_tag.get_text(strip=True) if name_tag else "Myntra Product"

        price = 0.0
        # Discounted selling price
        price_tag = soup.select_one("span.pdp-price strong")
        if price_tag:
            price = clean_price(price_tag.get_text())
        # Fallback: MRP
        if not price:
            mrp_tag = soup.select_one("span.pdp-mrp s") or soup.select_one(".pdp-price")
            if mrp_tag:
                price = clean_price(mrp_tag.get_text())

        img_tag   = soup.select_one("img.pdp-image") or soup.select_one(".image-grid-image")
        image_url = img_tag["src"] if img_tag and img_tag.get("src") else ""

        if not price:
            raise ValueError(
                "Could not extract price from Myntra — "
                "the page may be JavaScript-rendered. Try the URL directly in a browser first."
            )
        return {"name": name, "price": price, "image_url": image_url, "website": "myntra"}

    except requests.RequestException as exc:
        raise RuntimeError(f"Network error scraping Myntra: {exc}") from exc


# ── Meesho scraper ────────────────────────────────────────────────────────────

def scrape_meesho(url: str) -> dict:
    """
    Scrape Meesho product page (Next.js app).

    Strategy (in order):
      1. JSON-LD structured data
      2. __NEXT_DATA__ JSON blob — keys: "price", "name", "productName"
      3. CSS selectors:
         - h1[class*="ProductTitle"]  : product title
         - h4[class*="PriceHeader"]   : selling price
    """
    try:
        time.sleep(random.uniform(1, 3))
        resp = requests.get(url, headers=get_headers(), timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")

        # 1. JSON-LD
        jld = _extract_jsonld(soup)
        if jld.get("price") and jld.get("name"):
            return {**jld, "website": "meesho"}

        # 2. __NEXT_DATA__
        nd = _extract_next_data(
            soup,
            price_keys=["price", "sellingPrice", "selling_price", "sp"],
            name_keys=["name", "productName", "title", "product_name"],
        )
        if nd.get("price") and nd.get("name"):
            return {**nd, "website": "meesho"}

        # 3. CSS selectors (Meesho uses styled-components with hashed class names,
        #    so we match on partial class names using a custom filter)
        def _find_by_partial(tag, cls_fragment):
            return soup.find(tag, class_=re.compile(cls_fragment, re.I))

        name_tag  = _find_by_partial("h1", "ProductTitle") or soup.find("h1")
        name      = name_tag.get_text(strip=True) if name_tag else "Meesho Product"

        price = 0.0
        price_tag = _find_by_partial("h4", "PriceHeader") or _find_by_partial("h5", "price")
        if price_tag:
            price = clean_price(price_tag.get_text())

        if not price:
            raise ValueError(
                "Could not extract price from Meesho — "
                "the page may require JavaScript rendering."
            )
        return {"name": name, "price": price, "image_url": "", "website": "meesho"}

    except requests.RequestException as exc:
        raise RuntimeError(f"Network error scraping Meesho: {exc}") from exc


# ── Ajio scraper ──────────────────────────────────────────────────────────────

def _ajio_fetch(url: str) -> bytes:
    """
    Fetch an Ajio URL using curl_cffi to impersonate Chrome's TLS fingerprint,
    which bypasses Cloudflare bot detection that blocks the standard `requests`
    library (which exposes a Python TLS signature).

    Falls back to plain requests if curl_cffi is unavailable.
    """
    import json as _json

    ajio_headers = {
        # Full Chrome 124 header set that Cloudflare expects
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "text/html,application/xhtml+xml,application/xml;"
                           "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer":         "https://www.ajio.com/",
        "Origin":          "https://www.ajio.com",
        "sec-ch-ua":       '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest":  "document",
        "sec-fetch-mode":  "navigate",
        "sec-fetch-site":  "same-origin",
        "sec-fetch-user":  "?1",
        "upgrade-insecure-requests": "1",
        "DNT": "1",
        "Connection": "keep-alive",
    }

    if CURL_CFFI_AVAILABLE:
        # impersonate="chrome124" makes curl_cffi mimic Chrome 124's TLS handshake
        resp = cffi_requests.get(
            url,
            headers=ajio_headers,
            impersonate="chrome124",
            timeout=20,
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp.content
    else:
        # Plain requests fallback (may still get 403 on some requests)
        resp = requests.get(url, headers=ajio_headers, timeout=20)
        resp.raise_for_status()
        return resp.content


def _ajio_product_code(url: str) -> str | None:
    """
    Extract the Ajio product code from a product URL.

    URL patterns:
      https://www.ajio.com/liboza-women-embroidered-short-kurta/p/703036922_olive
      https://www.ajio.com/some-brand-product-name/p/460190498_white#gmf

    The product code is the numeric part after /p/ before the underscore.
    """
    match = re.search(r'/p/(\d+)', url)
    return match.group(1) if match else None



def scrape_ajio(url: str) -> dict:
    """
    Scrape Ajio product page.

    Ajio uses Akamai Bot Manager — a JavaScript anti-bot system that sets a
    cryptographic _abck cookie which can't be generated without real JS execution.
    curl_cffi (Chrome TLS impersonation) is tried first because it sometimes
    works depending on the network/IP. If Akamai still blocks, a clear error
    is raised directing the user to manual entry.

    Strategy:
      1. curl_cffi Session: homepage visit (cookie acquisition) then
         internal JSON API  GET /api/p/{product_code}
      2. curl_cffi Session: full HTML page + JSON-LD / CSS fallback
      3. Clear RuntimeError with manual-entry instructions
    """
    import json as _json

    clean_url    = url.split("#")[0]
    product_code = _ajio_product_code(clean_url)
    chrome_ua    = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    time.sleep(random.uniform(1, 2))

    if CURL_CFFI_AVAILABLE and product_code:
        try:
            session = cffi_requests.Session(impersonate="chrome124")

            # Acquire Akamai cookies from homepage
            session.get(
                "https://www.ajio.com/",
                headers={"User-Agent": chrome_ua,
                         "Accept": "text/html,*/*",
                         "Accept-Language": "en-IN,en;q=0.9"},
                timeout=15,
            )
            time.sleep(random.uniform(1.5, 2.5))

            # Try internal JSON API
            api_resp = session.get(
                f"https://www.ajio.com/api/p/{product_code}",
                headers={"User-Agent": chrome_ua,
                         "Accept": "application/json,*/*",
                         "Referer": clean_url,
                         "sec-fetch-dest": "empty",
                         "sec-fetch-mode": "cors",
                         "sec-fetch-site": "same-origin"},
                timeout=15,
            )
            if api_resp.status_code == 200:
                data      = api_resp.json()
                name      = (data.get("name") or data.get("productName") or "Ajio Product")
                price     = 0.0
                price_obj = data.get("price") or {}
                if isinstance(price_obj, dict):
                    price = clean_price(str(
                        price_obj.get("value") or price_obj.get("formattedValue") or 0
                    ))
                elif isinstance(price_obj, (int, float, str)):
                    price = clean_price(str(price_obj))
                if not price:
                    base = data.get("baseOptions") or []
                    if base:
                        price = clean_price(str(
                            base[0].get("selected", {}).get("priceData", {}).get("value", 0)
                        ))
                images    = data.get("images") or []
                image_url = ""
                if images:
                    raw = images[0].get("url", "")
                    image_url = ("https://assets.ajio.com" + raw
                                 if raw and not raw.startswith("http") else raw)
                if price and name:
                    logger.info("Ajio API success: %s @ Rs.%.2f", name, price)
                    return {"name": name, "price": price,
                            "image_url": image_url, "website": "ajio"}

            # HTML fallback with session cookies
            page_resp = session.get(
                clean_url,
                headers={"User-Agent": chrome_ua,
                         "Accept": "text/html,*/*",
                         "Referer": "https://www.ajio.com/"},
                timeout=20,
            )
            if page_resp.status_code == 200:
                soup = BeautifulSoup(page_resp.content, "html.parser")
                jld  = _extract_jsonld(soup)
                if jld.get("price") and jld.get("name"):
                    return {**jld, "website": "ajio"}
                name_tag = (soup.find("h1", class_=re.compile(r"prod-name", re.I))
                            or soup.find("h1"))
                name  = name_tag.get_text(strip=True) if name_tag else "Ajio Product"
                price = 0.0
                for sel in [".prod-sp", ".prod-cp", "span.prod-sp"]:
                    pt = soup.select_one(sel)
                    if pt:
                        price = clean_price(pt.get_text())
                        break
                if price:
                    return {"name": name, "price": price,
                            "image_url": "", "website": "ajio"}

        except Exception as exc:
            logger.warning("Ajio scrape attempt failed: %s", exc)

    # Akamai blocked — give actionable error
    raise RuntimeError(
        "Ajio blocked this request (Akamai Bot Manager requires a real browser). "
        "Use the Manual Entry option: enter the product name and current price "
        "directly, then update it with the Refresh button whenever the price changes."
    )


# ── Nykaa scraper ─────────────────────────────────────────────────────────────

def scrape_nykaa(url: str) -> dict:
    """
    Scrape Nykaa product page (Next.js).

    Strategy (in order):
      1. JSON-LD structured data — most reliable
      2. __NEXT_DATA__ JSON blob:
         keys searched: "price", "sellingPrice", "mrp", "name", "productName"
      3. CSS selectors:
         - h1[class*="product-title"] : product name
         - span[class*="price"]       : selling price
    """
    try:
        time.sleep(random.uniform(1, 3))
        resp = requests.get(url, headers=get_headers(), timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")

        # 1. JSON-LD
        jld = _extract_jsonld(soup)
        if jld.get("price") and jld.get("name"):
            return {**jld, "website": "nykaa"}

        # 2. __NEXT_DATA__
        nd = _extract_next_data(
            soup,
            price_keys=["price", "sellingPrice", "sp", "mrp", "discountedPrice"],
            name_keys=["name", "productName", "title", "displayName"],
        )
        if nd.get("price") and nd.get("name"):
            return {**nd, "website": "nykaa"}

        # 3. CSS selectors
        # Nykaa uses styled-components; class names are hashed but certain
        # fragments are stable enough for a best-effort fallback.
        name_tag = (
            soup.find("h1", attrs={"class": re.compile("product-title|productTitle", re.I)})
            or soup.find("h1")
        )
        name = name_tag.get_text(strip=True) if name_tag else "Nykaa Product"

        price = 0.0
        price_tag = soup.find("span", class_=re.compile(r"price", re.I))
        if price_tag:
            price = clean_price(price_tag.get_text())

        img_tag   = soup.find("img", class_=re.compile(r"product.*image|img.*product", re.I))
        image_url = img_tag["src"] if img_tag and img_tag.get("src") else ""

        if not price:
            raise ValueError(
                "Could not extract price from Nykaa — "
                "the page may require JavaScript rendering."
            )
        return {"name": name, "price": price, "image_url": image_url, "website": "nykaa"}

    except requests.RequestException as exc:
        raise RuntimeError(f"Network error scraping Nykaa: {exc}") from exc


# ── Pantaloons scraper ────────────────────────────────────────────────────────

def scrape_pantaloons(url: str) -> dict:
    """
    Scrape Pantaloons (pantaloons.com) product page.

    Pantaloons is part of the Aditya Birla Fashion group and runs a React SPA.

    Strategy (in order):
      1. JSON-LD structured data
      2. <script> inline containing "__REACT_QUERY_STATE__" or product JSON
      3. CSS selectors:
         - h1.pdp-title / h1[class*="title"]   : product name
         - span.price-value / .pdp-price        : selling price
    """
    try:
        time.sleep(random.uniform(1, 3))
        resp = requests.get(url, headers=get_headers(), timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")

        # 1. JSON-LD
        jld = _extract_jsonld(soup)
        if jld.get("price") and jld.get("name"):
            return {**jld, "website": "pantaloons"}

        # 2. Inline script data (React apps often inline critical data)
        import json as _json
        for script in soup.find_all("script"):
            text = script.string or ""
            # Try to find price directly via regex on the full script block
            price_m = re.search(r'"(?:sellingPrice|price|sp)"\s*:\s*"?(\d[\d,\.]*)"?', text)
            name_m  = re.search(r'"(?:name|productName|title)"\s*:\s*"([^"]{5,100})"', text)
            if price_m and name_m:
                return {
                    "name":      name_m.group(1),
                    "price":     clean_price(price_m.group(1)),
                    "image_url": "",
                    "website":   "pantaloons",
                }

        # 3. CSS selectors
        name_tag = (
            soup.find("h1", class_=re.compile(r"title|product-name|pdp", re.I))
            or soup.find("h1")
        )
        name = name_tag.get_text(strip=True) if name_tag else "Pantaloons Product"

        price = 0.0
        price_tag = (
            soup.select_one("span.price-value")
            or soup.find("span", class_=re.compile(r"price", re.I))
        )
        if price_tag:
            price = clean_price(price_tag.get_text())

        if not price:
            raise ValueError(
                "Could not extract price from Pantaloons — "
                "the page may require JavaScript rendering."
            )
        return {"name": name, "price": price, "image_url": "", "website": "pantaloons"}

    except requests.RequestException as exc:
        raise RuntimeError(f"Network error scraping Pantaloons: {exc}") from exc


# ── Router ────────────────────────────────────────────────────────────────────

SUPPORTED_SITES = ["amazon", "flipkart", "myntra", "meesho", "ajio", "nykaa", "pantaloons"]


def scrape_product(url: str) -> dict:
    """Route URL to the correct scraper based on detected website."""
    website = detect_website(url)
    scrapers = {
        "amazon":     scrape_amazon,
        "flipkart":   scrape_flipkart,
        "myntra":     scrape_myntra,
        "meesho":     scrape_meesho,
        "ajio":       scrape_ajio,
        "nykaa":      scrape_nykaa,
        "pantaloons": scrape_pantaloons,
    }
    if website in scrapers:
        return scrapers[website](url)
    raise ValueError(
        f"Unsupported website. Supported sites: {', '.join(SUPPORTED_SITES)}. Got: {url}"
    )


# ── Email alert ───────────────────────────────────────────────────────────────

def send_price_alert(email: str, product_name: str, url: str,
                     original_price: float, new_price: float) -> bool:
    """
    Send a price drop alert email via Gmail SMTP (TLS on port 587).
    Returns True on success, False on failure.
    """
    if not GMAIL_USER or not GMAIL_PASSWORD:
        logger.warning("Gmail credentials not configured — skipping email alert")
        return False

    savings = original_price - new_price
    savings_pct = (savings / original_price) * 100 if original_price else 0

    subject = f"🎉 Price Drop Alert: {product_name}"

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; background:#f4f4f4; padding:20px;">
      <div style="max-width:600px; margin:auto; background:#fff; border-radius:12px;
                  box-shadow:0 2px 8px rgba(0,0,0,.12); overflow:hidden;">
        <div style="background:linear-gradient(135deg,#667eea,#764ba2); padding:30px; text-align:center;">
          <h1 style="color:#fff; margin:0; font-size:24px;">🎉 Price Drop Alert!</h1>
        </div>
        <div style="padding:30px;">
          <h2 style="color:#333; margin-top:0;">{product_name}</h2>
          <table style="width:100%; border-collapse:collapse; margin:20px 0;">
            <tr style="background:#f8f9fa;">
              <td style="padding:12px; border:1px solid #dee2e6; color:#666;">Original Price</td>
              <td style="padding:12px; border:1px solid #dee2e6; font-weight:bold;
                         color:#dc3545; text-decoration:line-through;">₹{original_price:,.2f}</td>
            </tr>
            <tr>
              <td style="padding:12px; border:1px solid #dee2e6; color:#666;">New Price</td>
              <td style="padding:12px; border:1px solid #dee2e6; font-weight:bold;
                         color:#28a745; font-size:20px;">₹{new_price:,.2f}</td>
            </tr>
            <tr style="background:#fff3cd;">
              <td style="padding:12px; border:1px solid #dee2e6; color:#666;">You Save</td>
              <td style="padding:12px; border:1px solid #dee2e6; font-weight:bold; color:#856404;">
                ₹{savings:,.2f} ({savings_pct:.1f}% off)
              </td>
            </tr>
          </table>
          <div style="text-align:center; margin:30px 0;">
            <a href="{url}" style="background:linear-gradient(135deg,#667eea,#764ba2);
               color:#fff; padding:14px 32px; border-radius:8px; text-decoration:none;
               font-weight:bold; font-size:16px; display:inline-block;">
              🛒 Buy Now
            </a>
          </div>
          <p style="color:#999; font-size:12px; text-align:center; margin:0;">
            You're receiving this because you set up a price alert on Price Tracker.
          </p>
        </div>
      </div>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = email
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, email, msg.as_string())
        logger.info("Price alert email sent to %s for '%s'", email, product_name)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("Gmail authentication failed — check GMAIL_USER / GMAIL_PASSWORD in .env")
        return False
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", email, exc)
        return False


# ── Price check logic ─────────────────────────────────────────────────────────

def check_and_update_product(product_id: int):
    """
    Re-scrape a single product, update its price, log history,
    and fire email alerts if the threshold is met.
    """
    with get_db() as conn:
        product = conn.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        if not product:
            return

        try:
            data = scrape_product(product["url"])
            new_price = data["price"]
            now = datetime.utcnow().isoformat()

            # Log every price point for the sparkline / history chart
            conn.execute(
                "INSERT INTO price_history (product_id, price, checked_at) VALUES (?, ?, ?)",
                (product_id, new_price, now),
            )

            # Update the product row
            conn.execute(
                "UPDATE products SET current_price=?, last_checked=? WHERE id=?",
                (new_price, now, product_id),
            )

            # Check alerts for this product
            alerts = conn.execute(
                "SELECT * FROM alerts WHERE product_id=? AND is_active=1", (product_id,)
            ).fetchall()

            original = product["original_price"]
            drop_pct = ((original - new_price) / original * 100) if original else 0

            for alert in alerts:
                if drop_pct >= alert["threshold_pct"] and new_price < original:
                    sent = send_price_alert(
                        email=alert["email"],
                        product_name=product["name"],
                        url=product["url"],
                        original_price=original,
                        new_price=new_price,
                    )
                    if sent:
                        # Deactivate so we don't spam the same alert repeatedly
                        conn.execute(
                            "UPDATE alerts SET is_active=0 WHERE id=?", (alert["id"],)
                        )

            logger.info(
                "Updated '%s': ₹%.2f → ₹%.2f (drop %.1f%%)",
                product["name"], original, new_price, drop_pct,
            )

        except Exception as exc:
            logger.error("Error checking product id=%d: %s", product_id, exc)


def check_all_products():
    """Scheduled job: iterate every tracked product and update prices."""
    logger.info("Starting scheduled price check for all products...")
    with get_db() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM products").fetchall()]
    for pid in ids:
        check_and_update_product(pid)
    logger.info("Scheduled price check complete. Checked %d products.", len(ids))


# ── APScheduler setup ─────────────────────────────────────────────────────────
# Uses BackgroundScheduler so it runs in a daemon thread alongside Flask.
# IntervalTrigger fires every CHECK_INTERVAL_HOURS hours.
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(
    func=check_all_products,
    trigger=IntervalTrigger(hours=CHECK_INTERVAL_HOURS),
    id="price_check_job",
    name="Periodic price checker",
    replace_existing=True,
    misfire_grace_time=300,  # Allow up to 5 min delay before skipping
)


# ── REST API endpoints ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the single-page frontend."""
    return render_template("index.html")


@app.route("/api/add-product-manual", methods=["POST"])
def add_product_manual():
    """
    Add a product with a manually-entered name and price (no scraping).
    Used for sites like Ajio that block automated requests.

    Body (JSON): { url, name, price, email, threshold_pct }
    """
    data          = request.get_json(silent=True) or {}
    url           = (data.get("url") or "").strip()
    name          = (data.get("name") or "").strip()
    price_raw     = data.get("price", 0)
    email         = (data.get("email") or "").strip()
    threshold_pct = float(data.get("threshold_pct", 5))

    if not url:
        return jsonify({"error": "Product URL is required"}), 400
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid URL"}), 400
    if not name:
        return jsonify({"error": "Product name is required for manual entry"}), 400
    if not email or "@" not in email:
        return jsonify({"error": "Valid email address is required"}), 400

    try:
        price = float(price_raw)
        if price < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Price must be a non-negative number"}), 400

    website = detect_website(url)
    now     = datetime.utcnow().isoformat()

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM products WHERE url=?", (url,)
        ).fetchone()
        if existing:
            dup = conn.execute(
                "SELECT id FROM alerts WHERE product_id=? AND email=?",
                (existing["id"], email),
            ).fetchone()
            if dup:
                return jsonify({"error": "Already tracking this product with this email"}), 409
            conn.execute(
                "INSERT INTO alerts (product_id, email, threshold_pct, created_at) VALUES (?,?,?,?)",
                (existing["id"], email, threshold_pct, now),
            )
            return jsonify({"message": "Alert added to existing product",
                            "product_id": existing["id"]}), 200

        cursor = conn.execute(
            """INSERT INTO products
               (url, name, website, original_price, current_price, image_url, added_at, last_checked)
               VALUES (?,?,?,?,?,?,?,?)""",
            (url, name, website, price, price, "", now, now),
        )
        product_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO price_history (product_id, price, checked_at) VALUES (?,?,?)",
            (product_id, price, now),
        )
        conn.execute(
            "INSERT INTO alerts (product_id, email, threshold_pct, created_at) VALUES (?,?,?,?)",
            (product_id, email, threshold_pct, now),
        )

    return jsonify({
        "message": "Product added via manual entry",
        "product": {"id": product_id, "name": name,
                    "price": price, "website": website},
    }), 201


@app.route("/api/update-price-manual", methods=["POST"])
def update_price_manual():
    """
    Manually update the current price of a product (for bot-blocked sites).
    Body (JSON): { product_id, price }
    """
    data       = request.get_json(silent=True) or {}
    product_id = data.get("product_id")
    price_raw  = data.get("price")

    if not product_id:
        return jsonify({"error": "product_id is required"}), 400
    try:
        price = float(price_raw)
        if price < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "price must be a non-negative number"}), 400

    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        product = conn.execute(
            "SELECT * FROM products WHERE id=?", (product_id,)
        ).fetchone()
        if not product:
            return jsonify({"error": "Product not found"}), 404

        conn.execute(
            "UPDATE products SET current_price=?, last_checked=? WHERE id=?",
            (price, now, product_id),
        )
        conn.execute(
            "INSERT INTO price_history (product_id, price, checked_at) VALUES (?,?,?)",
            (product_id, price, now),
        )

        # Check alerts
        alerts = conn.execute(
            "SELECT * FROM alerts WHERE product_id=? AND is_active=1", (product_id,)
        ).fetchall()
        original  = product["original_price"]
        drop_pct  = ((original - price) / original * 100) if original else 0
        for alert in alerts:
            if drop_pct >= alert["threshold_pct"] and price < original:
                sent = send_price_alert(
                    email=alert["email"],
                    product_name=product["name"],
                    url=product["url"],
                    original_price=original,
                    new_price=price,
                )
                if sent:
                    conn.execute("UPDATE alerts SET is_active=0 WHERE id=?", (alert["id"],))

    return jsonify({"message": "Price updated", "product_id": product_id, "price": price})


@app.route("/api/add-product", methods=["POST"])
def add_product():
    """
    Add a new product to track.
    Body (JSON): { url, email, threshold_pct }
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    email = (data.get("email") or "").strip()
    threshold_pct = float(data.get("threshold_pct", 5))

    # ── Input validation ──────────────────────────────────────────────────────
    if not url:
        return jsonify({"error": "Product URL is required"}), 400
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid URL — must start with http:// or https://"}), 400
    if not email or "@" not in email:
        return jsonify({"error": "Valid email address is required"}), 400
    if not (1 <= threshold_pct <= 90):
        return jsonify({"error": "Threshold must be between 1% and 90%"}), 400
    if detect_website(url) == "other":
        return jsonify({"error": f"Unsupported website. Supported: {', '.join(SUPPORTED_SITES)}"}), 400

    # ── Duplicate check ───────────────────────────────────────────────────────
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM products WHERE url=?", (url,)
        ).fetchone()
        if existing:
            # Check if this email already has an alert on the same product
            dup_alert = conn.execute(
                "SELECT id FROM alerts WHERE product_id=? AND email=?",
                (existing["id"], email),
            ).fetchone()
            if dup_alert:
                return jsonify({"error": "You're already tracking this product with this email"}), 409
            # Different email — just add a new alert to existing product
            conn.execute(
                "INSERT INTO alerts (product_id, email, threshold_pct, created_at) VALUES (?,?,?,?)",
                (existing["id"], email, threshold_pct, datetime.utcnow().isoformat()),
            )
            return jsonify({"message": "Alert added to existing product", "product_id": existing["id"]}), 200

    # ── Scrape initial price ──────────────────────────────────────────────────
    try:
        scraped = scrape_product(url)
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 422

    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        # Insert product
        cursor = conn.execute(
            """INSERT INTO products
               (url, name, website, original_price, current_price, image_url, added_at, last_checked)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                url,
                scraped["name"],
                scraped["website"],
                scraped["price"],
                scraped["price"],
                scraped.get("image_url", ""),
                now,
                now,
            ),
        )
        product_id = cursor.lastrowid

        # Log first price point
        conn.execute(
            "INSERT INTO price_history (product_id, price, checked_at) VALUES (?,?,?)",
            (product_id, scraped["price"], now),
        )

        # Create alert
        conn.execute(
            "INSERT INTO alerts (product_id, email, threshold_pct, created_at) VALUES (?,?,?,?)",
            (product_id, email, threshold_pct, now),
        )

    return jsonify({
        "message": "Product added successfully",
        "product": {
            "id": product_id,
            "name": scraped["name"],
            "price": scraped["price"],
            "website": scraped["website"],
        },
    }), 201


@app.route("/api/products", methods=["GET"])
def get_products():
    """Return all tracked products with their active alert count."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT p.*,
                   COUNT(a.id) AS alert_count
            FROM products p
            LEFT JOIN alerts a ON a.product_id = p.id AND a.is_active = 1
            GROUP BY p.id
            ORDER BY p.added_at DESC
        """).fetchall()

        products = []
        for row in rows:
            p = dict(row)
            # Fetch last 10 price points for the sparkline
            history = conn.execute(
                """SELECT price FROM price_history
                   WHERE product_id=?
                   ORDER BY checked_at DESC LIMIT 10""",
                (p["id"],),
            ).fetchall()
            # Reverse so oldest → newest for chart rendering
            p["sparkline"] = [h["price"] for h in reversed(history)]
            products.append(p)

    return jsonify(products)


@app.route("/api/product-history/<int:product_id>", methods=["GET"])
def get_product_history(product_id: int):
    """Return full price history for a single product."""
    with get_db() as conn:
        product = conn.execute(
            "SELECT * FROM products WHERE id=?", (product_id,)
        ).fetchone()
        if not product:
            return jsonify({"error": "Product not found"}), 404

        history = conn.execute(
            """SELECT price, checked_at FROM price_history
               WHERE product_id=?
               ORDER BY checked_at ASC""",
            (product_id,),
        ).fetchall()

    return jsonify({
        "product": dict(product),
        "history": [dict(h) for h in history],
    })


@app.route("/api/delete-product/<int:product_id>", methods=["DELETE"])
def delete_product(product_id: int):
    """Delete a product and all related history/alerts (CASCADE)."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM products WHERE id=?", (product_id,)
        ).fetchone()
        if not existing:
            return jsonify({"error": "Product not found"}), 404

        # PRAGMA foreign_keys must be set before the DELETE on each connection
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM products WHERE id=?", (product_id,))
        # Also clean up orphaned rows in case FK cascade is not enforced
        conn.execute("DELETE FROM price_history WHERE product_id=?", (product_id,))
        conn.execute("DELETE FROM alerts WHERE product_id=?", (product_id,))

    return jsonify({"message": "Product deleted successfully"})


@app.route("/api/stats", methods=["GET"])
def get_stats():
    """Return aggregate stats for the dashboard header."""
    with get_db() as conn:
        total_products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        active_alerts = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE is_active=1"
        ).fetchone()[0]

        # Total savings = sum of (original_price - current_price) where positive
        savings_row = conn.execute(
            """SELECT COALESCE(SUM(original_price - current_price), 0) AS total
               FROM products
               WHERE current_price < original_price"""
        ).fetchone()
        total_savings = round(savings_row["total"], 2) if savings_row else 0.0

    return jsonify({
        "total_products": total_products,
        "active_alerts": active_alerts,
        "total_savings": total_savings,
    })


@app.route("/api/manual-check", methods=["POST"])
def manual_check():
    """
    Trigger an immediate price check for all products (or a single product).
    Body (JSON, optional): { product_id: <int> }
    """
    data = request.get_json(silent=True) or {}
    product_id = data.get("product_id")

    if product_id:
        check_and_update_product(int(product_id))
        return jsonify({"message": f"Price updated for product {product_id}"})

    # Check all products synchronously
    with get_db() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM products").fetchall()]

    for pid in ids:
        check_and_update_product(pid)

    return jsonify({"message": f"Checked {len(ids)} product(s)", "count": len(ids)})


@app.route("/api/scheduler-status", methods=["GET"])
def scheduler_status():
    """Return the next scheduled run time for the price-check job."""
    job = scheduler.get_job("price_check_job")
    next_run = None
    if job and job.next_run_time:
        next_run = job.next_run_time.isoformat()
    return jsonify({
        "running":        scheduler.running,
        "interval_hours": CHECK_INTERVAL_HOURS,
        "next_run":       next_run,
    })


@app.route("/api/alerts/<int:product_id>", methods=["GET"])
def get_alerts(product_id: int):
    """Return all alerts for a product (active and historical)."""
    with get_db() as conn:
        product = conn.execute(
            "SELECT id, name FROM products WHERE id=?", (product_id,)
        ).fetchone()
        if not product:
            return jsonify({"error": "Product not found"}), 404

        alerts = conn.execute(
            "SELECT * FROM alerts WHERE product_id=? ORDER BY created_at DESC",
            (product_id,),
        ).fetchall()

    return jsonify({
        "product_id":   product_id,
        "product_name": product["name"],
        "alerts":       [dict(a) for a in alerts],
    })


@app.route("/api/alerts/<int:alert_id>/reactivate", methods=["POST"])
def reactivate_alert(alert_id: int):
    """Re-enable a previously fired alert so it can trigger again on the next drop."""
    with get_db() as conn:
        alert = conn.execute(
            "SELECT id FROM alerts WHERE id=?", (alert_id,)
        ).fetchone()
        if not alert:
            return jsonify({"error": "Alert not found"}), 404
        conn.execute("UPDATE alerts SET is_active=1 WHERE id=?", (alert_id,))

    return jsonify({"message": "Alert reactivated"})


@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
def delete_alert(alert_id: int):
    """Delete a single alert without affecting the product or other alerts."""
    with get_db() as conn:
        alert = conn.execute(
            "SELECT id FROM alerts WHERE id=?", (alert_id,)
        ).fetchone()
        if not alert:
            return jsonify({"error": "Alert not found"}), 404
        conn.execute("DELETE FROM alerts WHERE id=?", (alert_id,))

    return jsonify({"message": "Alert deleted"})


# ── App entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    scheduler.start()
    logger.info(
        "APScheduler started — price checks every %d hour(s)", CHECK_INTERVAL_HOURS
    )
    # debug=False in production; use a proper WSGI server (gunicorn/waitress) instead
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
