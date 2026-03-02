# Collections

Store Postman/Newman collections here so they can be versioned and updated alongside the API contract monitoring.

## Structure

```
collections/
  vera-vhe.json      # Postman collection for VERA VHE (Verhuren Eenheden)
  vera-bdo.json      # Postman collection for VERA BDO
  ...
```

## Usage

- Add existing collections as JSON files (Postman export format).
- Collections can be run with Newman for smoke tests (future phase).
- Update collections when APIs change; keep them in sync with usage maps.

## Naming convention

`{product}-{api}.json` (e.g. `vera-vhe.json`, `vera-bdo.json`)
