# Upgrade Codeunit Patterns

## Required Triggers

Every upgrade codeunit must implement these triggers in order:

```
UpgradeCodeunit <ID> <Name>
{
    // 1. Check preconditions
    Trigger OnCheckPreconditionsPerDatabase()
    begin
        // Verify database state allows upgrade
    end;

    Trigger OnCheckPreconditionsPerCompany()
    begin
        // Verify company state allows upgrade
    end;

    // 2. Upgrade actions
    Trigger OnUpgradePerDatabase()
    begin
        // Schema-level changes (tables, fields)
    end;

    Trigger OnUpgradePerCompany()
    begin
        // Data-level changes per company
    end;

    // 3. Post-upgrade validation
    Trigger OnValidateUpgradePerDatabase()
    begin
        // Verify DB-level upgrade succeeded
    end;

    Trigger OnValidateUpgradePerCompany()
    begin
        // Verify company-level upgrade succeeded
    end;
}
```

## Upgrade Tags with Idempotency

Each upgrade method must use upgrade tags to ensure idempotency:

```al
[UpgradeTag('MYTAG-0001')]
Trigger OnUpgradePerCompany()
begin
    // Check if already applied
    if not UpgradeTag.IsExecuted('MYTAG-0001') then begin
        // Perform upgrade logic
        UpgradeTag.SetExecuted('MYTAG-0001');
    end;
end;
```

Idempotency rules:
1. Always check `UpgradeTag.IsExecuted()` before running upgrade logic.
2. Always call `UpgradeTag.SetExecuted()` after completing upgrade logic.
3. Tag format: `COMPONENT-PADDEDNUMBER` (e.g., `MYEXT-0001`, `MYEXT-0002`).
4. Never reuse or reorder upgrade tags.
5. Never remove an upgrade tag from production code.

## DataTransfer Pattern (>300K Records)

For large data migrations (>300K records), use `DataTransfer`:

```al
local procedure TransferLargeDataSet()
var
    DataTransfer: Codeunit "Data Transfer";
    SourceRec: Record "Source Table";
    TargetRec: Record "Target Table";
    DataTransferError: Record "Data Transfer Error";
begin
    DataTransfer.TransferData(
        DataTransferError,
        SourceRec,
        TargetRec,
        // Use batch processing for large datasets
        BatchSize := 50000
    );
end;
```

- Set `BatchSize` to 50000 for performance.
- Log `DataTransferError` records for audit trail.
- Use `OnTransferRecord` event for custom field mapping.
- For datasets <300K, a simple `INSERT` loop with `Modify`/`Insert` is acceptable.

## InitValue Setting

When adding new fields to existing tables in upgrades, always set `InitValue`:

```al
field(12345; "New Field"; Code[20])
{
    DataClassification = CustomerContent;
    // InitValue ensures existing records get a valid default
    InitValue = 'DEFAULT';
}
```

- Required for all new fields added to existing tables.
- Use a meaningful default that preserves existing behavior.
- Never use `InitValue = ''` for Code fields — the empty string is the implicit default.
- For Boolean fields, prefer `InitValue = false`.
