# Batch JSON to Excel

Two reusable exporters with no batch-specific paths or TeX parsing:

- `build_workbook.py`: four category sheets, one row per object.
- `build_hierarchy.py`: one hierarchy sheet, merged parent cells.

Requires Python 3.10+ and `openpyxl`. Keep `common.py` and `temporal.py` beside both scripts
when moving them. No Chinese field mapping or intermediate extraction files
are needed.

## Usage and Path Configuration

```bash
python -m pip install -r /path/to/batch_excel/requirements.txt
python /path/to/batch_excel/build_workbook.py /path/to/batch.json --output-dir /path/to/output
python /path/to/batch_excel/build_hierarchy.py /path/to/batch.json --output-dir /path/to/output
```

Output filenames are `<batch>_categories.xlsx` and `<batch>_hierarchy.xlsx`,
using the JSON's batch identifier. Omit `--output-dir` to write beside the JSON.
Existing files are protected: pass `--overwrite` to explicitly replace them.

Alternatively configure EACH script near its top and run without arguments:

```python
INPUT_JSON = "/absolute/path/to/batch.json"
OUTPUT_DIR = "/absolute/path/to/output"  # None uses the JSON directory.
```

Command-line arguments override configuration. Relative paths resolve against
the current working directory, so absolute configuration paths are recommended.

## JSON Contract

```json
{
  "batch": "BATCH001",
  "process": [{
    "subprocess": "Production",
    "step": [{
          "id": "S001",
          "stage": "Preparation",
          "form": "Confirmation form",
          "personnel": [{
            "id": "O00001",
            "name": "Operator name",
            "attributes": {"role": "operator", "date": "2025-09-02"}
          }],
          "materials": [],
          "equipment": [{
            "id": "O00002",
            "name": "Instrument",
            "attributes": {"equipment_id": "00123", "custom_property": "value"}
          }],
          "environment": []
    }]
  }]
}
```

Structural keys and hierarchy arrays are required. Category arrays may be
omitted (treated as empty). Names may be empty; attribute maps may be empty.
Values and names may use any language. Attribute keys use English `snake_case`;
values are strings, finite numbers, booleans or null, not nested arrays/objects.
New attribute keys require no code changes. Unknown structural keys, old form
ordinals, duplicate JSON keys and unsupported values produce errors rather
than being silently discarded. Batch identifiers allow letters, digits, `_`,
`.` and `-` for safe filenames.

`subprocess` is the process title, and `form` is a string, not another array.
Step and object IDs must be nonempty and unique within a batch; both exports
retain them as `step_id` and `id`. Legacy nested `name` / `subprocess[]` /
`step[]` / `form[]` inputs remain supported with their original column layout.

## Preservation and Verification

- Both formats deduplicate only exactly equal objects in the SAME form/category.
  Attribute key order does not matter; different roles/dates/values remain.
  Different form occurrences never deduplicate, even when their names match.
- JSON order is preserved and the input is never rewritten. No source columns
  or form ordinals, colors, bold headings or decorative borders are added.
- Attribute names colliding with category sheet headers receive `attribute_`
  prefixes (e.g. `name` versus `attribute_name`), repeated if necessary.
- Leading-zero strings remain text. JSON numbers remain numeric, except integers
  longer than Excel's 15-digit precision, which become text to retain all digits.
- Complete dates and times in date/time/validity fields become native Excel values.
  Dates display as `2026-09-15`; timestamps as `2026-09-15-16:00` or
  `2026-09-15-16:00:00`, preserving original seconds precision, including midnight.
  Clock-only values remain `16:00` or `16:00:00`, without an invented date.
  Partial, invalid and compound values remain text; no missing parts are invented.
- Empty objects/forms remain visible in the hierarchy; categories show objects
  only. Empty categories retain headers. Parent branches with no forms add no
  rows. An entirely empty hierarchy shows the batch identifier.
- Null, absent and empty-string cells all display as blank; retain the JSON
  when that distinction matters.
- All saved cell values are read back before publishing. Failed validation
  does not replace an existing workbook. Excel text/row/column limits are checked.
- Long text remains in cells, but row heights are capped at Excel's limit;
  use the formula bar for text that exceeds that display limit.

## Tests

```bash
python -m pytest -q /path/to/batch_excel/test_exporters.py
```
