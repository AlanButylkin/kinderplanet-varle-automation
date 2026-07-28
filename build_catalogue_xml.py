#!/usr/bin/env python3
"""Apply workbook barcode and required-field rules to a generated Varle XML.

The established Excel exporter remains responsible for the XML field mapping.
This finishing step:

* keeps ``ean`` when it exists;
* otherwise puts ``ean3`` in the XML barcode slot (including rows where
  ``ean3`` was researched from ``ean2``);
* omits rows with blank price, blank quantity, or no actual image URL.

All retained XML content outside ``barcode_format`` and ``barcode`` is copied
byte-for-byte from the established export.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PRODUCT_BLOCK_RE = re.compile(r"<product>(?P<body>.*?)</product>", re.DOTALL)
ID_RE = re.compile(r"<id>(?P<value>.*?)</id>", re.DOTALL)
BARCODE_FORMAT_RE = re.compile(
    r"(<barcode_format>)(?P<value>.*?)(</barcode_format>)", re.DOTALL
)
BARCODE_RE = re.compile(r"(<barcode>)(?P<value>.*?)(</barcode>)", re.DOTALL)
IMAGE_HEADER_RE = re.compile(r"image_\d+")


@dataclass(frozen=True)
class WorkbookRule:
    product_id: str
    barcode: str
    barcode_source: str
    include: bool
    exclusion_reasons: tuple[str, ...]


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def select_barcode(ean: Any, ean2: Any, ean3: Any) -> tuple[str, str]:
    primary = clean(ean)
    researched = clean(ean3)
    if primary:
        return primary, "ean"
    if researched:
        return researched, "ean3_from_ean2" if clean(ean2) else "ean3"
    return "", "blank"


def load_workbook_rules(path: Path) -> tuple[dict[str, WorkbookRule], dict[str, Any]]:
    # Kept local to this function so GitHub's stdlib-only test job can import
    # and test the XML transformation without installing spreadsheet packages.
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    rules: dict[str, WorkbookRule] = {}
    duplicate_ids: dict[str, int] = {}
    barcode_sources: Counter[str] = Counter()
    exclusion_reasons: Counter[str] = Counter()
    source_rows = 0

    for worksheet in workbook.worksheets:
        if worksheet.title == "xml":
            continue
        rows = worksheet.iter_rows(values_only=True)
        headers = [clean(value) for value in next(rows)]
        indexes = {header: index for index, header in enumerate(headers) if header}
        image_headers = [
            header for header in headers if IMAGE_HEADER_RE.fullmatch(header)
        ]

        def get(row: tuple[Any, ...], header: str) -> str:
            index = indexes.get(header)
            if index is None or index >= len(row):
                return ""
            return clean(row[index])

        for row in rows:
            if not any(clean(value) for value in row):
                continue
            source_rows += 1
            product_id = get(row, "product_code")
            barcode, barcode_source = select_barcode(
                get(row, "ean"), get(row, "ean2"), get(row, "ean3")
            )
            reasons: list[str] = []
            if not get(row, "price_EUR_incl_vat"):
                reasons.append("blank_price")
            if not get(row, "quantity"):
                reasons.append("blank_quantity")
            if not any(get(row, header) for header in image_headers):
                reasons.append("no_images")

            if product_id in rules:
                duplicate_ids[product_id] = duplicate_ids.get(product_id, 1) + 1
            rules[product_id] = WorkbookRule(
                product_id=product_id,
                barcode=barcode,
                barcode_source=barcode_source,
                include=not reasons,
                exclusion_reasons=tuple(reasons),
            )
            barcode_sources[barcode_source] += 1
            exclusion_reasons.update(reasons)

    if duplicate_ids:
        raise RuntimeError(f"Workbook contains duplicate product IDs: {duplicate_ids}")
    if "" in rules:
        raise RuntimeError("Workbook contains a blank product ID")
    return rules, {
        "source_rows": source_rows,
        "source_unique_ids": len(rules),
        "barcode_sources": dict(barcode_sources),
        "exclusion_reasons": dict(exclusion_reasons),
        "excluded_products": sum(not rule.include for rule in rules.values()),
        "expected_products": sum(rule.include for rule in rules.values()),
    }


def validate_xml_bytes(data: bytes) -> None:
    parser = ET.XMLPullParser(events=("start", "end"))
    for offset in range(0, len(data), 1024 * 1024):
        parser.feed(data[offset : offset + 1024 * 1024])
    parser.close()


def valid_gtin(value: str) -> bool:
    if not value.isdigit() or len(value) not in (8, 12, 13, 14):
        return False
    digits = [int(character) for character in value]
    reversed_payload = digits[-2::-1]
    weighted_sum = sum(
        digit * (3 if index % 2 == 0 else 1)
        for index, digit in enumerate(reversed_payload)
    )
    return (10 - weighted_sum % 10) % 10 == digits[-1]


def transform_xml(
    source_xml: Path,
    output_xml: Path,
    rules: dict[str, WorkbookRule],
    report_path: Path,
    workbook_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_bytes = source_xml.read_bytes()
    validate_xml_bytes(source_bytes)
    source_text = source_bytes.decode("utf-8")
    output_parts: list[str] = []
    cursor = 0
    source_ids: list[str] = []
    output_ids: list[str] = []
    updated_barcodes = 0

    for match in PRODUCT_BLOCK_RE.finditer(source_text):
        output_parts.append(source_text[cursor : match.start()])
        block = match.group(0)
        id_match = ID_RE.search(block)
        if id_match is None:
            raise RuntimeError("A source XML product has no id element")
        product_id = id_match.group("value").strip()
        source_ids.append(product_id)
        rule = rules.get(product_id)
        if rule is None:
            raise RuntimeError(f"XML product is absent from workbook: {product_id}")
        if rule.include:
            format_value = "EAN" if rule.barcode else ""
            original_barcode = BARCODE_RE.search(block)
            if original_barcode is None:
                raise RuntimeError(f"Product has no barcode element: {product_id}")
            if original_barcode.group("value").strip() != rule.barcode:
                updated_barcodes += 1
            block, format_count = BARCODE_FORMAT_RE.subn(
                rf"\g<1>{format_value}\g<3>", block, count=1
            )
            block, barcode_count = BARCODE_RE.subn(
                rf"\g<1>{rule.barcode}\g<3>", block, count=1
            )
            if format_count != 1 or barcode_count != 1:
                raise RuntimeError(
                    f"Product barcode structure is invalid: {product_id}"
                )
            output_parts.append(block)
            output_ids.append(product_id)
        cursor = match.end()
    output_parts.append(source_text[cursor:])

    if len(source_ids) != len(rules):
        missing = sorted(set(rules) - set(source_ids))
        raise RuntimeError(
            f"Workbook/XML product count mismatch; {len(missing)} workbook IDs "
            f"are absent from source XML"
        )
    candidate_bytes = "".join(output_parts).encode("utf-8")
    validate_xml_bytes(candidate_bytes)
    root = ET.fromstring(candidate_bytes)
    products = root.findall("./products/product")
    parsed_ids = [clean(product.findtext("id")) for product in products]
    if parsed_ids != output_ids:
        raise RuntimeError("Output product order or IDs changed unexpectedly")
    duplicate_output_ids = {
        product_id: count
        for product_id, count in Counter(parsed_ids).items()
        if count > 1
    }
    if duplicate_output_ids:
        raise RuntimeError(f"Output has duplicate IDs: {duplicate_output_ids}")

    barcode_mismatches: list[str] = []
    output_barcodes: list[str] = []
    for product in products:
        product_id = clean(product.findtext("id"))
        barcode = clean(product.findtext("barcode"))
        output_barcodes.append(barcode)
        if barcode != rules[product_id].barcode:
            barcode_mismatches.append(product_id)
    if barcode_mismatches:
        raise RuntimeError(f"Barcode validation failed: {barcode_mismatches[:25]}")
    blank_barcodes = sum(not barcode for barcode in output_barcodes)
    invalid_barcodes = [
        barcode for barcode in output_barcodes if barcode and not valid_gtin(barcode)
    ]
    duplicate_barcodes = {
        barcode: count
        for barcode, count in Counter(output_barcodes).items()
        if barcode and count > 1
    }
    if blank_barcodes:
        raise RuntimeError(f"Output has {blank_barcodes} blank barcodes")
    if invalid_barcodes:
        raise RuntimeError(
            f"Output has invalid GTIN values: {invalid_barcodes[:25]}"
        )
    if duplicate_barcodes:
        raise RuntimeError(
            f"Output has duplicate barcodes: {dict(list(duplicate_barcodes.items())[:25])}"
        )

    output_xml.parent.mkdir(parents=True, exist_ok=True)
    output_xml.write_bytes(candidate_bytes)
    report = {
        "mode": "workbook_filtered_catalogue",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_xml": str(source_xml),
        "output_xml": str(output_xml),
        "source_xml_products": len(source_ids),
        "output_products": len(products),
        "excluded_products": len(source_ids) - len(products),
        "updated_barcodes": updated_barcodes,
        "blank_output_barcodes": blank_barcodes,
        "invalid_output_barcodes": len(invalid_barcodes),
        "duplicate_output_barcodes": duplicate_barcodes,
        "duplicate_output_ids": duplicate_output_ids,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "output_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "output_bytes": len(candidate_bytes),
        "workbook": workbook_stats or {},
        "valid": True,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", required=True, type=Path)
    parser.add_argument("--source-xml", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    rules, workbook_stats = load_workbook_rules(args.workbook.resolve())
    report = transform_xml(
        args.source_xml.resolve(),
        args.output.resolve(),
        rules,
        args.report.resolve(),
        workbook_stats,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
