import os
import json
import time
import asyncio
import base64
import aiohttp
import logging
from datetime import datetime
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO)

# Load environment variables
load_dotenv()

WEBHOOK_URL    = os.getenv("WEBHOOK_URL")
CLIENT_ID      = os.getenv("EPIC_CLIENT_ID")
CLIENT_SECRET  = os.getenv("EPIC_CLIENT_SECRET")

# API Endpoints
OAUTH_URL            = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/token"
FORTNITE_PACKS_URL   = "https://catalog-public-service-prod06.ol.epicgames.com/catalog/api/shared/namespace/fn/offers?lang=en&country=US&count=25"

# File Storage
JSON_FILE     = "old_packs.json"
TOKEN_FILE    = "token_data.json"

# Configurable check interval (default: 60 seconds)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))

# Discord role ID to ping
ROLE_ID       = os.getenv("ROLE_ID", "YOUR_ROLE_ID")

async def fetch_new_token(session):
    """Fetches a new OAuth token from Epic Games."""
    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}
    data = {"grant_type": "client_credentials"}
    async with session.post(OAUTH_URL, headers=headers, data=data) as response:
        if response.status == 200:
            token_data = await response.json()
            token_data["expires_at"] = time.time() + token_data["expires_in"]
            with open(TOKEN_FILE, "w") as f:
                json.dump(token_data, f, indent=4)
            return token_data["access_token"]
        else:
            logging.error(f"Failed to get token: {response.status}")
            return None

async def get_token(session):
    """Retrieves a valid OAuth token, refreshing if necessary."""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            try:
                token_data = json.load(f)
                if time.time() < token_data["expires_at"]:
                    return token_data["access_token"]
            except json.JSONDecodeError:
                logging.error("Corrupted token file, fetching a new token.")
    return await fetch_new_token(session)

def convert_timestamp(ts):
    """Converts Epic Games timestamps to Discord timestamp format."""
    if not ts:
        return "N/A"
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        try:
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return "N/A"
    return f"<t:{int(dt.timestamp())}:F>"

async def send_notification(session, name, price, description,
                             wide_image_url, tall_image_url,
                             activation_date, expiration_date,
                             content=None):
    """Sends a Discord notification."""
    logging.info(f"Sending notification for: {name}")
    embed = {
        "title": "New Bundle Modification Update",
        "fields": [
            {"name": "Name", "value": name, "inline": False},
            {"name": "Price", "value": f"```{price}```", "inline": False},
            {"name": "Description", "value": f"```{description}```", "inline": False},
            {"name": "Activation Date", "value": f"**{activation_date}**", "inline": False},
            {"name": "Expiration Date", "value": f"**{expiration_date}**", "inline": False}
        ],
        "image": {"url": wide_image_url} if wide_image_url.startswith(("http://", "https://")) else None,
        "thumbnail": {"url": tall_image_url} if tall_image_url.startswith(("http://", "https://")) else None
    }
    payload = {"embeds": [embed]}
    if content:
        payload["content"] = content

    async with session.post(WEBHOOK_URL, json=payload) as response:
        if response.status == 204:
            logging.info("Notification sent successfully.")
        else:
            logging.error(f"Failed to send notification: {response.status}")

async def check_for_new_packs(session):
    """Checks for new or updated Fortnite packs."""
    logging.info("Checking for new Fortnite packs...")
    token = await get_token(session)
    if not token:
        logging.error("No valid token available. Skipping this check.")
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with session.get(FORTNITE_PACKS_URL, headers=headers) as response:
        if response.status == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            logging.warning(f"Rate limited. Retrying in {retry_after} seconds.")
            await asyncio.sleep(retry_after)
            return
        if response.status != 200:
            logging.error(f"Failed to fetch packs: {response.status}")
            return
        try:
            data = await response.json()
        except aiohttp.ContentTypeError:
            logging.error("Invalid JSON response received.")
            return

    items = data.get("elements", [])
    if not items:
        logging.info("No pack data available.")
        return

    # Load previous data
    old_data = {}
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE, "r") as f:
            try:
                old_data = json.load(f)
            except json.JSONDecodeError:
                logging.warning("Corrupted old_packs.json file, resetting.")

    new_data = {}
    changes_detected = False
    changes = []

    for pack in items:
        pack_name = pack.get("title", "Unknown Name")
        pack_description = pack.get("description", "No Description")
        price_cents = pack.get("currentPrice", 0)
        currency = pack.get("currencyCode", "Unknown")
        pack_price = f"${price_cents / 100:.2f} {currency}" if currency != "Unknown" else "Unknown Price"
        images = pack.get("keyImages", [])
        tall_image_url = next((img["url"] for img in images if img["type"] == "OfferImageTall"), "")
        wide_image_url = next((img["url"] for img in images if img["type"] == "OfferImageWide"), "")
        activation_date = convert_timestamp(pack.get("effectiveDate", ""))
        expiration_date = convert_timestamp(pack.get("expiryDate", ""))

        record = {
            "price":          pack_price,
            "description":    pack_description,
            "imageUrl":       wide_image_url,
            "activationDate": activation_date,
            "expirationDate": expiration_date
        }

        if old_data.get(pack_name) != record:
            logging.info(f"Change detected: {pack_name}")
            changes.append((pack_name, pack_price, pack_description,
                            wide_image_url, tall_image_url,
                            activation_date, expiration_date))
            changes_detected = True

        new_data[pack_name] = record

    if changes_detected:
        # Always ping once (even if only one change)
        for idx, args in enumerate(changes):
            if idx == 0:
                await send_notification(session, *args, content=f"<@&{ROLE_ID}>")
            else:
                await send_notification(session, *args)
        with open(JSON_FILE, "w") as f:
            json.dump(new_data, f, indent=4)
        logging.info("Changes detected, updating stored data.")
    else:
        logging.info("No changes detected.")

async def main():
    async with aiohttp.ClientSession() as session:
        while True:
            await check_for_new_packs(session)
            await asyncio.sleep(CHECK_INTERVAL)

# Start the script
asyncio.run(main())