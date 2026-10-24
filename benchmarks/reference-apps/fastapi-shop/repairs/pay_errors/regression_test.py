from app import db, defects, orders


def test_transient_gateway_errors_are_retried(monkeypatch) -> None:
    monkeypatch.setattr(defects, "ACTIVE", {"pay_errors"})
    db.init()
    oid = db.execute("INSERT INTO orders (user_id, product_id, quantity, status) VALUES (1, 1, 1, 'pending')")
    draws = iter([0.0, 0.99, 0.99, 0.99])  # the first charge times out, the next succeeds
    calls: list[int] = []

    def fake_random() -> float:
        calls.append(1)
        return next(draws)

    monkeypatch.setattr(orders.random, "random", fake_random)
    result = orders.pay_order(oid, user=1)
    assert result["status"] == "paid"
    assert len(calls) >= 2, "the failed charge was not retried"
