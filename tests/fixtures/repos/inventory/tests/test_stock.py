import pytest

from inventory import Inventory


def test_add_and_quantity():
    inv = Inventory()
    inv.add("apple", 3)
    inv.add("apple", 2)
    assert inv.quantity("apple") == 5


def test_remove_insufficient():
    inv = Inventory()
    inv.add("pear", 1)
    with pytest.raises(ValueError):
        inv.remove("pear", 2)
