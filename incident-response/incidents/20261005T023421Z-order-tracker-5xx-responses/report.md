# Incident 20261005T023421Z-order-tracker-5xx-responses

## Impact
`GET /api/orders/{order_id}` returned HTTP 500 for express orders placed in the
last two days of a month (here `express-1002`, created 2026-09-30). Customers
could not view those orders. Standard orders and other endpoints were unaffected.
1 server error was recorded in the alert window.

## Evidence used
- Alert: `Order Tracker 5xx responses` on `GET /api/orders/{order_id}`, value 1.
- Loki ERROR log `order lookup failed` (trace `1d0b3b2c7ddf7127c5fa301d28524dad`):
  `ValueError: day is out of range for month` at `app/main.py:62` in
  `order_detail`; `order_id=express-1002`, `order_priority=express`,
  `order_created_at=2026-09-30T02:19:36.381225+00:00`.
- Tempo trace for the same ID: `order.lookup` span errored with `ValueError`,
  HTTP span status 500.
- The other two traces in the window were expected 404s for `standard-1002`.

## Root cause
`order_detail` computed the estimated delivery date with
`placed_at.replace(day=placed_at.day + 2)`, which produces an invalid date
(e.g. September 32) when the order is placed within two days of month end.

## Fix
Use `placed_at + timedelta(days=2)` (`app/main.py`), which rolls over month
and year boundaries correctly. Added regression test
`tests/test_api.py::test_express_order_placed_at_month_end` (failed before the
fix with the same ValueError; passes after). `uv run --frozen pytest -q`:
4 passed.

## Verification
Redeployed with `docker compose up --build -d --wait app` (app healthy), then:

```
curl -s -w '\nHTTP %{http_code}\n' http://localhost:8000/api/orders/express-1002
```

Before the fix: `HTTP 500` (`Internal Server Error`).
After the fix: `HTTP 200`, body includes
`"created_at":"2026-09-30T02:19:36.381225+00:00","estimated_delivery":"2026-10-02"`.

## Commit
`8129ddf fix(20261005T023421Z-order-tracker-5xx-responses): compute express delivery date with timedelta` (not pushed).
