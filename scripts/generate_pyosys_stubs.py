from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from pybind11_stubgen import main as stubgen_main


def _safe_getmembers(
    obj: object, predicate: Callable[[Any], bool] | None = None
) -> list[tuple[str, Any]]:
    members: list[tuple[str, Any]] = []

    for name in dir(obj):
        try:
            value = getattr(obj, name)
        except Exception:
            continue

        if predicate is None or predicate(value):
            members.append((name, value))

    return sorted(members)


def main() -> None:
    setattr(inspect, "getmembers", _safe_getmembers)
    stubgen_main()


if __name__ == "__main__":
    main()