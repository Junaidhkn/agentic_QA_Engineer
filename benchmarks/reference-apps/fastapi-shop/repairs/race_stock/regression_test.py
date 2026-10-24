import threading

from fastapi import HTTPException

from app import db, defects
from app.orders import OrderIn, create_order


def test_limited_stock_is_never_oversold(monkeypatch) -> None:
    monkeypatch.setattr(defects, "ACTIVE", {"race_stock"})
    db.init()
    db.execute("UPDATE products SET stock = 10 WHERE id = 3")
    sold: list[int] = []
    start = threading.Barrier(20)

    def buy() -> None:
        start.wait()
        try:
            create_order(OrderIn(product_id=3, quantity=1), user=1)
            sold.append(1)
        except HTTPException as exc:
            assert exc.status_code == 409

    threads = [threading.Thread(target=buy) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stock = db.query("SELECT stock FROM products WHERE id = 3")[0]["stock"]
    assert stock >= 0, f"stock went negative: {stock}"
    assert len(sold) + stock == 10, f"sold {len(sold)} with stock {stock} left of 10"
