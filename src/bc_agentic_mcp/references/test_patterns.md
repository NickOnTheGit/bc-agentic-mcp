# AL Test Framework Patterns

## Test Codeunit Structure

```al
codeunit <ID> "<Name>Test"
{
    Subtype = Test;

    [SCENARIO('SCN-001: Description of scenario')]
    [GIVEN('Initial state description')]
    [WHEN('Action performed')]
    [THEN('Expected outcome')]
    local procedure TestScenarioName()
    var
        Library: Codeunit "<Library Name>";
    begin
        // [GIVEN] Setup
        Library.SetupDefaultData();

        // [WHEN] Execute
        Library.ExecuteAction();

        // [THEN] Assert
        Library.AssertExpectedOutcome();
    end;
}
```

## [SCENARIO]/[GIVEN]/[WHEN]/[THEN] Format

| Attribute | Usage                                              | Max Length |
|-----------|----------------------------------------------------|------------|
| SCENARIO  | Unique identifier + human-readable description     | 250 chars  |
| GIVEN     | Preconditions and initial state                    | 250 chars  |
| WHEN      | The action or trigger being tested                 | 250 chars  |
| THEN      | The expected postcondition or assertion            | 250 chars  |

Rules:
- Every test must have at least `[SCENARIO]` and one of `[GIVEN]`/`[WHEN]`/`[THEN]`.
- `SCENARIO` ID format: `SCN-NNN` with leading zeros.
- Descriptions should be business-readable, not implementation details.

## Handler Methods

```al
[EventSubscriber(ObjectType::Table, Database::"Sales Header", 'OnBeforeInsertEvent', '', false, false)]
local procedure HandleSalesHeaderOnBeforeInsert(var Rec: Record "Sales Header")
begin
    // Break test isolation: deny inserts to Sales Header
    Assert().Record(Rec).IsEmpty();
end;

// For confirmation dialogs:
[HandlerFunctions('HandlerConfirm')]
local procedure TestConfirmDialog()
var
    Library: Codeunit "Library - Confirm";
begin
    Library.SetResult(ConfirmMode::Yes);
end;
```

Common handler types:
- `HandlerFunctions` — for Confirm and StrMenu dialogs
- `RequestPageHandler` — for report/XMLport request pages
- `MessageHandler` — for Message statements
- `FilterPageHandler` — for FilterPage builder
- `SendNotificationHandler` — for notifications
- `HyperLinkHandler` — for HyperLink statements

## LibraryAssert Methods

| Method                            | Purpose                              |
|-----------------------------------|--------------------------------------|
| `Assert.AreEqual(Expected, Actual)` | Equality check                      |
| `Assert.AreNotEqual(NotExpected, Actual)` | Inequality check             |
| `Assert.IsTrue(Condition)`        | Boolean true check                   |
| `Assert.IsFalse(Condition)`       | Boolean false check                  |
| `Assert.IsNull(Value)`            | Null check                           |
| `Assert.IsNotNull(Value)`         | Not null check                       |
| `Assert.Record(Rec).IsEmpty()`      | Record is empty (no rows)            |
| `Assert.Record(Rec).IsNotEmpty()`   | Record has rows                      |
| `Assert.Record(Rec).Count()`        | Record count with filter             |
| `Assert.Throws(Codeunit, Method)`   | Verifies error is thrown             |

Prefer `LibraryAssert` over raw `Assert` for test readability.

## Test Naming Convention

Format: `<BusinessEntity>_<Action>_<ExpectedResult>`

Examples:
- `Customer_CalculateBalance_ReturnsCorrectTotal`
- `SalesOrder_Post_InsufficientInventory_Error`
- `Vendor_ApplyEntry_PartialAmount_RemainingOpen`

Best practices:
- Start with the business entity under test.
- Describe the action in past tense.
- End with the expected outcome.
- Use underscores to separate logical segments.
