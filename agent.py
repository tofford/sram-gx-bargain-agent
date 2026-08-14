import csv
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CONFIG_FILE = Path("config.json")
HISTORY_FILE = Path("price_history.csv")
STATE_FILE = Path("alert_state.json")

USER_AGENT = (
    "Mozilla/5.0 (compatible; SRAM-XG1275-Bargain-Watcher/1.0; "
    "+https://github.com/)"
)

EXCLUDED_TERMS = ("t-type", "t type", "transmission")
REQUIRED_TERMS = ("xg-1275", "10-52")


def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path, data):
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)


def clean_price(value):
    if value is None:
        return None
    match = re.search(r"(\d[\d,]*(?:\.\d{1,2})?)", str(value))
    return float(match.group(1).replace(",", "")) if match else None


def page_matches_product(soup):
    """
    Confirm that the page is the SRAM GX Eagle XG-1275
    10-52T cassette without rejecting the page merely
    because the retailer mentions other SRAM products.
    """

    title = ""
    if soup.title:
        title = soup.title.get_text(" ", strip=True)

    h1 = soup.find("h1")
    if h1:
        title += " " + h1.get_text(" ", strip=True)

    title = title.lower()

    # XG-1275 must appear somewhere in the page.
    page_text = soup.get_text(" ", strip=True).lower()

    if "xg-1275" not in page_text:
        return False, "XG-1275 not found"

    # 10-52 can appear anywhere on the product page.
    if (
        "10-52" not in page_text
        and "10–52" not in page_text
        and "10 / 52" not in page_text
    ):
        return False, "10-52 not found"

    # Only exclude T-Type if the actual title identifies it.
    if any(term in title for term in EXCLUDED_TERMS):
        return False, "Product title indicates T-Type/Transmission"

    return True, None
    
def extract_price(soup, selector=None):
    """Extract a product price from common ecommerce page formats."""

    # 1. Custom selector from config
    if selector:
        element = soup.select_one(selector)
        if element:
            price = clean_price(
                element.get("content")
                or element.get_text(" ", strip=True)
            )
            if price is not None:
                return price

    # 2. Common meta / HTML price fields
    candidates = [
        ('meta[property="product:price:amount"]', "content"),
        ('meta[property="og:price:amount"]', "content"),
        ('meta[itemprop="price"]', "content"),
        ('meta[name="twitter:data1"]', "content"),
        ('[itemprop="price"]', "content"),
        ('.price', None),
        ('.product-price', None),
        ('.price-item', None),
        ('.money', None),
    ]

    for css_selector, attribute in candidates:
        for element in soup.select(css_selector):
            raw = (
                element.get(attribute)
                if attribute
                else element.get_text(" ", strip=True)
            )

            price = clean_price(raw)

            # Ignore obviously bogus prices.
            if price is not None and 1 <= price <= 10000:
                return price

    # 3. JSON-LD structured product data
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            raw_json = script.string or script.get_text()
            data = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            continue

        def inspect_item(item):
            if not isinstance(item, dict):
                return None

            # Direct price
            for key in ("price", "lowPrice"):
                price = clean_price(item.get(key))
                if price is not None and 1 <= price <= 10000:
                    return price

            # Offers
            offers = item.get("offers", [])

            if isinstance(offers, dict):
                offers = [offers]

            if isinstance(offers, list):
                for offer in offers:
                    if isinstance(offer, dict):
                        for key in ("price", "lowPrice"):
                            price = clean_price(offer.get(key))
                            if price is not None and 1 <= price <= 10000:
                                return price

            # Nested graph
            graph = item.get("@graph")
            if isinstance(graph, list):
                for graph_item in graph:
                    price = inspect_item(graph_item)
                    if price is not None:
                        return price

            return None

        items = data if isinstance(data, list) else [data]

        for item in items:
            price = inspect_item(item)
            if price is not None:
                return price

    # 4. Shopify product JSON embedded in the page
    for script in soup.select("script"):
        text = script.string or script.get_text()

        if not text:
            continue

        if "product" not in text.lower() or "price" not in text.lower():
            continue

        # Look for common Shopify price representations.
        patterns = [
            r'"price"\s*:\s*"(\d+(?:\.\d+)?)"',
            r'"price"\s*:\s*(\d+(?:\.\d+)?)',
            r'"price"\s*:\s*"?(\d{3,6})"?',
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                price = clean_price(match.group(1))

                if price is not None:
                    # Shopify sometimes stores prices in cents.
                    if price > 10000:
                        price /= 100.0

                    if 1 <= price <= 10000:
                        return price

    return None
def check_source(source):
    name = source["name"]
    url = source["url"]

    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        return {"name": name, "url": url, "price": None, "status": f"Request failed: {error}"}

    soup = BeautifulSoup(response.text, "html.parser")
    matches, reason = page_matches_product(soup)
    if not matches:
        return {"name": name, "url": url, "price": None, "status": reason}

    price = extract_price(soup, source.get("price_selector"))
    if price is None:
        return {"name": name, "url": url, "price": None, "status": "No price found"}

    return {"name": name, "url": url, "price": price, "status": "OK"}


def append_history(timestamp, result, currency):
    new_file = not HISTORY_FILE.exists()
    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        if new_file:
            writer.writerow(["checked_at_utc", "store", "price", "currency", "status", "url"])
        writer.writerow([
            timestamp,
            result["name"],
            result["price"] if result["price"] is not None else "",
            currency,
            result["status"],
            result["url"],
        ])


def send_alert(currency, threshold, bargains):
    gmail_address = os.getenv("GMAIL_ADDRESS")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    alert_to = os.getenv("ALERT_TO")

    missing = [
        name for name, value in {
            "GMAIL_ADDRESS": gmail_address,
            "GMAIL_APP_PASSWORD": gmail_password,
            "ALERT_TO": alert_to,
        }.items() if not value
    ]
    if missing:
        raise RuntimeError(f"Missing GitHub Actions secret(s): {', '.join(missing)}")

    bargain_lines = []
    for item in bargains:
        bargain_lines.extend([
            f"{item['name']}: {currency}{item['price']:.2f}",
            item["url"],
            "",
        ])

    body = (
        "A SRAM GX Eagle XG-1275 10-52T listing reached your alert price.\n\n"
        + "\n".join(bargain_lines)
        + f"\nAlert threshold: {currency}{threshold:.2f}\n"
        + "\nPlease confirm stock, currency, shipping, and final checkout price before buying."
    )

    message = EmailMessage()
    message["Subject"] = f"SRAM XG-1275 bargain: {currency}{threshold:.0f} alert"
    message["From"] = gmail_address
    message["To"] = alert_to
    message.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_address, gmail_password)
        smtp.send_message(message)


def main():
    config = load_json(CONFIG_FILE, {})

    print("========== CONFIG DEBUG ==========")
    print("Config file:", CONFIG_FILE.resolve())
    print("Config exists:", CONFIG_FILE.exists())
    print("Config contents:")
    print(json.dumps(config, indent=2))
    print("==================================")

    sources = config.get("sources", [])
    thresholds = sorted(
        config.get("alert_thresholds", [300, 275]),
        reverse=True
    )
    currency = config.get("currency_symbol", "$")

    if not sources:
        raise RuntimeError("No sources found in config.json")

    state = load_json(STATE_FILE, {"sent_alerts": {}})
    timestamp = datetime.now(timezone.utc).isoformat()

    results = [check_source(source) for source in sources]

    for result in results:
        append_history(timestamp, result, currency)
        print(f"{result['name']}: {result['price'] or '—'} ({result['status']})")

    for threshold in thresholds:
        bargains = [
            result for result in results
            if result["price"] is not None and result["price"] <= threshold
        ]

        active_keys = {f"{result['url']}|{threshold}" for result in bargains}

        # Allow a fresh alert if a product later rises above the threshold, then drops again.
        for key in list(state["sent_alerts"]):
            if key.endswith(f"|{threshold}") and key not in active_keys:
                del state["sent_alerts"][key]

        new_bargains = [
            result for result in bargains
            if f"{result['url']}|{threshold}" not in state["sent_alerts"]
        ]

        if new_bargains:
            send_alert(currency, threshold, new_bargains)
            for result in new_bargains:
                state["sent_alerts"][f"{result['url']}|{threshold}"] = timestamp

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Agent failed: {error}", file=sys.stderr)
        raise
