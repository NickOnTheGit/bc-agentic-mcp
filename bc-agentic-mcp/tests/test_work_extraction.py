"""Tests for the deterministic work router/extractor (planner generalization)."""
from bc_agentic_mcp import work_extraction as wx


_PBI_239597 = (
    "1. Add a new field to table VeraSpaceDetailTypeFDN (id 11024288): name 'Facility Code Filter', "
    "type Code[250], NotEditable. "
    "2. Show the new field on page VeraSpaceDetailTypesFDN (id 11024487). "
    "3. Deliver a data upgrade codeunit that populates the field for existing records."
)

_API_ITEM = (
    "Extend the API EmpRentalMutation so external apps can read and update the on-hold attributes. "
    "The API page EmpRentalMutationv20OPN exposes rentalMutations."
)

# The RAW, un-sanitized item bundle as write_spec actually feeds it (description prose +
# "Ad 1./2./3." headings). This is what regressed to a phantom `table Ad`.
_PBI_239597_RAW = (
    "As admin I want to see for each VERA Space Detail Type which Facility Codes can be related, "
    "so that I know which facilities might be added to which spaces. "
    "For this reason we should perform the following changes: "
    "Add a new field to table VeraSpaceDetailTypeFDN "
    "Show new field on page VeraSpaceDetailTypesFDN "
    "Deliver a data upgrade codeunit that fills in the new field for all existing records in the table "
    "Ad 1. Add a new field to table VeraSpaceDetailTypeFDN Properties of the new field: "
    "Type Code[250] Not Editable "
    "Ad 2. Show new field on page VeraSpaceDetailTypesFDN "
    "Ad 3. Deliver a dataupgradecodeunit that fills in the new field for all existing records in the table"
)


def test_classify_table_field_page_upgrade():
    types = wx.classify(_PBI_239597)
    assert "table-field" in types
    assert "page" in types
    assert "upgrade" in types
    assert "api" not in types  # this item is NOT an API change


def test_classify_api_item():
    types = wx.classify(_API_ITEM)
    assert types[0] == "api" or "api" in types


def test_extract_objects_table_page_codeunit():
    objs = wx.extract_objects(_PBI_239597)
    kinds = {(o["kind"], o["name"]) for o in objs}
    assert ("table", "VeraSpaceDetailTypeFDN") in kinds
    assert ("page", "VeraSpaceDetailTypesFDN") in kinds
    assert any(o["kind"] == "codeunit" for o in objs)
    # table/page are modified (existing), codeunit is created (delivered)
    table = next(o for o in objs if o["kind"] == "table")
    assert table["action"] == "modify"
    assert table["id"] == "11024288"
    cu = next(o for o in objs if o["kind"] == "codeunit")
    assert cu["action"] == "create"


def test_extract_fields_type_and_editable():
    fields = wx.extract_fields(_PBI_239597)
    assert len(fields) == 1
    f = fields[0]
    assert f["name"] == "Facility Code Filter"
    assert f["al_type"] == "Code[250]"
    assert f["editable"] is False


def test_extract_fields_editable_default_true():
    fields = wx.extract_fields("Add field name 'Remark', type Text[250].")
    assert fields[0]["editable"] is True


def test_summarize_shape():
    s = wx.summarize(_PBI_239597)
    assert set(s.keys()) == {"work_types", "objects", "fields"}
    assert s["work_types"] and s["objects"] and s["fields"]


def test_no_domain_literals_leak():
    # A generic item must not be forced into api/rentalMutation classification.
    types = wx.classify("Add a new field to table CustomerFDN: name 'Loyalty Points', type Integer.")
    assert types == ["table-field"] or ("table-field" in types and "api" not in types)


def test_unknown_when_nothing_matches():
    assert wx.classify("Please review the documentation.") == ["unknown"]


def test_extract_ignores_prose_object_phrase():
    # "Extend table VERA Space Detail Type" must NOT yield a phantom `table VERA`.
    objs = wx.extract_objects("Extend table VERA Space Detail Type with a filter. "
                              "Add field to table VeraSpaceDetailTypeFDN (id 11024288).")
    names = {o["name"] for o in objs if o["kind"] == "table"}
    assert "VERA" not in names
    assert "VeraSpaceDetailTypeFDN" in names


def test_extract_objects_from_raw_prose_no_phantom_ad():
    # Regression for PBI 239597: the common noun "table" in "...records in the table"
    # followed by the "Ad 1." heading must NOT be captured as a phantom `table Ad`,
    # and the real objects must still resolve out of the messy prose.
    objs = wx.extract_objects(_PBI_239597_RAW)
    names = {(o["kind"], o["name"]) for o in objs}
    assert ("table", "Ad") not in names
    assert all(o.get("name") not in ("Ad", "the", "The", "Properties") for o in objs)
    assert ("table", "VeraSpaceDetailTypeFDN") in names
    assert ("page", "VeraSpaceDetailTypesFDN") in names
    assert any(o["kind"] == "codeunit" for o in objs)
    table = next(o for o in objs if o["kind"] == "table")
    assert table["action"] == "modify"


def test_extract_fields_from_captions_type_phrasing():
    # BC PBI phrasing (Captions/ENU/Type), not the inline `name 'X' type Y` form.
    text = (
        "Add a new field to table VeraSpaceDetailTypeFDN\n"
        "Captions:\n"
        "ENU: Facility Code Filter\n"
        "NLD: Voorzieningcodefilter\n"
        "Tooltips:\n"
        "ENU: Shows the default filter on the facility code applied to spaces.\n"
        "Type: Text Code[250]\n"
        "Not Editable\n"
    )
    fields = wx.extract_fields(text)
    assert len(fields) == 1
    assert fields[0]["name"] == "Facility Code Filter"
    assert fields[0]["al_type"] == "Code[250]"
    assert fields[0]["editable"] is False


def test_extract_simple_named_object_with_id():
    # A simple (non-affixed, no internal capital) name is accepted on its explicit (id N).
    objs = wx.extract_objects("Add field MutationDate to table Rental (id 50000)")
    tables = [o for o in objs if o["kind"] == "table"]
    assert tables and tables[0]["name"] == "Rental"
    assert tables[0]["id"] == "50000"
    assert tables[0]["action"] == "modify"
