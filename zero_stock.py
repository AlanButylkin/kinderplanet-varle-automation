#!/usr/bin/env python3
"""Create an emergency Varle feed with every existing product out of stock.

The XML is edited as text so all content outside the value of each product's
``quantity`` element remains byte-for-byte unchanged.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


PRODUCT_BLOCK_RE = re.compile(r"<product>(?P<body>.*?)</product>", re.DOTALL)
QUANTITY_RE = re.compile(r"(<quantity>)(?P<value>.*?)(</quantity>)", re.DOTALL)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_xml_bytes(data: bytes) -> None:
    parser = ET.XMLPullParser(events=("start", "end"))
    chunk_size = 1024 * 1024
    for offset in range(0, len(data), chunk_size):
        parser.feed(data[offset : offset + chunk_size])
    parser.close()


def zero_stock(
    main_path: Path,
    candidate_path: Path,
    report_path: Path,
    state_path: Path | None = None,
    *,
    minimum_main_products: int = 4000,
) -> dict[str, Any]:
    main_bytes = main_path.read_bytes()
    validate_xml_bytes(main_bytes)
    main_text = main_bytes.decode("utf-8")
    matches = list(PRODUCT_BLOCK_RE.finditer(main_text))
    if len(matches) < minimum_main_products:
        raise RuntimeError(
            f"Main feed has only {len(matches)} products; expected at least "
            f"{minimum_main_products}"
        )

    output_parts: list[str] = []
    cursor = 0
    quantity_changes = 0
    for match in matches:
        output_parts.append(main_text[cursor : match.start()])
        block = match.group(0)
        quantities = list(QUANTITY_RE.finditer(block))
        if len(quantities) != 1:
            raise RuntimeError(
                "Every product must contain exactly one quantity element; "
                f"found {len(quantities)}"
            )
        if quantities[0].group("value").strip() != "0":
            quantity_changes += 1
        zeroed_block, replacements = QUANTITY_RE.subn(r"\g<1>0\g<3>", block, count=1)
        if replacements != 1:
            raise RuntimeError("Failed to replace a product quantity")
        output_parts.append(zeroed_block)
        cursor = match.end()
    output_parts.append(main_text[cursor:])

    candidate_bytes = "".join(output_parts).encode("utf-8")
    validate_xml_bytes(candidate_bytes)

    candidate_text = candidate_bytes.decode("utf-8")
    candidate_matches = list(PRODUCT_BLOCK_RE.finditer(candidate_text))
    if len(candidate_matches) != len(matches):
        raise RuntimeError("Product count changed while creating emergency feed")
    for match in candidate_matches:
        quantity = QUANTITY_RE.search(match.group(0))
        if quantity is None or quantity.group("value").strip() != "0":
            raise RuntimeError("Candidate contains a non-zero or missing quantity")

    # Removing quantity values must leave identical XML on both sides.
    normalized_main = QUANTITY_RE.sub(r"\g<1>\g<3>", main_text)
    normalized_candidate = QUANTITY_RE.sub(r"\g<1>\g<3>", candidate_text)
    if normalized_main != normalized_candidate:
        raise RuntimeError("Emergency feed changed content outside quantity values")

    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_bytes(candidate_bytes)
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    report: dict[str, Any] = {
        "mode": "emergency_zero_stock",
        "generated_at": generated_at,
        "main": str(main_path),
        "candidate": str(candidate_path),
        "main_products": len(matches),
        "changed_products": quantity_changes,
        "quantity_changes": quantity_changes,
        "all_quantities_zero": True,
        "main_sha256": sha256_bytes(main_bytes),
        "candidate_sha256": sha256_bytes(candidate_bytes),
        "main_bytes": len(main_bytes),
        "candidate_bytes": len(candidate_bytes),
        "publish_allowed": True,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if state_path is not None:
        state: dict[str, Any] = {}
        if state_path.exists():
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("State must be a JSON object")
            state = loaded
        state["emergency_zero_stock"] = True
        state["emergency_generated_at"] = generated_at
        state["last_candidate_sha256"] = report["candidate_sha256"]
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set every product quantity in the existing Varle feed to zero"
    )
    parser.add_argument("--main", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--minimum-main-products", type=int, default=4000)
    args = parser.parse_args()
    report = zero_stock(
        args.main.resolve(),
        args.output.resolve(),
        args.report.resolve(),
        args.state.resolve() if args.state else None,
        minimum_main_products=args.minimum_main_products,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
