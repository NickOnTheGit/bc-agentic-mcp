# AL Naming Conventions & Guidelines

## Object Naming Suffixes

| Object Type   | Suffix       | Example                    |
|---------------|--------------|----------------------------|
| Table         | No suffix    | `Customer`                 |
| Page          | No suffix    | `CustomerList`             |
| Card Page     | `Card`       | `CustomerCard`             |
| List Page     | `List`       | `SalesOrderList`           |
| Document Page | `Document`   | `SalesOrderDocument`       |
| Codeunit      | No suffix    | `SalesOrderManagement`     |
| Enum          | No suffix    | `SalesOrderStatus`         |
| Table Extension | `Ext`      | `CustomerExt`              |
| Page Extension  | `Ext`      | `CustomerListExt`          |
| Enum Extension  | `Ext`      | `SalesOrderStatusExt`      |
| Report        | `Report`     | `CustomerStatementReport`  |
| XMLport       | `XMLport`    | `DataExportXMLport`        |
| Query         | `Query`      | `TopCustomersQuery`        |
| ControlAddIn  | `AddIn`      | `MapControlAddIn`          |
| Profile       | `Profile`    | `SalesManagerProfile`      |
| PageCustomization | `Customization` | `SalesRoleCenterCustomization` |

## Variable Naming

| Category         | Convention                | Example                |
|------------------|---------------------------|------------------------|
| Parameter        | PascalCase                | `CustomerNo: Code[20]` |
| Local variable   | camelCase                 | `salesAmount`          |
| Global variable  | PascalCase                | `SalesHeader`          |
| Temp table       | `Temp` prefix             | `TempSalesLine`        |
| Rec variable     | `Rec` or `<Entity>Rec`    | `CustLedgEntryRec`     |
| Enum value       | PascalCase                | `Approved`             |
| Boolean          | `Is`/`Has`/`Can` prefix   | `IsPosted`             |

## Event Subscriber Patterns

```al
[EventSubscriber(ObjectType::Codeunit, Codeunit::"Sales-Post", 'OnAfterPostSalesDoc', '', SkipOnNotFound, ManualFire)]
local procedure OnAfterPostSalesDoc(var Rec: Record "Sales Header")
begin
    // Extension logic
end;
```

Guidelines:
- Always use `SkipOnNotFound` or `SkipOnNotFoundOnInstall` unless the subscriber MUST run.
- Use `ManualFire` for custom events fired by your own code.
- Use `OnBefore`/`OnAfter` prefixes for event names.
- Group subscribers by business domain in dedicated codeunits.

## Error Handling

```al
// Use proper error messages with field captions
Error(Text001, Rec.FieldCaptions.FieldToValidate());

// Use Confirm for destructive operations
if not Confirm(Text002) then
    exit;

// Log detailed info before Error for diagnostics
LogMessage(Severity::Warning, Text003, Tag);
Error(Text001);
```

- Always define `TextXXX` constants instead of using string literals.
- Write error messages in the user perspective: "You cannot ... because ...".
- Use `Code` unit for reusable error text patterns.
