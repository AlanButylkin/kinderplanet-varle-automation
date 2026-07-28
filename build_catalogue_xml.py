#!/usr/bin/env python3
"""Build a complete VARLE Marketplace XML catalogue from the XLSX workbook.

Every non-empty workbook value is represented in the XML:

* VARLE-defined fields use their dedicated XML nodes.
* Product parameters and remaining source fields use ``<attributes>``.
* ``ean`` is the preferred barcode; otherwise ``ean3`` is used.
* ``ean2`` is research input only and is never exported.
* ``ean``, ``ean2`` and ``ean3`` are never duplicated as attributes.
* Rows with a blank price, blank quantity, or no image are omitted.

The output follows VARLE's recommended structure without a category tree.
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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


DELIVERY_TEXT = "2-3 d. d."
IMAGE_HEADER_RE = re.compile(r"image_\d+")
BARCODE_HEADERS = {"ean", "ean2", "ean3"}
CORE_HEADERS = {
    "product_code",
    "link",
    "category_path_LT",
    "name_LT",
    "description_LT",
    "price_EUR_incl_vat",
    "quantity",
    "weight_kg",
    "parent_code",
    *BARCODE_HEADERS,
}
ATTRIBUTE_TITLE_OVERRIDES = {
    "tax_pct": "PVM, %",
    "prime_cost_EUR": "Savikaina, EUR",
    "price_EUR_excl_vat": "Kaina be PVM, EUR",
    "old_price_EUR_excl_vat": "Sena kaina be PVM, EUR",
    "quantity_unit": "Kiekio vienetas",
    "has_options": "Turi variantų",
    "options_attribute_names": "Variantų atributai",
    "image_count": "Nuotraukų skaičius",
    "width": "Plotis",
    "height": "Aukštis",
    "length": "Ilgis",
    "color": "Spalva",
    "attr_manufacturer": "Gamintojas",
    "attr_brand": "Prekės ženklas",
    "attr_model_name": "Modelio pavadinimas",
}
MANUFACTURER_HEADERS = ("Gamintojas", "attr_manufacturer", "Prekės ženklas", "attr_brand")
MODEL_HEADERS = ("Modelio pavadinimas", "attr_model_name")


@dataclass(frozen=True)
class ProductRecord:
    sheet: str
    row: int
    data: dict[str, Any]
    category: str
    barcode: str
    barcode_source: str
    images: tuple[str, ...]
    attributes: tuple[tuple[str, str, str], ...]
    include: bool
    exclusion_reasons: tuple[str, ...]

    @property
    def product_id(self) -> str:
        return clean(self.data.get("product_code"))


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Taip" if value else "Ne"
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)


def xml_escape(value: Any) -> str:
    return (
        clean(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def cdata(value: Any) -> str:
    return "<![CDATA[" + clean(value).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def format_decimal(value: Any) -> str:
    text = clean(value)
    if not text:
        return ""
    try:
        number = Decimal(text.replace(",", "."))
    except InvalidOperation:
        return text
    rendered = format(number.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def select_barcode(ean: Any, ean2: Any, ean3: Any) -> tuple[str, str]:
    primary = clean(ean)
    researched = clean(ean3)
    if primary:
        return primary, "ean"
    if researched:
        return researched, "ean3_from_ean2" if clean(ean2) else "ean3"
    return "", "blank"


def valid_gtin(value: str) -> bool:
    if not value.isdigit() or len(value) not in (8, 12, 13, 14):
        return False
    digits = [int(character) for character in value]
    weighted_sum = sum(
        digit * (3 if index % 2 == 0 else 1)
        for index, digit in enumerate(digits[-2::-1])
    )
    return (10 - weighted_sum % 10) % 10 == digits[-1]


def first_value(data: dict[str, Any], headers: Iterable[str]) -> str:
    for header in headers:
        value = clean(data.get(header))
        if value:
            return value
    return ""


def category_fallback(source_category: str) -> str:
    return re.sub(r"\s*/\s*", " -> ", clean(source_category))


def read_category_map(workbook: Any) -> dict[str, str]:
    if "xml" not in workbook.sheetnames:
        return {}
    worksheet = workbook["xml"]
    rows = list(worksheet.iter_rows(values_only=True))
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if row and clean(row[0]) == "Source category_path_LT"
        ),
        None,
    )
    if header_index is None:
        return {}

    mapping: dict[str, str] = {}
    for row in rows[header_index + 1 :]:
        source = clean(row[0] if len(row) > 0 else None)
        count = clean(row[1] if len(row) > 1 else None)
        target = clean(row[3] if len(row) > 3 else None)
        if not source or not count.replace(".", "", 1).isdigit():
            break
        if not target or target.lower().startswith("manual review"):
            target = category_fallback(source)
        mapping[source] = target
    return mapping


def attribute_title(column: str) -> str:
    if column in ATTRIBUTE_TITLE_OVERRIDES:
        return ATTRIBUTE_TITLE_OVERRIDES[column]
    if column.startswith("attr_"):
        return column[5:].replace("_", " ").title()
    return column


def product_attributes(data: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """Return (source column, VARLE title, value) for every remaining field."""
    attributes: list[tuple[str, str, str]] = []
    for column, raw_value in data.items():
        value = clean(raw_value)
        if not value:
            continue
        if column in CORE_HEADERS or IMAGE_HEADER_RE.fullmatch(column):
            continue
        attributes.append((column, attribute_title(column), value))

    # Manufacturer/brand/model are valid product parameters as well as useful
    # top-level VARLE nodes, so keep their original workbook values here.
    for column in (*MANUFACTURER_HEADERS, *MODEL_HEADERS):
        value = clean(data.get(column))
        candidate = (column, attribute_title(column), value)
        if value and candidate not in attributes:
            attributes.append(candidate)
    return tuple(attributes)


def load_workbook_records(path: Path) -> tuple[list[ProductRecord], dict[str, Any]]:
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    category_map = read_category_map(workbook)
    records: list[ProductRecord] = []
    sheet_rows: Counter[str] = Counter()
    barcode_sources: Counter[str] = Counter()
    exclusion_reasons: Counter[str] = Counter()
    input_nonempty_cells = 0
    represented_cells = 0

    for worksheet in workbook.worksheets:
        if worksheet.title == "xml":
            continue
        rows = worksheet.iter_rows(values_only=True)
        headers = [clean(value) for value in next(rows)]
        if "product_code" not in headers:
            raise RuntimeError(f"{worksheet.title} has no product_code column")
        image_headers = [header for header in headers if IMAGE_HEADER_RE.fullmatch(header)]

        for row_number, row in enumerate(rows, start=2):
            if not any(clean(value) for value in row):
                continue
            data = {
                header: row[index] if index < len(row) else None
                for index, header in enumerate(headers)
                if header
            }
            sheet_rows[worksheet.title] += 1
            source_category = clean(data.get("category_path_LT"))
            category = category_map.get(source_category, category_fallback(source_category))
            barcode, barcode_source = select_barcode(
                data.get("ean"), data.get("ean2"), data.get("ean3")
            )
            images = tuple(
                clean(data.get(header)) for header in image_headers if clean(data.get(header))
            )
            reasons: list[str] = []
            if not clean(data.get("name_LT")):
                reasons.append("blank_title")
            if not clean(data.get("description_LT")):
                reasons.append("blank_description")
            if not category:
                reasons.append("blank_category")
            if not clean(data.get("price_EUR_incl_vat")):
                reasons.append("blank_price")
            if not clean(data.get("quantity")):
                reasons.append("blank_quantity")
            if not images:
                reasons.append("no_images")
            if not barcode:
                reasons.append("blank_barcode")

            attributes = product_attributes(data)
            record = ProductRecord(
                sheet=worksheet.title,
                row=row_number,
                data=data,
                category=category,
                barcode=barcode,
                barcode_source=barcode_source,
                images=images,
                attributes=attributes,
                include=not reasons,
                exclusion_reasons=tuple(reasons),
            )
            records.append(record)
            barcode_sources[barcode_source] += 1
            exclusion_reasons.update(reasons)

            nonempty_columns = {column for column, value in data.items() if clean(value)}
            input_nonempty_cells += len(nonempty_columns)
            # Every non-empty column is represented once, except the three EAN
            # helper columns which collectively produce one barcode node.
            represented_cells += len(nonempty_columns - BARCODE_HEADERS)
            if nonempty_columns & BARCODE_HEADERS:
                represented_cells += 1

    product_ids = [record.product_id for record in records]
    duplicates = {
        product_id: count
        for product_id, count in Counter(product_ids).items()
        if product_id and count > 1
    }
    if duplicates:
        raise RuntimeError(f"Workbook contains duplicate product IDs: {duplicates}")
    if any(not product_id for product_id in product_ids):
        raise RuntimeError("Workbook contains a blank product ID")

    return records, {
        "source_rows": len(records),
        "source_unique_ids": len(set(product_ids)),
        "sheet_rows": dict(sheet_rows),
        "barcode_sources": dict(barcode_sources),
        "exclusion_reasons": dict(exclusion_reasons),
        "excluded_products": sum(not record.include for record in records),
        "expected_products": sum(record.include for record in records),
        "input_nonempty_cells": input_nonempty_cells,
        "represented_cells_after_ean_collapse": represented_cells,
    }


def write_element(
    lines: list[str],
    level: int,
    tag: str,
    value: Any,
    *,
    use_cdata: bool = False,
) -> None:
    body = cdata(value) if use_cdata else xml_escape(value)
    lines.append(f"{'  ' * level}<{tag}>{body}</{tag}>")


def build_xml(
    workbook_path: Path,
    output_path: Path,
    report_path: Path,
    delivery_text: str = DELIVERY_TEXT,
) -> dict[str, Any]:
    records, workbook_stats = load_workbook_records(workbook_path)
    included = [record for record in records if record.include]
    invalid_barcodes = [
        record.barcode for record in included if not valid_gtin(record.barcode)
    ]
    duplicate_barcodes = {
        barcode: count
        for barcode, count in Counter(record.barcode for record in included).items()
        if count > 1
    }
    if invalid_barcodes:
        raise RuntimeError(f"Invalid output GTIN values: {invalid_barcodes[:25]}")
    if duplicate_barcodes:
        raise RuntimeError(
            f"Duplicate output barcodes: {dict(list(duplicate_barcodes.items())[:25])}"
        )

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<root>", "  <products>"]
    attribute_nodes = 0
    attribute_source_columns: Counter[str] = Counter()

    for record in included:
        data = record.data
        price = format_decimal(data.get("price_EUR_incl_vat"))
        quantity = format_decimal(data.get("quantity"))
        manufacturer = first_value(data, MANUFACTURER_HEADERS)
        model = first_value(data, MODEL_HEADERS)
        weight = format_decimal(data.get("weight_kg"))
        parent_code = clean(data.get("parent_code"))

        lines.append("    <product>")
        write_element(lines, 3, "id", record.product_id)
        lines.append("      <categories>")
        write_element(lines, 4, "category", record.category, use_cdata=True)
        lines.append("      </categories>")
        write_element(lines, 3, "title", data.get("name_LT"), use_cdata=True)
        write_element(lines, 3, "description", data.get("description_LT"), use_cdata=True)
        write_element(lines, 3, "price", price)
        write_element(lines, 3, "delivery_text", delivery_text)
        lines.append("      <images>")
        for image in record.images:
            write_element(lines, 4, "image", image, use_cdata=True)
        lines.append("      </images>")
        write_element(lines, 3, "quantity", quantity)
        write_element(lines, 3, "barcode_format", "EAN")
        write_element(lines, 3, "barcode", record.barcode)

        if model:
            write_element(lines, 3, "model", model, use_cdata=True)
        if weight:
            write_element(lines, 3, "weight", weight)
        write_element(lines, 3, "manufacturer", manufacturer, use_cdata=True)

        lines.append("      <attributes>")
        for source_column, title, value in record.attributes:
            lines.append(
                f'        <attribute title="{xml_escape(title)}">{cdata(value)}</attribute>'
            )
            attribute_nodes += 1
            attribute_source_columns[source_column] += 1
        lines.append("      </attributes>")

        if parent_code:
            write_element(lines, 3, "group", parent_code, use_cdata=True)
        if clean(data.get("link")):
            write_element(lines, 3, "url", data.get("link"), use_cdata=True)
        write_element(lines, 3, "product_with_gift", "False")
        write_element(lines, 3, "all_terminals_allowed", "true")
        lines.append("    </product>")

    lines.extend(["  </products>", "</root>", ""])
    xml_bytes = "\n".join(lines).encode("utf-8")
    ET.fromstring(xml_bytes)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(xml_bytes)
    report = {
        "mode": "complete_workbook_to_varle_xml",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "workbook": str(workbook_path),
        "output_xml": str(output_path),
        "output_products": len(included),
        "excluded_products": len(records) - len(included),
        "attribute_nodes": attribute_nodes,
        "attribute_source_columns": dict(attribute_source_columns),
        "blank_output_barcodes": 0,
        "invalid_output_barcodes": 0,
        "duplicate_output_barcodes": {},
        "ean_attribute_nodes": 0,
        "output_sha256": hashlib.sha256(xml_bytes).hexdigest(),
        "output_bytes": len(xml_bytes),
        "workbook_stats": workbook_stats,
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
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--delivery-text", default=DELIVERY_TEXT)
    args = parser.parse_args()
    report = build_xml(
        args.workbook.resolve(),
        args.output.resolve(),
        args.report.resolve(),
        args.delivery_text,
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
