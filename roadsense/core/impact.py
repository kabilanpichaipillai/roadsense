"""What the pipeline saved the road authority, from counts it already keeps.

Conservative on purpose: only work that demonstrably did not reach a person is counted. A merged duplicate or a
suppressed re-sighting is a 311 intake that never happened; a provincial-road report sent straight to the province is
one the city did not have to receive and redirect. Assumptions are settings so a city can put in its own numbers,
and the defaults are the low end of published figures (sources in docs/business-case.md).
"""
from __future__ import annotations

PROVINCE = "Nova Scotia Public Works"


def estimate(stats: dict, cost_per_contact: float, minutes_per_contact: float) -> dict:
    duplicates = int(stats.get("duplicates_merged", 0)) + int(stats.get("suppressed_after_rejection", 0))
    rerouted = int((stats.get("authorities") or {}).get(PROVINCE, 0))
    contacts = duplicates + rerouted
    return {
        "duplicate_contacts_avoided": duplicates,
        "routed_to_province": rerouted,
        "false_alarms_filtered": int(stats.get("false_alarms_removed", 0)),
        "contacts_avoided": contacts,
        "staff_minutes_saved": round(contacts * minutes_per_contact, 1),
        "intake_cost_avoided": round(contacts * cost_per_contact, 2),
        "assumptions": {"cost_per_contact": cost_per_contact, "minutes_per_contact": minutes_per_contact},
    }
