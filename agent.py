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
    text = soup.get_text(" ", strip=True).lower()

    if any(term in text for term in EXCLUDED_TERMS):
        return False, "Excluded: page mentions T-Type/Transmission"

    if not all(term in text for term in REQUIRED_TERMS):
        return False, "Product match not confirmed (needs XG-1275 and 10-52)"

    return True, None


def extract_price(soup, selector=None):
    # A custom selector in config takes priority.
    if selector:
        element = soup.select_one(selector)
        if element:
            price = clean_price(element.get_text(" ", strip=True))
            if price is not None:
                return price

    # Common structured price fields used by stores.
    candidates = [
        ('meta[property="product:price:amount"]', "content"),
        ('meta[itemprop="price"]', "content"),
        ('meta[name="twitter:data1"]', "content"),
        ('[itemprop="price"]', "content"),
    ]

    for css_selector, attribute in candidates:
        element = soup.select_one(css_selector)
        if element:
            raw = element.get(attribute) or element.get_text(" ", strip=True)
            price = clean_price(raw)
            if price is not None:
                return price

    # JSON-LD product data, commonly the most reliable source.
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except json.JSONDecodeError:
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            offers = item.get("offers", [])
            if isinstance(offers, dict):
                offers = [offers]

            for offer in offers:
                if isinstance(offer, dict):
                    price = clean_price(offer.get("price") or offer.get("lowPrice"))
                    if price is not None:
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
