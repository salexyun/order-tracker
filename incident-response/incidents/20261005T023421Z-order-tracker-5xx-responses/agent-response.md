I fixed the 500 errors on `GET /api/orders/{order_id}` and the app is back up: `express-1002` now returns 200 with `estimated_delivery` of `2026-10-02`. The fix is committed locally but not pushed.

- **Impact:** customers got 500 errors when viewing express orders placed in the last two days of a month. Here that was `express-1002`, created 2026-09-30. The other errors in the window were normal 404s for an order that doesn't exist.
- **Cause:** `order_detail` (`app/main.py:62`) worked out the delivery date by adding 2 to the day number. For an order placed on 30 September that gives "September 32", which raises a `ValueError`.
- **Fix:** it now adds two days with `timedelta(days=2)`, which carries over into the next month and year correctly. I added a regression test with a 30 September order. It failed before the fix, and all 4 tests pass after it.
- **Verification:** before the fix, `curl -s -w '\nHTTP %{http_code}\n' http://localhost:8000/api/orders/express-1002` returned 500. After redeploying the app it returns 200.
- **Commit:** `8129ddf` contains only `app/main.py` and `tests/test_api.py`. The incident write-up is in `report.md` in the incident directory.

RESULT: fixed - Express delivery estimates were calculated by adding 2 to the day number, which raised a ValueError for orders placed in the last two days of a month.
