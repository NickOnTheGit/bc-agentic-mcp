# TDD Template Sections

When writing a TDD document, include the following sections in order:

## 1. Overview

High-level summary of the feature:
- Problem statement (2-3 sentences)
- Business value
- Key stakeholders
- Dependencies on other features/modules

## 2. Proposed Solution (Objects Table)

List every AL object that will be created or modified:

| Object Type   | Object Name                  | Action | Description                    |
|---------------|------------------------------|--------|--------------------------------|
| Table         | `MyFeatureBuffer`            | New    | Temporary storage for feature  |
| Page          | `MyFeatureSetup`             | New    | Setup page for configuration   |
| Codeunit      | `MyFeatureManagement`        | New    | Core business logic            |
| Table         | `ExistingTable`              | Modify | Add fields X, Y, Z            |
| Enum          | `MyFeatureStatus`            | New    | Status values for feature      |

## 3. Data Model

For each new or modified table:
- Field list with name, type, length, and description
- Primary key definition
- Unique indexes and SIFT keys
- Relationships (TableRelation)

## 4. Business Logic

For each codeunit:
- Purpose and responsibility
- Key procedures with signature, parameters, and return values
- Error conditions and expected error messages
- Event publishers (OnBefore/OnAfter pattern)

## 5. Security

- Permissions required (table/data/security filter)
- Permissionset objects to create/modify
- Entitlement considerations

## 6. Integration

- External system touch points
- Web service endpoints (if any)
- Event bus integration
- File format specifications

## 7. AppSource Items

- Extension objects listing
- Dependencies on other apps
- Features in app.json

## 8. Test Automation

- Test codeunits needed
- Key test scenarios with [GIVEN]/[WHEN]/[THEN]
- Library methods to create/extend
