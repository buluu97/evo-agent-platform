"""Reference script used only in the handwritten acceptance fixtures."""

import csv
import json
import re
import sys


def summarize(input_path):
    result = dict(total_rows=0, valid_rows=0, missing_amount_rows=0,
                  invalid_amount_rows=0, total_amount_cents=0)
    with open(input_path, encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream):
            amount = (row.get('amount_cents') or '').strip()
            result['total_rows'] += 1
            if not amount:
                result['missing_amount_rows'] += 1
            elif re.fullmatch(r'[0-9]+', amount):
                result['valid_rows'] += 1
                result['total_amount_cents'] += int(amount)
            else:
                result['invalid_amount_rows'] += 1
    return result


if __name__ == '__main__':
    with open(sys.argv[2], 'w', encoding='utf-8') as output:
        json.dump(summarize(sys.argv[1]), output)
