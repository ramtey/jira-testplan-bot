"""A case that needs unusual data has to say what kind of ask that is.

From SK-2325 (2026-09-23): three cases asked Engineering to "supply a property"
for shapes live provider data cannot produce. Establishing that took mining ~86
real parcels — malformed numeric values exist only on ~2010-era listings the
latest-list-date selector excludes, comma-formatted acreage appears nowhere, and
the assessment supplies lot square footage and acreage together or not at all.
Each was written as a data hunt, which sends QA looking for something that
cannot exist and makes the request read as a failure to look hard enough.

The generator's job is to pick the route (see the DATA SHAPE rule in the system
prompt). This module's job is narrower: catch a case that hands the data work to
someone else and never says which route it is. TestOrdinaryDataIsNotFlagged is
the constraint that keeps it useful — a check that fires on every case that
mentions a property teaches the reader to skim past it.
"""
import pytest

from src.app.data_provisioning import (
    flag_unactionable_data_asks,
    inspect_provisioning,
)


def _case(**overrides):
    case = {
        "title": "Import a property and verify the acreage",
        "steps": ["Start an import", "Verify the acreage on the saved file"],
        "expected": "Acreage matches the assessment",
        "surface": "web_ui",
    }
    case.update(overrides)
    return case


class _Plan:
    """The attribute surface `flag_unactionable_data_asks` walks."""

    def __init__(self, **sections):
        self.happy_path = sections.get("happy_path", [])
        self.edge_cases = sections.get("edge_cases", [])
        self.integration_tests = sections.get("integration_tests", [])
        self.needs_spec_cases = sections.get("needs_spec_cases", [])
        self.regression_checklist = sections.get("regression_checklist", [])


class TestOrdinaryDataIsNotFlagged:
    """The scope line. Picking a record with a characteristic is what nearly
    every case in this app does; only deferring the work without a route is the
    defect."""

    def test_a_case_with_no_data_ask_is_clean(self):
        assert inspect_provisioning(_case()) == []

    @pytest.mark.parametrize("step", [
        "Enter a property address that has monthly HOA dues",
        "Use a test account with a brokerage-level permission",
        "Find a listing with an assessment on file",
    ])
    def test_picking_a_record_yourself_is_not_a_deferred_ask(self, step):
        assert inspect_provisioning(_case(steps=[step])) == []


class TestADeferredAskNeedsARoute:
    @pytest.mark.parametrize("step", [
        "Ask Engineering to supply a property whose lot size is non-numeric",
        "Request from the dev team a parcel with comma-formatted acreage",
        "Have a developer seed a record with square footage but no acreage",
        "Engineering to provide a payload with a malformed numeric field",
        "Coordinate with the data team for a parcel in this state",
    ])
    def test_handing_the_work_off_without_a_route_is_flagged(self, step):
        reasons = inspect_provisioning(_case(steps=[step]))
        assert len(reasons) == 1
        assert "no provisioning route" in reasons[0]

    def test_the_ask_is_read_from_test_data_and_preconditions_too(self):
        assert inspect_provisioning(_case(
            steps=["Start an import"],
            test_data="Ask Engineering for a payload with a non-numeric acreage",
        ))
        assert inspect_provisioning(_case(
            steps=["Start an import"],
            preconditions="Engineering has seeded a parcel with no assessment",
        ))

    def test_a_route_answers_the_ask(self):
        """Naming the route is the whole remedy — the ask stays, and becomes
        one the reader can act on."""
        assert inspect_provisioning(_case(
            steps=["Ask Engineering to supply a payload with a non-numeric acreage"],
            surface="backend_http",
            data_shape={
                "shape": "An import payload whose acreage field holds a non-numeric string",
                "provisioning": "stub",
                "evidence": "No such value appears in any sampled parcel",
                "obtain": "Engineering stubs the provider response at the import client",
            },
        )) == []


class TestTheRouteItselfIsChecked:
    @pytest.mark.parametrize("route", [
        "real_record", "real_record_unconfirmed", "stub", "unreachable",
    ])
    def test_every_documented_route_is_accepted(self, route):
        assert inspect_provisioning(_case(
            surface="backend_http",
            data_shape={
                "shape": "A parcel with an assessment but no acreage",
                "provisioning": route,
                "obtain": "Search for one; if none exists, Engineering stubs it",
            },
        )) == []

    @pytest.mark.parametrize("route", [None, "", "fixture", "ask_engineering"])
    def test_an_unrecognised_route_is_flagged(self, route):
        reasons = inspect_provisioning(_case(data_shape={
            "shape": "A parcel with comma-formatted acreage",
            "provisioning": route,
            "obtain": "Find one",
        }))
        assert any("no usable provisioning route" in r for r in reasons)

    def test_unconfirmed_without_a_fallback_is_flagged(self):
        """`real_record_unconfirmed` is the honest answer, but only when it
        says what to do when the search comes up empty — that fallback is a
        different ask to a different person."""
        reasons = inspect_provisioning(_case(data_shape={
            "shape": "A parcel with square footage but no acreage",
            "provisioning": "real_record_unconfirmed",
            "evidence": "Nothing in the ticket shows the fields varying independently",
        }))
        assert any("fallback" in r for r in reasons)

    def test_unreachable_cannot_be_driven_through_the_app(self):
        reasons = inspect_provisioning(_case(
            surface="web_ui",
            data_shape={
                "shape": "An imported record whose acreage is non-numeric",
                "provisioning": "unreachable",
                "evidence": "The importer takes the latest list date; the malformed "
                            "values all sit on superseded older listings",
                "obtain": "Engineering covers it with a stubbed payload",
            },
        ))
        assert any("unreachable" in r for r in reasons)

    def test_unreachable_is_fine_on_a_backend_case(self):
        assert inspect_provisioning(_case(
            surface="backend_http",
            data_shape={
                "shape": "An imported record whose acreage is non-numeric",
                "provisioning": "unreachable",
                "obtain": "Engineering covers it with a stubbed payload",
            },
        )) == []

    def test_a_route_with_no_shape_is_flagged(self):
        reasons = inspect_provisioning(_case(
            surface="backend_http",
            data_shape={"provisioning": "stub", "obtain": "Mock the client"},
        ))
        assert any("without saying what the shape is" in r for r in reasons)


class TestItNeverMovesACase:
    """Section counts are the progress-key fingerprint, so this pass marks in
    place — the same discipline as `citation_integrity`."""

    def test_flags_are_written_onto_the_case(self):
        case = _case(steps=["Ask Engineering to supply a non-numeric acreage"])
        plan = _Plan(happy_path=[case])

        flagged = flag_unactionable_data_asks(plan)

        assert flagged == [case]
        assert case["data_ask_unactionable"] is True
        assert "no provisioning route" in case["data_ask_unactionable_reason"]

    def test_section_counts_are_unchanged(self):
        plan = _Plan(
            happy_path=[_case(steps=["Ask Engineering for a seeded parcel"])],
            edge_cases=[_case(), _case()],
            integration_tests=[_case()],
            regression_checklist=["Existing imports still populate acreage"],
        )

        flag_unactionable_data_asks(plan)

        assert len(plan.happy_path) == 1
        assert len(plan.edge_cases) == 2
        assert len(plan.integration_tests) == 1
        assert plan.regression_checklist == [
            "Existing imports still populate acreage"
        ]

    def test_every_case_section_is_walked(self):
        ask = ["Ask Engineering to seed a parcel with no acreage"]
        plan = _Plan(
            happy_path=[_case(steps=ask)],
            edge_cases=[_case(steps=ask)],
            integration_tests=[_case(steps=ask)],
            needs_spec_cases=[_case(steps=ask)],
        )

        assert len(flag_unactionable_data_asks(plan)) == 4

    def test_a_clean_plan_is_left_untouched(self):
        case = _case()
        flag_unactionable_data_asks(_Plan(happy_path=[case]))
        assert "data_ask_unactionable" not in case

    def test_non_dict_entries_are_skipped(self):
        plan = _Plan(happy_path=["a bare string that should not be here"])
        assert flag_unactionable_data_asks(plan) == []
