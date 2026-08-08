import json
import re
import smtplib
import sqlite3
from datetime import datetime, timezone
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup

with open("config.json", "r") as f:
    config = json.load(f)

DB = "prices.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BargainWatcher/1.0)"
}


def setup_database():
    db = sqlite3.connect(DB)

    db.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY,
            name TEXT,
            price REAL,
            url TEXT,
            checked_at TEXT
        )
    """)

    db.commit()
    return db


def get_price(text):
    prices = re.findall(r'\$\s*(\d+(?:\.\d{1,2})?)', text)

    if not prices:
        return None

    prices = [float(p) for p in prices]

    # Ignore obviously unrelated numbers
    prices = [p for p in prices if 20 <= p <= 2000]

    return min(prices) if prices else None


def search_page(url):
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=20
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        results = []

        for item in soup.find_all(
            ["h1", "h2", "h3", "h4", "a"]
        ):

            name = item.get_text(
                " ",
                strip=True
            )

            if not name:
                continue

            surrounding_text = item.parent.get_text(
                " ",
                strip=True
            )

            price = get_price(
                surrounding_text
            )

            if price:
                results.append({
                    "name": name,
                    "price": price,
                    "url": url
                })

        return results

    except Exception as e:
        print(
            f"Could not check {url}: {e}"
        )

        return []


def is_target_product(name):

    name = name.lower()

    required = config[
        "target_product"
    ]["required_terms"]

    excluded = config[
        "target_product"
    ]["excluded_terms"]

    for term in required:
        if term not in name:
            return False

    for term in excluded:
        if term in name:
            return False

    return True


def send_email(subject, message):

    smtp_server = "smtp.gmail.com"
    smtp_port = 587

    sender = "YOUR_EMAIL"
    password = "YOUR_APP_PASSWORD"
    recipient = "YOUR_EMAIL"

    email = EmailMessage()

    email["Subject"] = subject
    email["From"] = sender
    email["To"] = recipient

    email.set_content(message)

    with smtplib.SMTP(
        smtp_server,
        smtp_port
    ) as server:

        server.starttls()

        server.login(
            sender,
            password
        )

        server.send_message(
            email
        )


def main():

    db = setup_database()

    # Initial NZ cycling retailers.
    sites = [
        "https://www.giantwellington.co.nz/",
        "https://www.bikeaholic.co.nz/",
        "https://www.pushbikes.co.nz/"
    ]

    target = config[
        "target_product"
    ]

    target_price = target[
        "target_price"
    ]

    strong_price = target[
        "strong_price"
    ]

    for site in sites:

        results = search_page(site)

        for result in results:

            if not is_target_product(
                result["name"]
            ):
                continue

            price = result["price"]

            db.execute(
                """
                INSERT INTO prices
                (name, price, url, checked_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    result["name"],
                    price,
                    result["url"],
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                )
            )

            db.commit()

            if price <= strong_price:

                send_email(
                    "🚨 SRAM GX Eagle bargain",
                    f"""
SRAM GX Eagle XG-1275 10-52T

PRICE: ${price:.2f}

This is below your strong-buy
threshold of ${strong_price}.

Link:
{result["url"]}
"""
                )

            elif price <= target_price:

                send_email(
                    "SRAM GX Eagle good deal",
                    f"""
SRAM GX Eagle XG-1275 10-52T

PRICE: ${price:.2f}

This is below your target price
of ${target_price}.

Link:
{result["url"]}
"""
                )

    db.close()


if __name__ == "__main__":
    main()
