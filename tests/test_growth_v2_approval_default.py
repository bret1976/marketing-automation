"""Growth V2 must stage Autopilot for review when require_autopilot_approval is missing."""

import growth_v2 as g


def test_approval_required_defaults_true_when_missing():
    assert g.approval_required(None) is True
    assert g.approval_required({}) is True
    assert g.approval_required({"require_autopilot_approval": None}) is True


def test_approval_required_honors_explicit_values():
    assert g.approval_required({"require_autopilot_approval": True}) is True
    assert g.approval_required({"require_autopilot_approval": False}) is False
    assert g.approval_required({"require_autopilot_approval": "false"}) is False
    assert g.approval_required({"require_autopilot_approval": "true"}) is True
