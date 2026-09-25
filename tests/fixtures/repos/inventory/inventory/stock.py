"""In-memory stock tracking."""


class Inventory:
    def __init__(self) -> None:
        self._stock: dict[str, int] = {}

    def add(self, sku: str, quantity: int) -> None:
        self._stock[sku] = self._stock.get(sku, 0) + quantity

    def remove(self, sku: str, quantity: int) -> None:
        current = self._stock.get(sku, 0)
        if quantity > current:
            raise ValueError(f"insufficient stock for {sku}")
        self._stock[sku] = current - quantity

    def quantity(self, sku: str) -> int:
        return self._stock.get(sku, 0)
