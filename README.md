# Aika

Web app for selling chicken to hotels: hotels order online, you track farmer purchases, stock, deliveries, payments and invoices.

## Run it (Windows, cmd.exe)

1. Install Python 3.10+ if you don't have it.
2. Open `run.bat` in a text editor and change `AIKA_ADMIN_PASSWORD` and the business phone/location.
3. Double-click `run.bat` (first run creates a venv and installs FastAPI + Uvicorn).
4. Open http://localhost:8000. Phones on the same Wi-Fi can use `http://<your-PC-IP>:8000`.

Default admin login: `admin@aika.local` / whatever you set in `run.bat`.

Run on Linux/Mac: `pip install -r requirements.txt && uvicorn app.main:app --port 8000`

## How it works

**Hotels** register, pick products and quantities, choose a delivery date, and place an order. They can see their orders, balance, print invoices, and cancel orders still pending.

**You (admin)**
- **Dashboard:** pending and to-deliver orders, sales and purchases this month, what customers owe you, what you owe farmers, stock levels.
- **Orders:** confirm, mark delivered, cancel, record payments (M-Pesa/cash/bank with reference), open invoice.
- **Products:** add products, edit prices, hide items.
- **Farmers / Purchases:** record each purchase; stock goes up and the amount owed to the farmer is tracked until you mark it paid.
- **Customers:** each hotel's total ordered and outstanding balance.

Stock goes up on purchases and down when an order is marked **delivered**. Prices are locked into the order when placed, so later price changes don't alter old invoices.

## Data

Everything is in one SQLite file, `aika.db`, next to the app. **Back it up regularly** (copy the file).

## Deploy to Vercel with Appwrite

The Vercel project is linked to this directory. In its Vercel project settings, add these environment variables for Production (and Preview/Development if needed):

- `APPWRITE_ENDPOINT` — your Appwrite API endpoint, such as `https://fra.cloud.appwrite.io/v1`
- `APPWRITE_PROJECT_ID` — your Appwrite project ID
- `APPWRITE_API_KEY` — a server API key; keep it secret and never expose it in browser code

The API key needs database, collection, attribute, index, and document read/write permissions. With those variables set in your local shell, run `python appwrite_setup.py` once to create the Appwrite database, collection, attributes, and index. Then deploy from this directory using the Vercel CLI (`vercel --prod`) or trigger a deployment from the Vercel dashboard.

When Appwrite is configured, the app stores compressed, versioned SQLite snapshots in that collection. Concurrent writes are rejected with HTTP 409 instead of silently overwriting one another. Check `/api/health` after deployment; its `appwrite.healthy` value should be `true`.

## Tests

`python test_flow.py` runs an end-to-end check on a throwaway database.

## Not included yet (good next steps)

- M-Pesa Daraja STK push so hotels pay in-app (record payments manually for now)
- SMS/WhatsApp notifications on new orders and deliveries
- Password reset, and putting it online behind HTTPS (needed before real use outside your LAN)
- Standing/recurring weekly orders for regular hotel customers
