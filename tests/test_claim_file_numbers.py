#!/usr/bin/env python3
"""Check that the claim files write each printed value as a JSON number that reads again.

A paper can print ".074", "-.90", "+4" or "1,000". The claim register keeps the printed
text of a value, to keep its printed decimals. Covers:
  - each such value is written as a JSON number, and the file reads again with the same
    value and the same printed decimals;
  - a usual value, such as "0.50" or "12", is written as it was printed;
  - a value that JSON cannot write with the same value is refused.

Returns 1 if any case fails.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from schema import inputs  # noqa: E402
from utils import claim_files  # noqa: E402

FAILED: list[str] = []
# printed text, and the JSON text that the claim files write for it
WRITTEN = [(".074", "0.074"), ("-.90", "-0.90"), ("+4", "4"), ("+330", "330"),
           ("+0.152", "0.152"), ("1,000", "1000"), ("007", "7"), ("-0.0", "-0.0"),
           ("0.50", "0.50"), ("12", "12"), ("1.5e3", "1.5e3"), ("3.", "3")]


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def main() -> int:
    """Run the cases."""
    claims = [{"id": f"c{k}", "value": inputs.PrintedFloat(inputs.PrintedFloat.plain(text))}
              for k, (text, _) in enumerate(WRITTEN)]
    text = claim_files.dump({"claims": claims})
    for printed, want in WRITTEN:
        expect(f"{printed!r} is written as {want}", f'"value": {want}\n' in text,
               text[:400])
    try:
        read = json.loads(text, parse_float=inputs.PrintedFloat)["claims"]
    except json.JSONDecodeError as e:
        expect("the written file reads again as JSON", False, str(e))
        read = []
    for (printed, _), claim, back in zip(WRITTEN, claims, read, strict=False):
        expect(f"{printed!r} reads again with its value and printed decimals",
               float(back["value"]) == float(claim["value"])
               and inputs.PrintedFloat.count_printed_decimals(back["value"])
               == inputs.PrintedFloat.count_printed_decimals(claim["value"]),
               repr(back["value"]))
    try:
        claim_files.json_number_text(inputs.PrintedFloat("nan"))
        expect("a value that JSON cannot write is refused", False, "no error")
    except ValueError:
        expect("a value that JSON cannot write is refused", True)
    if FAILED:
        print(f"\n{len(FAILED)} claim file number case(s) failed.")
        return 1
    print("\nAll claim file number cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
