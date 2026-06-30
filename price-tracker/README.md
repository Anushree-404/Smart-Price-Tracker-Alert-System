# Smart Price Tracker

A production-ready Flask web app that tracks Amazon India and Flipkart product prices,
logs price history, and sends Gmail email alerts when prices drop past your threshold.

---

## Features

- **Multi-product tracking** — add any number of Amazon / Flipkart URLs
- **Automatic price checks** — APScheduler runs a background job every 6 hours (configurable)
- **Email alerts** — Gmail SMTP notification when the price drops by your chosen %
- **Price history charts** — full Chart.js line chart per product + sparkline trend on each card
- **Alert management** — view, re-enable, or delete individual alerts from the history modal
- **Live search/filter** — instantly filter your product list by name or website
- **Dark mode** — one-click toggle, persisted in `localStorage`
- **Per-card refresh** — update a single product price without refreshing everything
- **Scheduler status footer** — always shows when the next automatic check will run

---

## Quick Start

### 1. Clone / download the project

```
price-tracker/
├── app.py
├── requirements.txt
├── .env
├── templates/index.html
└── static/
    ├── styles.css
    └── script.js
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

> **Windows note:** `lxml` is not included in requirements because it requires
> Visual C++ build tools on Windows. The app uses Python's built-in `html.parser` instead
> with no loss of functionality.

### 3. Configure `.env`

Edit the `.env` file in the project root:

```env
GMAIL_USER=your_email@gmail.com
GMAIL_PASSWORD=your_app_password_here
SECRET_KEY=change-me-to-a-random-secret-key
CHECK_INTERVAL_HOURS=6
```

> **Gmail App Password:** If you have 2-Factor Authentication enabled (recommended),
> generate an App Password at https://myaccount.google.com/apppasswords
> and use that instead of your regular password.

### 4. Run

```bash
python app.py
```

Open your browser at **http://localhost:5000**

---

## REST API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Serve the SPA |
| `POST` | `/api/add-product` | Add a product to track |
| `GET` | `/api/products` | List all products with sparkline data |
| `GET` | `/api/product-history/<id>` | Full price history for one product |
| `DELETE` | `/api/delete-product/<id>` | Delete a product and all its data |
| `GET` | `/api/stats` | Aggregate stats (total products, savings, alerts) |
| `POST` | `/api/manual-check` | Trigger immediate price check (all or one) |
| `GET` | `/api/scheduler-status` | Next scheduled run time |
| `GET` | `/api/alerts/<product_id>` | List all alerts for a product |
| `POST` | `/api/alerts/<alert_id>/reactivate` | Re-enable a fired alert |
| `DELETE` | `/api/alerts/<alert_id>` | Delete a single alert |

### POST `/api/add-product`

```json
{
  "url": "https://www.amazon.in/dp/B0XXXXXX",
  "email": "you@example.com",
  "threshold_pct": 10
}
```

### POST `/api/manual-check`

```json
{ "product_id": 3 }   // omit to check all products
```

---

## Database Schema

**SQLite** file: `price_tracker.db` (auto-created on first run, excluded from git)

```
products       — id, url (unique), name, website, original_price, current_price,
                 image_url, added_at, last_checked
price_history  — id, product_id, price, checked_at
alerts         — id, product_id, email, threshold_pct, is_active, created_at
```

---

## Production Deployment

For production, replace the development server with a proper WSGI server:

```bash
pip install waitress
waitress-serve --port=5000 app:app
```

Or with gunicorn on Linux/macOS:

```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:5000 app:app
```

> **Note:** Keep `use_reloader=False` in `app.run()` (already set) to prevent
> APScheduler from starting twice when the Flask reloader is active.

---

## Scraper Notes

Both scrapers use multiple CSS selector fallbacks to handle Amazon/Flipkart layout variations.
If a site changes its HTML structure, update the selectors in `scrape_amazon()` or
`scrape_flipkart()` in `app.py`. The selectors are documented inline.

Sites may occasionally return CAPTCHA pages or rate-limit requests. The scrapers add a
random 1–3s delay between requests and rotate through 5 different User-Agent strings.
