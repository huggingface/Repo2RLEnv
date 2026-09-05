from __future__ import annotations

import fcntl
import json
import math
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from repo2rlenv.curation.inference import MAX_OUTPUT_TOKENS, MODEL_TIMEOUT_SEC, anthropic_options


class BudgetExceeded(RuntimeError):
    pass


def _scope_constraints(registry: dict) -> dict:
    """Validate and normalize the host-owned registry without changing the ledger."""
    if not isinstance(registry, dict):
        raise ValueError("Invalid scope budget constraints registry")
    normalized = {}
    for scope, constraints in registry.items():
        if (
            not isinstance(scope, str)
            or not scope.strip()
            or not isinstance(constraints, dict)
            or not constraints
        ):
            raise ValueError("Invalid scope budget constraints")
        normalized[scope] = {}
        for name, rule in constraints.items():
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(rule, dict)
                or set(rule) != {"scopes", "groups", "limit_usd"}
            ):
                raise ValueError("Invalid named scope budget constraint")
            limit = rule["limit_usd"]
            if type(limit) not in (int, float) or not math.isfinite(limit) or limit <= 0:
                raise ValueError("Scope budget constraint limit must be finite and positive")
            memberships = {}
            for field in ("scopes", "groups"):
                values = rule[field]
                if (
                    not isinstance(values, list)
                    or any(not isinstance(value, str) or not value.strip() for value in values)
                    or len(set(values)) != len(values)
                ):
                    raise ValueError("Invalid scope budget constraint membership")
                memberships[field] = sorted(values)
            if not memberships["scopes"] and not memberships["groups"]:
                raise ValueError("Scope budget constraint needs scope or group membership")
            normalized[scope][name] = {**memberships, "limit_usd": limit}
    return normalized


def register_scope_constraints(state: dict, scope: str, constraints: dict) -> None:
    """Bind limits while the caller holds the ledger lock, before any child charge.

    Rules use named scope/group unions. Each also includes its target scope, so
    reconstructed subprocess budgets cannot omit prior child charges by omitting
    the group argument. Existing bindings are immutable and registration is
    idempotent. Settlement and historical entries are never rewritten.
    """
    registry = _scope_constraints(state.get("scope_constraints", {}))
    proposed = _scope_constraints({scope: constraints})[scope]
    if scope in registry:
        if registry[scope] != proposed:
            raise ValueError("Scope budget constraints already bound to a different policy")
        return
    if any(entry.get("scope") == scope for entry in state["entries"].values()):
        raise ValueError("Cannot register missing scope constraints after charges exist")
    state.setdefault("scope_constraints", {})[scope] = proposed


def _enforce_scope_constraints(state: dict, scope: str | None, amount: float) -> None:
    registry = _scope_constraints(state.get("scope_constraints", {}))
    for name, rule in registry.get(scope, {}).items():
        scopes, groups = set(rule["scopes"]) | {scope}, set(rule["groups"])
        committed = sum(
            entry["charged_usd"]
            for entry in state["entries"].values()
            if entry.get("scope") in scopes or entry.get("group") in groups
        )
        if committed + amount > rule["limit_usd"]:
            label = name.replace("_", " ").capitalize()
            raise BudgetExceeded(
                f"{label} allowance ${rule['limit_usd']:.2f}: "
                f"${committed:.2f} committed; need ${amount:.2f}"
            )


class Budget:
    """Process-safe write-ahead reservations. Crashes retain outstanding charges."""

    def __init__(
        self,
        path: Path,
        limit: float,
        *,
        scope: str | None = None,
        scope_limit: float | None = None,
        group: str | None = None,
        group_limit: float | None = None,
    ):
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("Budget must be finite and positive")
        self.path, self.limit = Path(path), limit
        self.scope, self.scope_limit = scope, scope_limit
        self.group, self.group_limit = group, group_limit
        if group_limit is not None and (
            not group or not math.isfinite(group_limit) or group_limit <= 0
        ):
            raise ValueError("Group limit needs a name and a finite positive limit")
        if scope_limit is not None and (not math.isfinite(scope_limit) or scope_limit <= 0):
            raise ValueError("Scope limit must be finite and positive")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self):
        with self.path.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(self.path.read_text()) if self.path.exists() else {"entries": {}}
            try:
                yield state
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, indent=2))
                tmp.replace(self.path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @property
    def spent(self) -> float:
        with self._locked() as state:
            return sum(
                e["charged_usd"]
                for e in state["entries"].values()
                if self.scope is None or e.get("scope") == self.scope
            )

    def reserve(self, amount: float, label: str) -> str:
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("Reservation must be finite and positive")
        with self._locked() as state:
            _enforce_scope_constraints(state, self.scope, amount)
            total = sum(e["charged_usd"] for e in state["entries"].values())
            if total + amount > self.limit:
                raise BudgetExceeded(
                    f"Budget ${self.limit:.2f}: ${total:.2f} committed; need ${amount:.2f}"
                )
            if self.scope_limit is not None:
                scoped = sum(
                    e["charged_usd"]
                    for e in state["entries"].values()
                    if e.get("scope") == self.scope
                )
                if scoped + amount > self.scope_limit:
                    raise BudgetExceeded(
                        f"Candidate budget ${self.scope_limit:.2f}: ${scoped:.2f} committed; need ${amount:.2f}"
                    )
            if self.group_limit is not None:
                grouped = sum(
                    e["charged_usd"]
                    for e in state["entries"].values()
                    if e.get("group") == self.group
                )
                if grouped + amount > self.group_limit:
                    raise BudgetExceeded(
                        f"Batch budget ${self.group_limit:.2f}: ${grouped:.2f} committed; need ${amount:.2f}"
                    )
            key = uuid4().hex
            state["entries"][key] = {
                "label": label,
                "scope": self.scope,
                **({"group": self.group} if self.group is not None else {}),
                "reserved_usd": amount,
                "charged_usd": amount,
                "status": "reserved",
                "time": time.time(),
            }
            return key

    def settle(self, key: str, actual: float | None = None, *, estimated: bool = False) -> None:
        with self._locked() as state:
            e = state["entries"][key]
            if e["status"] != "reserved":
                raise ValueError("Reservation already settled")
            amount = e["reserved_usd"] if actual is None else actual
            if not math.isfinite(amount) or amount < 0:
                raise ValueError("Invalid settled cost")
            e.update(
                charged_usd=amount, status="estimated" if actual is None or estimated else "metered"
            )
            if amount > e["reserved_usd"]:
                e["overrun"] = True


async def completion(
    budget: Budget,
    model: str,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    max_charge: float | None = None,
    tool_choice: str | None = None,
):
    import litellm

    pricing = litellm.get_model_info(model)
    in_rate, out_rate = pricing.get("input_cost_per_token"), pricing.get("output_cost_per_token")
    if not in_rate or not out_rate:
        raise ValueError(f"No known price for {model}; refusing an unbudgeted call")
    # UTF-8 bytes overestimate text tokens, plus tool/protocol overhead. Never
    # rely on an optimistic tokenizer estimate for the pre-call reservation.
    input_bound = len(json.dumps([messages, tools], ensure_ascii=False).encode()) + 4096
    cache_options = (
        {"cache_control": {"type": "ephemeral"}} if model.startswith("anthropic/") else {}
    )
    # Cache writes cost 1.25x input; reserve that worst case, settle actual reads.
    ceiling = input_bound * in_rate * (1.25 if cache_options else 1) + max_tokens * out_rate
    if max_charge is not None and ceiling > max_charge:
        raise BudgetExceeded("Next model request would exceed the agent cost limit")
    key = budget.reserve(ceiling, f"llm:{model}")
    response = await litellm.acompletion(
        model=model,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        timeout=MODEL_TIMEOUT_SEC,
        num_retries=0,
        **({"tool_choice": tool_choice} if tool_choice is not None else {}),
        **cache_options,
        **anthropic_options(model),
    )
    cost = float(litellm.completion_cost(completion_response=response))
    budget.settle(key, cost)
    return response, cost
