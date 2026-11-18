from app.database import AppwriteError, AppwriteStore, appwrite_configured


def appwrite_health() -> dict:
    if not appwrite_configured():
        return {"configured": False, "healthy": False}
    try:
        AppwriteStore().request(
            "GET", "/databases/aika/collections/aika_state"
        )
        return {"configured": True, "healthy": True, "database": "aika"}
    except AppwriteError as exc:
        reason = "permission_denied" if exc.status == 401 else "collection_not_ready"
        return {"configured": True, "healthy": False, "database": "aika", "reason": reason}
