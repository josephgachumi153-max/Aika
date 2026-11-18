import os
import time

from app.database import AppwriteError, AppwriteStore


required_settings = ("APPWRITE_ENDPOINT", "APPWRITE_PROJECT_ID", "APPWRITE_API_KEY")
missing_settings = [key for key in required_settings if not os.environ.get(key)]
if missing_settings:
    raise SystemExit(f"Set these environment variables before setup: {', '.join(missing_settings)}")

store = AppwriteStore()
database_path = "/databases/aika"
try:
    store.request("GET", database_path)
except AppwriteError as exc:
    if exc.status != 404:
        raise SystemExit(f"Cannot access Appwrite database: {exc}") from exc
    store.request(
        "POST",
        "/databases",
        {"databaseId": "aika", "name": "Aika"},
    )

collection_path = "/databases/aika/collections/aika_state"

try:
    store.request("GET", collection_path)
except AppwriteError as exc:
    if exc.status != 404:
        raise SystemExit(f"Cannot access Appwrite collection: {exc}") from exc
    store.request(
        "POST",
        "/databases/aika/collections",
        {
            "collectionId": "aika_state",
            "name": "Aika database state",
            "documentSecurity": False,
            "permissions": [],
        },
    )

for kind, key, definition in (
    ("integer", "version", {"key": "version", "required": True, "min": 1, "max": 2147483647}),
    ("string", "payload", {"key": "payload", "required": True, "size": 1_000_000}),
):
    attribute_path = f"{collection_path}/attributes/{kind}/{key}"
    try:
        store.request("GET", attribute_path)
    except AppwriteError as exc:
        if exc.status != 404:
            raise SystemExit(f"Cannot access Appwrite attribute {key}: {exc}") from exc
        store.request("POST", f"{collection_path}/attributes/{kind}", definition)

    for _ in range(30):
        attribute = store.request("GET", attribute_path)
        if attribute.get("status") == "available":
            break
        time.sleep(2)
    else:
        raise SystemExit(f"Appwrite attribute {key} did not become available")

index_path = f"{collection_path}/indexes/version_idx"
try:
    store.request("GET", index_path)
except AppwriteError as exc:
    if exc.status != 404:
        raise SystemExit(f"Cannot access Appwrite index: {exc}") from exc
    store.request(
        "POST",
        f"{collection_path}/indexes",
        {
            "key": "version_idx",
            "type": "key",
            "attributes": ["version"],
            "orders": ["DESC"],
        },
    )

print("Appwrite collection aika_state is ready.")
