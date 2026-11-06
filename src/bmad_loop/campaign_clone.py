"""Fast isolated copies of campaign dataclasses and their JSON-shaped payloads.

Atomic leaves need no memo lookup or generic reconstruction. Mutable containers
and dataclass instances remain independently owned; shared references and cycles
retain deepcopy semantics. Specialized objects, including lazy event journals,
keep their existing deepcopy contract.
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any

_ATOMIC = frozenset({type(None), bool, int, float, str, bytes, complex})


def clone(value: Any, memo: dict[int, Any] | None = None) -> Any:
    if type(value) in _ATOMIC:
        return value
    if memo is None:
        memo = {}
    identity = id(value)
    if identity in memo:
        return memo[identity]
    kind = type(value)
    if kind is dict:
        result: dict[Any, Any] = {}
        memo[identity] = result
        for key, item in value.items():
            result[key if type(key) in _ATOMIC else clone(key, memo)] = (
                item if type(item) in _ATOMIC else clone(item, memo)
            )
        return result
    if kind is list:
        items: list[Any] = []
        memo[identity] = items
        items.extend(item if type(item) in _ATOMIC else clone(item, memo) for item in value)
        return items
    if kind is tuple:
        items = [item if type(item) in _ATOMIC else clone(item, memo) for item in value]
        # A tuple/list cycle may have completed this tuple in the recursive call.
        if identity in memo:
            return memo[identity]
        result_tuple = tuple(items)
        memo[identity] = result_tuple
        return result_tuple
    if (
        kind.__module__ == "bmad_loop.campaign_model"
        and dataclasses.is_dataclass(value)
        and not isinstance(value, type)
    ):
        result_object = copy.copy(value)
        memo[identity] = result_object
        # Preserve attached data as well as declared fields, matching deepcopy.
        # A slotted child may inherit an instance dictionary from its parent.
        attributes = dict(vars(value)) if hasattr(value, "__dict__") else {}
        for field in dataclasses.fields(value):
            if field.name not in attributes:
                attributes[field.name] = getattr(value, field.name)
        for name, item in attributes.items():
            object.__setattr__(
                result_object, name, item if type(item) in _ATOMIC else clone(item, memo)
            )
        return result_object
    return copy.deepcopy(value, memo)
