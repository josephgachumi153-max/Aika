"""End-to-end smoke test using a throwaway database."""
import os, tempfile
os.environ["AIKA_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
from datetime import date, timedelta
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as c:
    def h(t): return {"Authorization": f"Bearer {t}"}
    ok = lambda r, code=200: (r.status_code == code) or (_ for _ in ()).throw(AssertionError(f"{r.status_code} {r.text}"))

    # admin login
    r = c.post("/api/login", json={"email": "admin@aika.local", "password": "admin123"}); ok(r)
    admin = r.json()["token"]
    # bad login

    # hotel registers
    r = c.post("/api/register", json={"name": "Jane", "business_name": "Brackenhurst", "email": "J@b.com",
                                      "phone": "0712345678", "address": "Limuru", "password": "secret1"}); ok(r)
    hotel = r.json()["token"]
    ok(c.post("/api/register", json={"name": "Jane", "business_name": "Dup", "email": "j@b.com",
                                     "phone": "0712345678", "password": "secret1"}), 400)

    # permissions
    ok(c.get("/api/farmers", headers=h(hotel)), 403)
    ok(c.get("/api/products"), 401)
    prods = c.get("/api/products", headers=h(hotel)).json()
    assert len(prods) == 6 and "stock" not in prods[0]

    # admin records farmer + purchase -> stock up
    fid = c.post("/api/farmers", headers=h(admin), json={"name": "Mama Wanjiru", "phone": "0700"}).json()["id"]
    byname = {p["name"]: p for p in prods}
    pid = byname["Dressed broiler (whole)"]["id"]
    breast = byname["Chicken breast"]["id"]
    ok(c.post("/api/purchases", headers=h(admin), json={"farmer_id": fid, "product_id": pid, "quantity": 50, "unit_cost": 380}))
    s = c.get("/api/reports/summary", headers=h(admin)).json()
    assert s["owed_to_farmers"] == 19000, s
    assert [p for p in s["stock"] if p["id"] == pid][0]["stock"] == 50

    # hotel orders
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    ok(c.post("/api/orders", headers=h(hotel), json={"items": [], "delivery_date": tomorrow}), 422)
    ok(c.post("/api/orders", headers=h(hotel), json={"items": [{"product_id": pid, "quantity": 5}], "delivery_date": "2020-01-01"}), 400)
    r = c.post("/api/orders", headers=h(hotel), json={"items": [{"product_id": pid, "quantity": 10}, {"product_id": breast, "quantity": 2}],
                                                      "delivery_date": tomorrow}); ok(r)
    oid, total = r.json()["id"], r.json()["total"]
    assert total == 10 * 480 + 2 * 700, total

    # hotel can't confirm, can see only own
    ok(c.post(f"/api/orders/{oid}/status", headers=h(hotel), json={"status": "confirmed"}), 403)
    r2 = c.post("/api/register", json={"name": "Bob", "business_name": "Other", "email": "bob@x.com", "phone": "0711111111", "password": "secret1"}).json()
    ok(c.get(f"/api/orders/{oid}/payments", headers=h(r2["token"])), 404)
    assert c.get("/api/orders", headers=h(r2["token"])).json() == []

    # admin flow: confirm -> deliver -> stock down -> pay
    ok(c.post(f"/api/orders/{oid}/status", headers=h(admin), json={"status": "confirmed"}))
    ok(c.post(f"/api/orders/{oid}/status", headers=h(admin), json={"status": "delivered"}))
    s = c.get("/api/reports/summary", headers=h(admin)).json()
    assert [p for p in s["stock"] if p["id"] == pid][0]["stock"] == 40
    assert s["receivable"] == total
    ok(c.post(f"/api/orders/{oid}/payments", headers=h(admin), json={"amount": 99999, "method": "cash"}), 400)
    ok(c.post(f"/api/orders/{oid}/payments", headers=h(admin), json={"amount": 3000, "method": "mpesa", "reference": "QWE123"}))
    o = c.get("/api/orders", headers=h(hotel)).json()[0]
    assert o["paid"] == 3000 and o["balance"] == total - 3000
    # can't re-deliver / cancel delivered
    ok(c.post(f"/api/orders/{oid}/status", headers=h(admin), json={"status": "cancelled"}), 400)

    # invoice
    r = c.get(f"/api/orders/{oid}/invoice", params={"token": hotel}); ok(r)
    assert "Brackenhurst" in r.text and "QWE123" in r.text
    ok(c.get(f"/api/orders/{oid}/invoice", params={"token": r2["token"]}), 404)

    # hotel cancels own pending order
    oid2 = c.post("/api/orders", headers=h(hotel), json={"items": [{"product_id": pid, "quantity": 1}], "delivery_date": tomorrow}).json()["id"]
    ok(c.post(f"/api/orders/{oid2}/status", headers=h(hotel), json={"status": "cancelled"}))

    # customers + index page
    cust = c.get("/api/customers", headers=h(admin)).json()
    assert any(x["business_name"] == "Brackenhurst" and x["balance"] == total - 3000 for x in cust), cust
    ok(c.get("/"))
    print("ALL CHECKS PASSED")
