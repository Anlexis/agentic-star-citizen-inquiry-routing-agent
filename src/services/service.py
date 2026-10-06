"""AgentCore Platform v1.0"""

# Service layer: domain lookups and data access.
# Must NOT contain routing decisions, business rules or credentials — nodes own
# those. This module owns one thing: what the department directory says.
#
# The shipped directory is the built-in default table below. A deployment
# normally replaces it wholesale, either in config/config.yaml (routing.table)
# or per request through the caller-data contract; both arrive here as an
# already-validated table. This is also the seam to wire a real directory
# service (an org-chart API, a case-management system) in place of the static
# mapping — replace lookup()'s source, and nothing else in the pipeline changes.

from __future__ import annotations

from typing import Any, Optional

# Default department directory: category → routing entry.
# Every entry has the same shape the caller-data contract enforces:
#   department       [a-z0-9_]{1,48}
#   sla_hours        whole hours, 1..720
#   escalation_path  [a-z0-9_]{1,48} or None
#   contact_key      [a-z0-9_]{1,48}
_DEFAULT_DEPARTMENT_TABLE: dict[str, dict[str, Any]] = {
    "permit_inquiry": {
        "department": "urban_planning_dept",
        "sla_hours": 48,
        "escalation_path": None,
        "contact_key": "urban_planning_contact",
    },
    "tax_question": {
        "department": "taxation_dept",
        "sla_hours": 24,
        "escalation_path": None,
        "contact_key": "taxation_contact",
    },
    "social_welfare": {
        "department": "welfare_dept",
        "sla_hours": 12,
        "escalation_path": "welfare_supervisor",
        "contact_key": "welfare_contact",
    },
    "public_safety": {
        "department": "public_safety_dept",
        "sla_hours": 2,
        "escalation_path": "emergency_line",
        "contact_key": "public_safety_contact",
    },
    "infrastructure": {
        "department": "public_works_dept",
        "sla_hours": 24,
        "escalation_path": None,
        "contact_key": "public_works_contact",
    },
    "complaint": {
        "department": "citizen_relations_dept",
        "sla_hours": 8,
        "escalation_path": "citizen_relations_supervisor",
        "contact_key": "citizen_relations_contact",
    },
    "other": {
        "department": "general_inquiry_dept",
        "sla_hours": 72,
        "escalation_path": None,
        "contact_key": "general_inquiry_contact",
    },
}

# The catch-all every directory must be able to fall back to.
_FALLBACK_CATEGORY = "other"


class DepartmentDirectoryService:
    """Read-only view of the department directory in force for one invocation.

    Constructed with the effective table (deployment- or caller-supplied) or
    with none, in which case the built-in default directory applies.
    """

    def __init__(self, table: Optional[dict[str, Any]] = None) -> None:
        supplied = isinstance(table, dict) and bool(table)
        source = table if supplied and table is not None else _DEFAULT_DEPARTMENT_TABLE
        # Copy: the directory is read-only for the lifetime of the invocation.
        self._table: dict[str, Any] = dict(source)
        self._is_default = not supplied

    @property
    def is_default_directory(self) -> bool:
        """True when the built-in directory is in use (nothing was supplied)."""
        return self._is_default

    def lookup(self, category: str) -> dict[str, Any]:
        """Return the directory entry for a category, normalised.

        Falls back to the directory's own catch-all entry, then to the built-in
        catch-all, so a partial directory can never leave an inquiry unrouted.
        Every field of the returned entry is guaranteed present and of the right
        type: a directory declared in config/config.yaml is operator-written and
        is not required to be complete, and a missing or wrong-typed field must
        degrade to the catch-all's value rather than fail the invocation. The
        returned dict is a fresh copy — callers may adjust it freely.
        """
        entry = self._table.get(category) or self._table.get(_FALLBACK_CATEGORY)
        if not isinstance(entry, dict):
            entry = {}
        catch_all = _DEFAULT_DEPARTMENT_TABLE[_FALLBACK_CATEGORY]

        department = entry.get("department")
        contact_key = entry.get("contact_key")
        sla_hours = entry.get("sla_hours")
        escalation_path = entry.get("escalation_path")
        return {
            "department": department if isinstance(department, str) and department else catch_all["department"],
            "sla_hours": (
                sla_hours if isinstance(sla_hours, int) and not isinstance(sla_hours, bool) else catch_all["sla_hours"]
            ),
            "escalation_path": escalation_path if isinstance(escalation_path, str) and escalation_path else None,
            "contact_key": contact_key if isinstance(contact_key, str) and contact_key else catch_all["contact_key"],
        }
