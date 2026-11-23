"""DB-transaction budget gate (DOC.md §6.1 #7, workspace_budgets)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

FALLBACK_RATIO = Decimal("0.80")
STOP_RATIO = Decimal("1.00")


@dataclass
class BudgetDecision:
    action: str  # ALLOW | FALLBACK | HALT
    model: str | None
    ratio: Decimal


def check_budget(limit_usd, spent_usd, estimate_usd, preferred_model: str, cheap_model: str) -> BudgetDecision:
    limit, spent, est = Decimal(str(limit_usd)), Decimal(str(spent_usd)), Decimal(str(estimate_usd))
    if limit <= 0:
        return BudgetDecision("HALT", None, Decimal("Infinity"))
    ratio = (spent + est) / limit
    if ratio >= STOP_RATIO or spent / limit >= STOP_RATIO:
        return BudgetDecision("HALT", None, ratio)
    if spent / limit >= FALLBACK_RATIO:
        return BudgetDecision("FALLBACK", cheap_model, ratio)
    return BudgetDecision("ALLOW", preferred_model, ratio)


# Atomic charge executed in step (3) of the 3-step short transaction (§4.1).
# Returns no row when the charge would exceed the limit -> caller switches to HITL.
CHARGE_SQL = """
UPDATE workspace_budgets
   SET spent_usd = spent_usd + %(cost)s
 WHERE workspace_id = %(workspace_id)s AND period_ym = %(period_ym)s
   AND spent_usd + %(cost)s <= limit_usd
RETURNING spent_usd, limit_usd;
"""


class BudgetHalt(Exception):
    """Workspace budget exhausted (>= 100 %): stop the pipeline and hand over to a human (HITL)."""
