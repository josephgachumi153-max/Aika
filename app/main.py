"""Aika - chicken ordering & records for hotels.

FastAPI + SQLite (stdlib). Two roles:
  - admin: manages products/prices, farmers, purchases (stock in), orders, payments, reports
  - hotel: registers, places orders, views own orders, invoices and balance
"""
import hashlib
import hmac
import os
import secrets
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from app.database import database
from app.appwrite import appwrite_health

BASE = Path(__file__).parent
BUSINESS_NAME = os.environ.get("AIKA_BUSINESS", "Aika")
BUSINESS_PHONE = os.environ.get("AIKA_PHONE", "")
BUSINESS_LOCATION = os.environ.get("AIKA_LOCATION", "Limuru, Kenya")

ORDER_STATUSES = ["pending", "confirmed", "delivered", "cancelled"]

app = FastAPI(title=BUSINESS_NAME)


@app.get("/api/health")
def health():
    return {"ok": True, "appwrite": appwrite_health()}


# ---------------------------------------------------------------- database
def db():
    return database(BASE.parent)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    business_name TEXT,
    email TEXT NOT NULL UNIQUE,
    phone TEXT,
    address TEXT,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'hotel',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    unit TEXT NOT NULL DEFAULT 'kg',
    price REAL NOT NULL,
    stock REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS farmers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    location TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    farmer_id INTEGER NOT NULL REFERENCES farmers(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity REAL NOT NULL,
    unit_cost REAL NOT NULL,
    paid INTEGER NOT NULL DEFAULT 0,
    purchase_date TEXT NOT NULL,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'pending',
    delivery_date TEXT,
    delivery_address TEXT,
    notes TEXT,
    total REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity REAL NOT NULL,
    unit_price REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    amount REAL NOT NULL,
    method TEXT NOT NULL DEFAULT 'mpesa',
    reference TEXT,
    paid_at TEXT NOT NULL,
    recorded_by INTEGER REFERENCES users(id)
);
"""

SEED_PRODUCTS = [
    ("Dressed broiler (whole)", "Vet-inspected, cleaned and dressed", "kg", 480),
    ("Kienyeji chicken (whole)", "Free-range, dressed", "bird", 1200),
    ("Chicken breast", "Boneless, vet-inspected", "kg", 700),
    ("Chicken drumsticks", "Vet-inspected", "kg", 520),
    ("Chicken wings", "Vet-inspected", "kg", 450),
    ("Eggs (tray of 30)", "Farm fresh", "tray", 480),
]


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_password(password, salt), stored)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@app.on_event("startup")
def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        if not conn.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            email = os.environ.get("AIKA_ADMIN_EMAIL", "admin@aika.local")
            password = os.environ.get("AIKA_ADMIN_PASSWORD", "admin123")
            conn.execute(
                "INSERT INTO users (name, business_name, email, password_hash, role, created_at) "
                "VALUES (?, ?, ?, ?, 'admin', ?)",
                ("Admin", BUSINESS_NAME, email, hash_password(password), now()),
            )
            print(f"\n*** Admin created: {email} / {password}  (change AIKA_ADMIN_PASSWORD!) ***\n")
        if not conn.execute("SELECT 1 FROM products").fetchone():
            conn.executemany(
                "INSERT INTO products (name, description, unit, price) VALUES (?, ?, ?, ?)",
                SEED_PRODUCTS,
            )


# -------------------------------------------------------------------- auth
def current_user(authorization: Optional[str] = Header(None)) -> Any:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not logged in")
    token = authorization[7:]
    with db() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Session expired, please log in again")
    return row


def admin_only(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return user


def public_user(u) -> dict:
    return {k: u[k] for k in ("id", "name", "business_name", "email", "phone", "address", "role")}


class RegisterIn(BaseModel):
    name: str = Field(min_length=2)
    business_name: str = Field(min_length=2)
    email: str
    phone: str = Field(min_length=7)
    address: str = ""
    password: str = Field(min_length=6)


class LoginIn(BaseModel):
    email: str
    password: str


def make_session(conn, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?)", (token, user_id, now()))
    return token


@app.post("/api/register")
def register(body: RegisterIn):
    with db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (body.email.lower(),)).fetchone():
            raise HTTPException(400, "That email is already registered")
        cur = conn.execute(
            "INSERT INTO users (name, business_name, email, phone, address, password_hash, role, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'hotel', ?)",
            (body.name, body.business_name, body.email.lower(), body.phone, body.address,
             hash_password(body.password), now()),
        )
        token = make_session(conn, cur.lastrowid)
        user = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"token": token, "user": public_user(user)}


@app.post("/api/login")
def login(body: LoginIn):
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (body.email.lower(),)).fetchone()
        if not user or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(401, "Wrong email or password")
        token = make_session(conn, user["id"])
    return {"token": token, "user": public_user(user)}


@app.post("/api/logout")
def logout(authorization: Optional[str] = Header(None), user=Depends(current_user)):
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (authorization[7:],))
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return public_user(user)


# ---------------------------------------------------------------- products
class ProductIn(BaseModel):
    name: str = Field(min_length=2)
    description: str = ""
    unit: str = "kg"
    price: float = Field(ge=0)
    active: bool = True


@app.get("/api/products")
def list_products(user=Depends(current_user)):
    with db() as conn:
        sql = "SELECT * FROM products"
        if user["role"] != "admin":
            sql += " WHERE active = 1"
        rows = conn.execute(sql + " ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if user["role"] != "admin":
            d.pop("stock")  # hotels don't see stock levels
        out.append(d)
    return out


@app.post("/api/products")
def create_product(body: ProductIn, admin=Depends(admin_only)):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO products (name, description, unit, price, active) VALUES (?, ?, ?, ?, ?)",
            (body.name, body.description, body.unit, body.price, int(body.active)),
        )
    return {"id": cur.lastrowid}


@app.put("/api/products/{pid}")
def update_product(pid: int, body: ProductIn, admin=Depends(admin_only)):
    with db() as conn:
        conn.execute(
            "UPDATE products SET name=?, description=?, unit=?, price=?, active=? WHERE id=?",
            (body.name, body.description, body.unit, body.price, int(body.active), pid),
        )
    return {"ok": True}


# ----------------------------------------------------------------- farmers
class FarmerIn(BaseModel):
    name: str = Field(min_length=2)
    phone: str = ""
    location: str = ""
    notes: str = ""


@app.get("/api/farmers")
def list_farmers(admin=Depends(admin_only)):
    with db() as conn:
        rows = conn.execute(
            """SELECT f.*,
                      COALESCE(SUM(p.quantity * p.unit_cost), 0) AS total_bought,
                      COALESCE(SUM(CASE WHEN p.paid = 0 THEN p.quantity * p.unit_cost END), 0) AS owed
               FROM farmers f LEFT JOIN purchases p ON p.farmer_id = f.id
               GROUP BY f.id ORDER BY f.name"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/farmers")
def create_farmer(body: FarmerIn, admin=Depends(admin_only)):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO farmers (name, phone, location, notes) VALUES (?, ?, ?, ?)",
            (body.name, body.phone, body.location, body.notes),
        )
    return {"id": cur.lastrowid}


# --------------------------------------------------------------- purchases
class PurchaseIn(BaseModel):
    farmer_id: int
    product_id: int
    quantity: float = Field(gt=0)
    unit_cost: float = Field(ge=0)
    paid: bool = False
    purchase_date: Optional[str] = None
    notes: str = ""


@app.get("/api/purchases")
def list_purchases(admin=Depends(admin_only)):
    with db() as conn:
        rows = conn.execute(
            """SELECT p.*, f.name AS farmer_name, pr.name AS product_name, pr.unit,
                      p.quantity * p.unit_cost AS total
               FROM purchases p
               JOIN farmers f ON f.id = p.farmer_id
               JOIN products pr ON pr.id = p.product_id
               ORDER BY p.purchase_date DESC, p.id DESC LIMIT 200"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/purchases")
def create_purchase(body: PurchaseIn, admin=Depends(admin_only)):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO purchases (farmer_id, product_id, quantity, unit_cost, paid, purchase_date, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (body.farmer_id, body.product_id, body.quantity, body.unit_cost, int(body.paid),
             body.purchase_date or date.today().isoformat(), body.notes),
        )
        conn.execute("UPDATE products SET stock = stock + ? WHERE id = ?", (body.quantity, body.product_id))
    return {"id": cur.lastrowid}


@app.post("/api/purchases/{pid}/pay")
def mark_purchase_paid(pid: int, admin=Depends(admin_only)):
    with db() as conn:
        conn.execute("UPDATE purchases SET paid = 1 WHERE id = ?", (pid,))
    return {"ok": True}


# ------------------------------------------------------------------ orders
class OrderItemIn(BaseModel):
    product_id: int
    quantity: float = Field(gt=0)


class OrderIn(BaseModel):
    items: list[OrderItemIn] = Field(min_length=1)
    delivery_date: str
    delivery_address: str = ""
    notes: str = ""


def order_dict(conn, row) -> dict:
    d = dict(row)
    items = conn.execute(
        """SELECT oi.*, p.name, p.unit FROM order_items oi
           JOIN products p ON p.id = oi.product_id WHERE oi.order_id = ?""",
        (row["id"],),
    ).fetchall()
    d["items"] = [dict(i) for i in items]
    paid = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE order_id = ?", (row["id"],)
    ).fetchone()[0]
    d["paid"] = paid
    d["balance"] = round(row["total"] - paid, 2)
    return d


@app.post("/api/orders")
def place_order(body: OrderIn, user=Depends(current_user)):
    if user["role"] == "admin":
        raise HTTPException(400, "Log in as a hotel account to place orders")
    try:
        if datetime.fromisoformat(body.delivery_date).date() < date.today():
            raise HTTPException(400, "Delivery date cannot be in the past")
    except ValueError:
        raise HTTPException(400, "Invalid delivery date")
    with db() as conn:
        total = 0.0
        lines = []
        for item in body.items:
            p = conn.execute("SELECT * FROM products WHERE id = ? AND active = 1", (item.product_id,)).fetchone()
            if not p:
                raise HTTPException(400, "A selected product is no longer available")
            lines.append((p["id"], item.quantity, p["price"]))
            total += item.quantity * p["price"]
        cur = conn.execute(
            "INSERT INTO orders (user_id, delivery_date, delivery_address, notes, total, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], body.delivery_date, body.delivery_address or user["address"],
             body.notes, round(total, 2), now()),
        )
        conn.executemany(
            "INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (?, ?, ?, ?)",
            [(cur.lastrowid, *l) for l in lines],
        )
    return {"id": cur.lastrowid, "total": round(total, 2)}


@app.get("/api/orders")
def list_orders(status: Optional[str] = None, user=Depends(current_user)):
    sql = """SELECT o.*, u.business_name, u.name AS contact_name, u.phone
             FROM orders o JOIN users u ON u.id = o.user_id"""
    args: list = []
    where = []
    if user["role"] != "admin":
        where.append("o.user_id = ?")
        args.append(user["id"])
    if status:
        where.append("o.status = ?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.id DESC LIMIT 300"
    with db() as conn:
        return [order_dict(conn, r) for r in conn.execute(sql, args).fetchall()]


def get_order_or_404(conn, oid: int, user) -> Any:
    row = conn.execute(
        """SELECT o.*, u.business_name, u.name AS contact_name, u.phone, u.email
           FROM orders o JOIN users u ON u.id = o.user_id WHERE o.id = ?""",
        (oid,),
    ).fetchone()
    if not row or (user["role"] != "admin" and row["user_id"] != user["id"]):
        raise HTTPException(404, "Order not found")
    return row


class StatusIn(BaseModel):
    status: str


@app.post("/api/orders/{oid}/status")
def set_status(oid: int, body: StatusIn, user=Depends(current_user)):
    if body.status not in ORDER_STATUSES:
        raise HTTPException(400, "Bad status")
    with db() as conn:
        order = get_order_or_404(conn, oid, user)
        if user["role"] != "admin":
            # hotels may only cancel their own pending orders
            if body.status != "cancelled" or order["status"] != "pending":
                raise HTTPException(403, "You can only cancel an order that is still pending")
        if order["status"] == body.status:
            return {"ok": True}
        if order["status"] in ("delivered", "cancelled"):
            raise HTTPException(400, f"Order is already {order['status']}")
        if body.status == "delivered":
            # deduct stock (may go negative if you sold before recording a purchase; admin sees it)
            for it in conn.execute("SELECT * FROM order_items WHERE order_id = ?", (oid,)).fetchall():
                conn.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (it["quantity"], it["product_id"]))
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (body.status, oid))
    return {"ok": True}


# ---------------------------------------------------------------- payments
class PaymentIn(BaseModel):
    amount: float = Field(gt=0)
    method: str = "mpesa"
    reference: str = ""
    paid_at: Optional[str] = None


@app.post("/api/orders/{oid}/payments")
def add_payment(oid: int, body: PaymentIn, admin=Depends(admin_only)):
    with db() as conn:
        order = get_order_or_404(conn, oid, admin)
        paid = conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE order_id=?", (oid,)).fetchone()[0]
        if body.amount + paid > order["total"] + 0.01:
            raise HTTPException(400, f"Payment exceeds the balance of KES {order['total'] - paid:,.2f}")
        conn.execute(
            "INSERT INTO payments (order_id, amount, method, reference, paid_at, recorded_by) VALUES (?,?,?,?,?,?)",
            (oid, body.amount, body.method, body.reference, body.paid_at or now(), admin["id"]),
        )
    return {"ok": True}


@app.get("/api/orders/{oid}/payments")
def order_payments(oid: int, user=Depends(current_user)):
    with db() as conn:
        get_order_or_404(conn, oid, user)
        rows = conn.execute("SELECT * FROM payments WHERE order_id = ? ORDER BY id", (oid,)).fetchall()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------- invoice
def esc(s) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@app.get("/api/orders/{oid}/invoice", response_class=HTMLResponse)
def invoice(oid: int, token: str):
    """Printable invoice. Token in query string so it can open in a new tab."""
    user = current_user(f"Bearer {token}")
    with db() as conn:
        order = get_order_or_404(conn, oid, user)
        d = order_dict(conn, order)
        pays = conn.execute("SELECT * FROM payments WHERE order_id = ? ORDER BY id", (oid,)).fetchall()
    rows = "".join(
        f"<tr><td>{esc(i['name'])}</td><td class=r>{i['quantity']:g} {esc(i['unit'])}</td>"
        f"<td class=r>{i['unit_price']:,.2f}</td><td class=r>{i['quantity'] * i['unit_price']:,.2f}</td></tr>"
        for i in d["items"]
    )
    pay_rows = "".join(
        f"<tr><td>{esc(p['paid_at'][:10])}</td><td>{esc(p['method'])} {esc(p['reference'])}</td>"
        f"<td class=r>{p['amount']:,.2f}</td></tr>" for p in pays
    )
    pay_block = (f"<h3>Payments received</h3><table>{pay_rows}</table>" if pays else "")
    return f"""<!doctype html><html><head><meta charset=utf-8><title>Invoice #{oid:05d}</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
body{{font:15px/1.5 system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color:#222}}
h1{{margin:0}} .top{{display:flex;justify-content:space-between;flex-wrap:wrap;gap:12px;border-bottom:2px solid #222;padding-bottom:12px}}
table{{width:100%;border-collapse:collapse;margin:12px 0}} th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}
.r{{text-align:right}} .tot td{{font-weight:700;border-top:2px solid #222}} .muted{{color:#666}}
button{{padding:8px 16px;margin-top:16px}} @media print{{button{{display:none}}}}
</style></head><body>
<div class=top><div><h1>{esc(BUSINESS_NAME)}</h1><div class=muted>{esc(BUSINESS_LOCATION)}<br>{esc(BUSINESS_PHONE)}</div></div>
<div style="text-align:right"><h2 style="margin:0">INVOICE #{oid:05d}</h2>
<div class=muted>Issued {esc(d['created_at'][:10])}<br>Status: {esc(d['status'])}</div></div></div>
<p><b>Bill to:</b><br>{esc(d['business_name'])}<br>{esc(d['contact_name'])} &middot; {esc(d['phone'])}<br>
<b>Deliver to:</b> {esc(d['delivery_address'])} on {esc(d['delivery_date'])}</p>
<table><tr><th>Item</th><th class=r>Qty</th><th class=r>Price (KES)</th><th class=r>Amount (KES)</th></tr>{rows}
<tr class=tot><td colspan=3 class=r>Total</td><td class=r>KES {d['total']:,.2f}</td></tr>
<tr><td colspan=3 class=r>Paid</td><td class=r>KES {d['paid']:,.2f}</td></tr>
<tr class=tot><td colspan=3 class=r>Balance due</td><td class=r>KES {d['balance']:,.2f}</td></tr></table>
{pay_block}
<p class=muted>All poultry is vet-inspected before delivery. Thank you for your business.</p>
<button onclick="window.print()">Print / Save as PDF</button></body></html>"""


# ----------------------------------------------------------------- reports
@app.get("/api/reports/summary")
def summary(admin=Depends(admin_only)):
    with db() as conn:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
        month = date.today().strftime("%Y-%m")
        data = {
            "pending_orders": q("SELECT COUNT(*) FROM orders WHERE status='pending'"),
            "to_deliver": q("SELECT COUNT(*) FROM orders WHERE status='confirmed'"),
            "sales_this_month": q(
                "SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='cancelled' AND substr(created_at,1,7)=?", month),
            "purchases_this_month": q(
                "SELECT COALESCE(SUM(quantity*unit_cost),0) FROM purchases WHERE substr(purchase_date,1,7)=?", month),
            "receivable": q(
                """SELECT COALESCE(SUM(o.total - COALESCE((SELECT SUM(amount) FROM payments WHERE order_id=o.id),0)),0)
                   FROM orders o WHERE o.status IN ('confirmed','delivered')"""),
            "owed_to_farmers": q(
                "SELECT COALESCE(SUM(quantity*unit_cost),0) FROM purchases WHERE paid=0"),
        }
        data["stock"] = [dict(r) for r in conn.execute(
            "SELECT id, name, unit, stock FROM products WHERE active=1 ORDER BY name")]
        data["top_customers"] = [dict(r) for r in conn.execute(
            """SELECT u.business_name, COUNT(o.id) AS orders, SUM(o.total) AS total
               FROM orders o JOIN users u ON u.id=o.user_id WHERE o.status!='cancelled'
               GROUP BY u.id ORDER BY total DESC LIMIT 5""")]
    return data


@app.get("/api/customers")
def customers(admin=Depends(admin_only)):
    with db() as conn:
        rows = conn.execute(
            """SELECT u.id, u.business_name, u.name, u.phone, u.email, u.address,
                      COUNT(o.id) AS orders,
                      COALESCE(SUM(CASE WHEN o.status!='cancelled' THEN o.total END),0) AS total_ordered,
                      COALESCE(SUM(CASE WHEN o.status!='cancelled' THEN o.total END),0)
                        - COALESCE((SELECT SUM(p.amount) FROM payments p JOIN orders o2 ON o2.id=p.order_id
                                    WHERE o2.user_id=u.id),0) AS balance
               FROM users u LEFT JOIN orders o ON o.user_id=u.id
               WHERE u.role='hotel' GROUP BY u.id ORDER BY u.business_name"""
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- frontend
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")
