# CodeCop & AppSourceCop Rules

## Mandatory CodeCop Rules (Enabled)

| Rule ID    | Title                                        | Severity |
|------------|----------------------------------------------|----------|
| AA0005     | The variable name should have a suffix       | Warning  |
| AA0006     | Do not use begin/end in AL code             | Warning  |
| AA0010     | Do not use FIND('-') or FIND('=')           | Warning  |
| AA0012     | Do not use a subordinate form               | Warning  |
| AA0013     | Do not use Action for navigation            | Warning  |
| AA0014     | Indentation should be 4 spaces per level     | Warning  |
| AA0015     | Do not use OnRun trigger                    | Warning  |
| AA0016     | Do not use OnInsert without handling         | Warning  |
| AA0017     | Do not use OnModify without handling         | Warning  |
| AA0018     | Do not use OnDelete without handling         | Warning  |
| AA0019     | Do not use OnAfterValidate without calling   | Warning  |
| AA0020     | Do not add code to the Report object         | Warning  |
| AA0021     | Do not use the OnAfterGetRecord trigger      | Warning  |
| AA0022     | Do not use the OnPreReport trigger           | Warning  |
| AA0023     | Do not use the OnPostReport trigger          | Warning  |
| AA0024     | Do not use the OnAfterGetRecord trigger      | Warning  |
| AA0025     | Do not use the OnPreReport trigger           | Warning  |
| AA0026     | Do not use the OnPostReport trigger          | Warning  |
| AA0027     | Do not use the OnAfterGetRecord trigger      | Warning  |
| AA0028     | Do not use the OnPreReport trigger           | Warning  |
| AA0030     | The field should have a data classification  | Error   |
| AA0040     | The extension object should have a name     | Warning  |
| AA0050     | The variable name is misspelled              | Warning  |
| AA0060     | The function name is misspelled              | Warning  |
| AA0070     | The trigger name is misspelled               | Warning  |
| AA0080     | The event name is misspelled                 | Warning  |
| AA0090     | The parameter name is misspelled             | Warning  |
| AA0100     | The text constant should be used             | Warning  |
| AA0110     | The text constant is too long                | Warning  |
| AA0120     | The code should not contain TODO             | Warning  |
| AA0130     | The code should not contain FIXME            | Warning  |
| AA0131     | The code should not contain HACK             | Warning  |
| AA0132     | The code should not contain XXX              | Warning  |
| AA0140     | The code should not contain commented code   | Warning  |
| AA0150     | The code should not contain empty sections   | Warning  |
| AA0160     | The code should not contain empty methods     | Warning  |
| AA0170     | The method should have a comment             | Warning  |
| AA0180     | The trigger should have a comment            | Warning  |
| AA0190     | The event subscriber should have a comment   | Warning  |
| AA0200     | The variable should be assigned a value       | Warning  |
| AA0201     | The variable should be used                   | Warning  |
| AA0210     | Inconsistent casing                          | Warning  |
| AA0215     | The method should be marked as local         | Warning  |
| AA0216     | The variable should be marked as local       | Warning  |
| AA0217     | The trigger should be marked as local        | Warning  |

## Opted-Out CodeCop Rules

| Rule ID    | Reason                                         |
|------------|-------------------------------------------------|
| AA0045     | Allows repetitive field names in extensions    |
| AA0075     | Regional spelling variations are intentional   |
| AA0139     | Temp tables may have lowercase names           |
| AA0220     | Event publisher parameters may differ           |
| AA0221     | Event subscriber parameter naming may not match |

## AppSourceCop Rules (Mandatory)

| Rule ID    | Title                                              |
|------------|----------------------------------------------------|
| AS0001     | The extension name must not change                 |
| AS0002     | The extension publisher must not change            |
| AS0003     | The extension version must increase                |
| AS0004     | The table field properties must not be changed     |
| AS0005     | The table field must not be deleted                |
| AS0006     | The table field must not be renamed                |

## AS000X Series (AppSource Validation)

| Rule ID    | Title                                                    |
|------------|----------------------------------------------------------|
| AS0007     | The table key must not be deleted                        |
| AS0008     | The table key must not be renamed                        |
| AS0009     | The page field must not be deleted                       |
| AS0010     | The page field must not be renamed                       |
| AS0011     | The enum value must not be deleted                       |
| AS0012     | The enum value must not be renamed                       |
| AS0013     | The codeunit interface must not be changed               |
| AS0014     | The control add-in must not be removed                   |
| AS0015     | The profile must not be removed                          |
| AS0016     | The permission set must not be removed                   |
| AS0017     | The table extension field must not be deleted            |
| AS0018     | The page extension field must not be deleted             |
