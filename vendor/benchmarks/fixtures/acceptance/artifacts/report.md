# Order Statistics

## Method

Skip the header and trim whitespace. Count blank amounts as missing.
Amounts made up only of ASCII digits are valid, including zero.
Count all other amounts as invalid and sum valid amounts in integer cents.

## Results

- total_rows: 6
- valid_rows: 4
- missing_amount_rows: 1
- invalid_amount_rows: 1
- total_amount_cents: 2250

## Sources

The Python CSV documentation describes reading tabular data with a header via `DictReader`.
<https://docs.python.org/3/library/csv.html>

## Review

Pass. The Reviewer checked the script against the independent order data.
