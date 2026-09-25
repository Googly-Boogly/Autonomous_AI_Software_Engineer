from fastapi import FastAPI

app = FastAPI(title="sample-fastapi")

_ITEMS = {1: {"id": 1, "name": "widget"}, 2: {"id": 2, "name": "gadget"}}


@app.get("/items")
def list_items() -> list[dict[str, object]]:
    return list(_ITEMS.values())


@app.get("/items/{item_id}")
def get_item(item_id: int) -> dict[str, object]:
    from fastapi import HTTPException

    if item_id not in _ITEMS:
        raise HTTPException(status_code=404, detail="item not found")
    return _ITEMS[item_id]
