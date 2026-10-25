from app import db, defects, products


def test_search_does_not_block_the_request(monkeypatch) -> None:
    monkeypatch.setattr(defects, "ACTIVE", {"slow_search"})
    db.init()

    def no_sleep(seconds: float) -> None:
        raise AssertionError("time.sleep() called while serving a search request")

    monkeypatch.setattr(products.time, "sleep", no_sleep)
    assert [p["name"] for p in products.list_products(q="mug")] == ["Limited mug"]
