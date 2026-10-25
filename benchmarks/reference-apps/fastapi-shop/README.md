# fastapi-shop (reference app)

A small shop API used by the evaluation corpus (docs/architecture/evaluation.md §3):
sign-up/login, products, orders with payment and cancellation, backed by SQLite.

Run: `uvicorn app.main:app --port 8000` (or `python -m app.main`).

## Defect toggles

`DEFECTS` (comma-separated) switches on seeded defects with known root causes. Each defect is
labelled in `labels.json` with its mechanism and the files a correct fix touches.

| Defect | Symptom | Mechanism |
|---|---|---|
| `race_stock` | stock oversold under concurrent orders (invariant violation) | check-then-act on stock without a transaction/lock |
| `n_plus_one` | `GET /orders` latency grows with order count | one product query per order row |
| `slow_search` | `GET /products?q=` slow under load | per-request full scan with an artificial sleep |
| `pay_errors` | ~5% of `POST /orders/{id}/pay` return 500 | unhandled exception in the payment adapter |

With `DEFECTS` empty the app is healthy.
