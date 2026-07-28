#!/usr/bin/env python3
"""Validate complete XLSX-to-VARLE XML field coverage and structure."""

from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from build_catalogue_xml import (
    MANUFACTURER_HEADERS,
    MODEL_HEADERS,
    clean,
    first_value,
    format_decimal,
    load_workbook_records,
)


def validate(workbook_path: Path, xml_path: Path, report_path: Path) -> dict:
    records, workbook_stats = load_workbook_records(workbook_path)
    expected = [record for record in records if record.include]
    expected_by_id = {record.product_id: record for record in expected}
    xml_bytes = xml_path.read_bytes()
    root = ET.fromstring(xml_bytes)
    products = root.findall("./products/product")
    xml_ids = [clean(product.findtext("id")) for product in products]
    duplicate_ids = {
        product_id: count
        for product_id, count in Counter(xml_ids).items()
        if product_id and count > 1
    }

    errors: list[str] = []
    field_checks = 0
    attribute_checks = 0
    if len(products) != len(expected):
        errors.append(f"Product count {len(products)} != {len(expected)}")
    if xml_ids != [record.product_id for record in expected]:
        errors.append("Product IDs or order differ from the eligible workbook rows")
    if duplicate_ids:
        errors.append(f"Duplicate XML IDs: {duplicate_ids}")

    for product in products:
        product_id = clean(product.findtext("id"))
        record = expected_by_id.get(product_id)
        if record is None:
            errors.append(f"Unexpected XML product: {product_id}")
            continue
        data = record.data
        expected_core = {
            "category": record.category,
            "title": clean(data.get("name_LT")),
            "description": clean(data.get("description_LT")),
            "price": format_decimal(data.get("price_EUR_incl_vat")),
            "quantity": format_decimal(data.get("quantity")),
            "barcode_format": "EAN",
            "barcode": record.barcode,
            "model": first_value(data, MODEL_HEADERS),
            "weight": format_decimal(data.get("weight_kg")),
            "manufacturer": first_value(data, MANUFACTURER_HEADERS),
            "group": clean(data.get("parent_code")),
            "url": clean(data.get("link")),
        }
        actual_core = {
            "category": clean(product.findtext("./categories/category")),
            "title": clean(product.findtext("title")),
            "description": clean(product.findtext("description")),
            "price": clean(product.findtext("price")),
            "quantity": clean(product.findtext("quantity")),
            "barcode_format": clean(product.findtext("barcode_format")),
            "barcode": clean(product.findtext("barcode")),
            "model": clean(product.findtext("model")),
            "weight": clean(product.findtext("weight")),
            "manufacturer": clean(product.findtext("manufacturer")),
            "group": clean(product.findtext("group")),
            "url": clean(product.findtext("url")),
        }
        for field, expected_value in expected_core.items():
            field_checks += 1
            if actual_core[field] != expected_value:
                errors.append(
                    f"{product_id}: {field}={actual_core[field]!r}, "
                    f"expected {expected_value!r}"
                )

        actual_images = [clean(image.text) for image in product.findall("./images/image")]
        field_checks += len(record.images)
        if actual_images != list(record.images):
            errors.append(f"{product_id}: image list differs from workbook")

        expected_attributes = [(title, value) for _, title, value in record.attributes]
        actual_attributes = [
            (clean(attribute.get("title")), clean(attribute.text))
            for attribute in product.findall("./attributes/attribute")
        ]
        attribute_checks += len(expected_attributes)
        if actual_attributes != expected_attributes:
            errors.append(f"{product_id}: attribute list differs from workbook")
        if any(title.lower() in {"ean", "ean2", "ean3"} for title, _ in actual_attributes):
            errors.append(f"{product_id}: EAN helper column leaked into attributes")

    required_blank_counts = {
        tag: sum(not clean(product.findtext(tag)) for product in products)
        for tag in ("id", "title", "description", "price", "quantity", "barcode")
    }
    products_without_images = sum(
        not product.findall("./images/image") for product in products
    )
    if any(required_blank_counts.values()):
        errors.append(f"Blank required nodes: {required_blank_counts}")
    if products_without_images:
        errors.append(f"Products without images: {products_without_images}")

    report = {
        "valid": not errors,
        "workbook": str(workbook_path),
        "xml": str(xml_path),
        "xml_sha256": hashlib.sha256(xml_bytes).hexdigest(),
        "xml_bytes": len(xml_bytes),
        "source_rows": workbook_stats["source_rows"],
        "eligible_products": len(expected),
        "xml_products": len(products),
        "excluded_products": workbook_stats["excluded_products"],
        "field_checks": field_checks,
        "attribute_checks": attribute_checks,
        "required_blank_counts": required_blank_counts,
        "products_without_images": products_without_images,
        "duplicate_xml_ids": duplicate_ids,
        "ean_attribute_nodes": sum(
            clean(attribute.get("title")).lower() in {"ean", "ean2", "ean3"}
            for product in products
            for attribute in product.findall("./attributes/attribute")
        ),
        "errors": errors[:100],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if errors:
        raise RuntimeError(f"Catalogue validation failed: {errors[:10]}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", required=True, type=Path)
    parser.add_argument("--xml", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = validate(
        args.workbook.resolve(), args.xml.resolve(), args.report.resolve()
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
