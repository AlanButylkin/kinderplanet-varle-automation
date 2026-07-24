#!/usr/bin/env python3
"""Build an autonomous, historical catalogue view from the Verskis XML.

The tracker keeps a compressed state beside the Varle recovery release.  It
does not alter the public Varle feed; ``sync_varle.py`` remains responsible for
that.  This module records additions, removals, restorations, and field-level
changes and creates Excel/CSV files suitable for monthly inspection.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape, quoteattr


IMAGE_COLUMNS = [f"image_{index}" for index in range(1, 29)]
SCRAPER_COLUMNS = [
    "product_code",
    "link",
    "parent_code",
    "ean",
    "category_path_LT",
    "name_LT",
    "description_LT",
    "tax_pct",
    "prime_cost_EUR",
    "price_EUR_excl_vat",
    "old_price_EUR_excl_vat",
    "price_EUR_incl_vat",
    "quantity",
    "quantity_unit",
    "weight_kg",
    "width",
    "height",
    "length",
    "has_options",
    "attr_manufacturer",
    "attr_brand",
    "attr_model_name",
    *IMAGE_COLUMNS,
    "image_count",
]
EXTRA_COLUMNS = [
    "record_kind",
    "in_varle",
    "short_description_LT",
    "minimum_purchase_quantity",
    "published_LT",
    "published_EN",
    "published_RU",
    "supplier",
    "stock_location",
    "attributes_json",
]
TRACKED_COLUMNS = SCRAPER_COLUMNS + EXTRA_COLUMNS
STATUS_COLUMNS = [
    "active",
    "missing_runs",
    "first_seen_at",
    "last_seen_at",
    "removed_at",
]
CURRENT_COLUMNS = TRACKED_COLUMNS + STATUS_COLUMNS
EVENT_COLUMNS = [
    "detected_at",
    "product_code",
    "event_type",
    "field_name",
    "old_value",
    "new_value",
    "record_kind",
]
INVALID_XML_CHARS_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]"
)


def clean(value: Any) -> str:
    return str(value or "").strip()


def decimal_text(value: Any) -> str:
    text = clean(value).replace(",", ".")
    if not text:
        return ""
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text
    formatted = format(number, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted or "0"


def gross_price(net: str, tax: str) -> str:
    try:
        net_value = Decimal(net)
        tax_value = Decimal(tax or "0")
    except InvalidOperation:
        return ""
    gross = net_value * (Decimal("1") + tax_value / Decimal("100"))
    return decimal_text(gross.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def child_text(element: ET.Element, tag: str) -> str:
    child = element.find(tag)
    return clean(child.text) if child is not None else ""


def nested_text(element: ET.Element, path: str) -> str:
    child = element.find(path)
    return clean(child.text) if child is not None else ""


def locale_text(element: ET.Element, tag: str, locale: str = "LT") -> str:
    localized = element.find(f"{tag}[@locale='{locale}']")
    if localized is not None:
        return clean(localized.text)
    fallback = element.find(tag)
    return clean(fallback.text) if fallback is not None else ""


def price_list_text(element: ET.Element, tag: str, price_list: str = "A") -> str:
    for child in element.findall(tag):
        if child.get("priceListName", "A") == price_list:
            return decimal_text(child.text)
    return ""


def publish_value(element: ET.Element, locale: str) -> str:
    value = locale_text(element, "Publish", locale).lower()
    if not value:
        return ""
    return "true" if value in {"1", "true", "yes", "taip"} else "false"


def attributes_for(element: ET.Element) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for attribute in element.findall("Attribute"):
        name = locale_text(attribute, "Name", "LT") or locale_text(attribute, "Name", "EN")
        value = locale_text(attribute, "Value", "LT") or locale_text(attribute, "Value", "EN")
        if name and value:
            attributes[name] = value
    return attributes


def attribute_value(attributes: dict[str, str], names: Iterable[str]) -> str:
    wanted = {name.casefold() for name in names}
    for name, value in attributes.items():
        if name.casefold() in wanted:
            return value
    return ""


def record_hash(record: dict[str, str]) -> str:
    payload = json.dumps(
        {column: record.get(column, "") for column in TRACKED_COLUMNS},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def base_record(
    element: ET.Element,
    *,
    code: str,
    kind: str,
    parent: ET.Element,
    parent_code: str,
    parent_attributes: dict[str, str],
    parent_images: list[str],
) -> dict[str, str]:
    tax = decimal_text(child_text(parent, "Tax"))
    net = price_list_text(element, "Price")
    old_net = price_list_text(element, "OldPrice")
    prime_cost = decimal_text(child_text(element, "PrimeCost"))
    quantity_node = element.find("Quantity")
    quantity_unit = clean(quantity_node.get("unit")) if quantity_node is not None else ""
    dimensions = element.find("Dimensions")
    images = [clean(node.text) for node in element.findall("Image") if clean(node.text)]
    if kind == "option" and not images:
        images = list(parent_images)

    attributes = dict(parent_attributes)
    if kind == "option":
        option_names = [
            locale_text(node, "Name", "LT") or locale_text(node, "Name", "EN")
            for node in parent.findall("OptionsAttribute")
        ]
        option_values = [
            locale_text(node, "Value", "LT") or locale_text(node, "Value", "EN")
            for node in element.findall("Attribute")
        ]
        for name, value in zip(option_names, option_values):
            if name and value:
                attributes[name] = value
    else:
        attributes.update(attributes_for(element))

    ean = child_text(element, "Barcode")
    if not ean:
        ean = attribute_value(attributes, ["EAN kodas", "EAN", "Barcode"])

    record: dict[str, str] = {column: "" for column in TRACKED_COLUMNS}
    record.update(
        {
            "product_code": code,
            "link": "",
            "parent_code": parent_code,
            "ean": ean,
            "category_path_LT": locale_text(parent, "CategoryPath", "LT"),
            "name_LT": locale_text(parent, "Name", "LT"),
            "description_LT": locale_text(parent, "Description", "LT"),
            "tax_pct": tax,
            "prime_cost_EUR": prime_cost,
            "price_EUR_excl_vat": net,
            "old_price_EUR_excl_vat": old_net,
            "price_EUR_incl_vat": gross_price(net, tax),
            "quantity": decimal_text(child_text(element, "Quantity")),
            "quantity_unit": quantity_unit or "piece",
            "weight_kg": decimal_text(child_text(element, "Weight")),
            "width": decimal_text(child_text(dimensions, "Width")) if dimensions is not None else "",
            "height": decimal_text(child_text(dimensions, "Height")) if dimensions is not None else "",
            "length": decimal_text(child_text(dimensions, "Length")) if dimensions is not None else "",
            "has_options": "True" if parent.findall("Option") else "False",
            "attr_manufacturer": attribute_value(attributes, ["Manufacturer", "Gamintojas"]),
            "attr_brand": attribute_value(attributes, ["Brand", "Prekinis ženklas"]),
            "attr_model_name": attribute_value(attributes, ["Modelio pavadinimas", "Model name"]),
            "image_count": str(len(images)),
            "record_kind": kind,
            "short_description_LT": locale_text(parent, "ShortDescription", "LT"),
            "minimum_purchase_quantity": decimal_text(nested_text(element, "Cart/MinimumQuantity")),
            "published_LT": publish_value(element, "LT") or publish_value(parent, "LT"),
            "published_EN": publish_value(element, "EN") or publish_value(parent, "EN"),
            "published_RU": publish_value(element, "RU") or publish_value(parent, "RU"),
            "supplier": child_text(element, "Supplier"),
            "stock_location": child_text(element, "StockLocation"),
            "attributes_json": json.dumps(attributes, ensure_ascii=False, sort_keys=True),
        }
    )
    for index, image in enumerate(images[: len(IMAGE_COLUMNS)], start=1):
        record[f"image_{index}"] = image
    record["_hash"] = record_hash(record)
    return record


def parse_source(path: Path) -> tuple[dict[str, dict[str, str]], list[str], dict[str, int]]:
    records_by_code: dict[str, list[dict[str, str]]] = defaultdict(list)
    product_count = 0
    option_count = 0

    for _, product in ET.iterparse(path, events=("end",)):
        if product.tag != "Product":
            continue
        product_count += 1
        code = clean(product.get("code"))
        parent_attributes = attributes_for(product)
        parent_images = [clean(node.text) for node in product.findall("Image") if clean(node.text)]
        if code:
            records_by_code[code].append(
                base_record(
                    product,
                    code=code,
                    kind="product",
                    parent=product,
                    parent_code="",
                    parent_attributes=parent_attributes,
                    parent_images=parent_images,
                )
            )
        for option in product.findall("Option"):
            option_count += 1
            option_code = clean(option.get("code"))
            if option_code:
                records_by_code[option_code].append(
                    base_record(
                        option,
                        code=option_code,
                        kind="option",
                        parent=product,
                        parent_code=code,
                        parent_attributes=parent_attributes,
                        parent_images=parent_images,
                    )
                )
        product.clear()

    duplicate_codes = sorted(code for code, rows in records_by_code.items() if len(rows) > 1)
    records = {code: rows[0] for code, rows in records_by_code.items() if len(rows) == 1}
    return records, duplicate_codes, {
        "source_products": product_count,
        "source_options": option_count,
        "source_unique_codes": len(records),
        "source_duplicate_codes": len(duplicate_codes),
    }


def parse_varle_ids(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    product_ids: set[str] = set()
    for _, product in ET.iterparse(path, events=("end",)):
        if product.tag != "product":
            continue
        code = child_text(product, "id")
        if code:
            product_ids.add(code)
        product.clear()
    return product_ids


def read_state(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"version": 1, "records": {}, "events": []}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict):
        raise ValueError("Catalogue state must be a JSON object")
    state.setdefault("records", {})
    state.setdefault("events", [])
    return state


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))


def event(
    timestamp: str,
    code: str,
    event_type: str,
    *,
    field_name: str = "",
    old_value: Any = "",
    new_value: Any = "",
    record_kind: str = "",
) -> dict[str, str]:
    def event_value(value: Any) -> str:
        text = clean(value)
        return text if len(text) <= 5000 else text[:4997] + "..."

    return {
        "detected_at": timestamp,
        "product_code": code,
        "event_type": event_type,
        "field_name": field_name,
        "old_value": event_value(old_value),
        "new_value": event_value(new_value),
        "record_kind": record_kind,
    }


def parse_timestamp(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def update_catalogue(
    source_path: Path,
    previous_state_path: Path | None,
    output_state_path: Path,
    *,
    varle_path: Path | None = None,
    missing_confirmations: int = 2,
    history_days: int = 400,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]], dict[str, Any]]:
    now = now or dt.datetime.now(dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)
    timestamp = now.isoformat()
    source_records, duplicate_codes, source_stats = parse_source(source_path)
    varle_ids = parse_varle_ids(varle_path)
    if varle_path is not None:
        for code, record in source_records.items():
            record["in_varle"] = "true" if code in varle_ids else "false"
            record["_hash"] = record_hash(record)
    not_in_varle_codes = sorted(
        code for code in source_records if varle_path is not None and code not in varle_ids
    )
    old_state = read_state(previous_state_path)
    old_records: dict[str, dict[str, Any]] = old_state.get("records", {})
    old_duplicate_codes = set(old_state.get("duplicate_codes", []))
    initialized = bool(old_records)
    next_records: dict[str, dict[str, Any]] = {}
    latest_events: list[dict[str, str]] = []
    added_codes: list[str] = []
    removed_codes: list[str] = []
    restored_codes: list[str] = []

    for code, source_record in source_records.items():
        previous = old_records.get(code)
        current = dict(source_record)
        if previous is None:
            current.update(
                {
                    "active": True,
                    "missing_runs": 0,
                    "first_seen_at": timestamp,
                    "last_seen_at": timestamp,
                    "removed_at": "",
                }
            )
            if initialized:
                added_codes.append(code)
                latest_events.append(
                    event(timestamp, code, "added", record_kind=current.get("record_kind", ""))
                )
        else:
            current.update(
                {
                    "active": True,
                    "missing_runs": 0,
                    "first_seen_at": previous.get("first_seen_at") or timestamp,
                    "last_seen_at": timestamp,
                    "removed_at": "",
                }
            )
            if not previous.get("active", True) or int(previous.get("missing_runs", 0) or 0) > 0:
                restored_codes.append(code)
                latest_events.append(
                    event(timestamp, code, "restored", record_kind=current.get("record_kind", ""))
                )
            if previous.get("_hash") != current.get("_hash"):
                for field in TRACKED_COLUMNS:
                    old_value = clean(previous.get(field))
                    new_value = clean(current.get(field))
                    if old_value != new_value:
                        latest_events.append(
                            event(
                                timestamp,
                                code,
                                "changed",
                                field_name=field,
                                old_value=old_value,
                                new_value=new_value,
                                record_kind=current.get("record_kind", ""),
                            )
                        )
        next_records[code] = current

    for code, previous in old_records.items():
        if code in source_records:
            continue
        current = dict(previous)
        misses = int(previous.get("missing_runs", 0) or 0) + 1
        current["missing_runs"] = misses
        if misses == 1:
            latest_events.append(
                event(timestamp, code, "missing_pending", record_kind=clean(current.get("record_kind")))
            )
        if misses >= max(1, missing_confirmations):
            if previous.get("active", True):
                removed_codes.append(code)
                latest_events.append(
                    event(timestamp, code, "removed", record_kind=clean(current.get("record_kind")))
                )
            current["active"] = False
            current["quantity"] = "0"
            current["removed_at"] = previous.get("removed_at") or timestamp
        next_records[code] = current

    for code in duplicate_codes:
        if code not in old_duplicate_codes:
            latest_events.append(event(timestamp, code, "duplicate_code"))

    cutoff = now - dt.timedelta(days=max(1, history_days))
    historical_events = []
    for row in old_state.get("events", []):
        detected = parse_timestamp(clean(row.get("detected_at")))
        if detected is not None and detected >= cutoff:
            historical_events.append({column: clean(row.get(column)) for column in EVENT_COLUMNS})
    all_events = historical_events + latest_events

    state = {
        "version": 1,
        "updated_at": timestamp,
        **source_stats,
        "duplicate_codes": duplicate_codes,
        "records": next_records,
        "events": all_events,
    }
    write_state(output_state_path, state)
    report = {
        "generated_at": timestamp,
        **source_stats,
        "tracked_records": len(next_records),
        "active_records": sum(1 for row in next_records.values() if row.get("active", True)),
        "inactive_records": sum(1 for row in next_records.values() if not row.get("active", True)),
        "baseline_created": not initialized,
        "added_products": len(added_codes),
        "removed_products": len(removed_codes),
        "restored_products": len(restored_codes),
        "field_changes": sum(1 for row in latest_events if row["event_type"] == "changed"),
        "latest_events": len(latest_events),
        "retained_events": len(all_events),
        "not_in_varle": len(not_in_varle_codes),
        "duplicate_codes": duplicate_codes[:100],
        "not_in_varle_codes": not_in_varle_codes[:500],
        "added_codes": added_codes[:500],
        "removed_codes": removed_codes[:500],
        "restored_codes": restored_codes[:500],
    }
    selections = {
        "added_codes": set(added_codes),
        "removed_codes": set(removed_codes),
        "duplicate_codes": duplicate_codes,
        "not_in_varle_codes": set(not_in_varle_codes),
    }
    return state, latest_events, {"report": report, **selections}


def write_csv(path: Path, rows: Iterable[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def excel_column(index: int) -> str:
    value = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def excel_text(value: Any) -> str:
    text = INVALID_XML_CHARS_RE.sub("", str(value if value is not None else ""))
    if len(text) > 32767:
        text = text[:32764] + "..."
    return text


def worksheet_xml(rows: list[dict[str, Any]], columns: list[str]) -> str:
    last_row = max(1, len(rows) + 1)
    last_column = excel_column(max(1, len(columns)))
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        f'<dimension ref="A1:{last_column}{last_row}"/>',
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
        "<sheetData>",
    ]
    all_rows: list[list[Any]] = [columns]
    all_rows.extend([[row.get(column, "") for column in columns] for row in rows])
    for row_index, values in enumerate(all_rows, start=1):
        cells = []
        for column_index, value in enumerate(values, start=1):
            reference = f"{excel_column(column_index)}{row_index}"
            style = ' s="1"' if row_index == 1 else ""
            text = excel_text(value)
            cells.append(
                f'<c r="{reference}" t="inlineStr"{style}><is><t xml:space="preserve">'
                f"{escape(text)}</t></is></c>"
            )
        lines.append(f'<row r="{row_index}">' + "".join(cells) + "</row>")
    lines.append("</sheetData>")
    if columns:
        end = f"{excel_column(len(columns))}{max(1, len(all_rows))}"
        lines.append(f'<autoFilter ref="A1:{end}"/>')
    lines.append("</worksheet>")
    return "".join(lines)


def write_xlsx(
    path: Path,
    sheets: list[tuple[str, list[dict[str, Any]], list[str]]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ]
    workbook_sheets = []
    workbook_relationships = []
    for index, (name, _, _) in enumerate(sheets, start=1):
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        workbook_sheets.append(
            f'<sheet name={quoteattr(name[:31])} sheetId="{index}" r:id="rId{index}"/>'
        )
        workbook_relationships.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
    style_relationship_id = len(sheets) + 1
    workbook_relationships.append(
        f'<Relationship Id="rId{style_relationship_id}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    content_types.append("</Types>")
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>" + "".join(workbook_sheets) + "</sheets></workbook>"
    )
    root_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(workbook_relationships)
        + "</Relationships>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/>'
        '<bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/>'
        "</cellStyles></styleSheet>"
    )

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("[Content_Types].xml", "".join(content_types))
        archive.writestr("_rels/.rels", root_relationships)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles)
        for index, (_, rows, columns) in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", worksheet_xml(rows, columns))


def build_outputs(
    state: dict[str, Any],
    latest_events: list[dict[str, str]],
    selections: dict[str, Any],
    *,
    current_csv: Path,
    events_csv: Path,
    latest_changes_csv: Path,
    new_products_csv: Path,
    removed_products_csv: Path,
    not_in_varle_csv: Path,
    workbook: Path,
) -> None:
    records: dict[str, dict[str, Any]] = state["records"]
    current_rows = [records[code] for code in sorted(records)]
    added_rows = [records[code] for code in sorted(selections["added_codes"])]
    removed_rows = [records[code] for code in sorted(selections["removed_codes"])]
    not_in_varle_rows = [records[code] for code in sorted(selections["not_in_varle_codes"])]
    all_events = state.get("events", [])
    issues = [
        {
            "product_code": code,
            "issue": "duplicate_code",
            "details": "The code occurs more than once in the current Verskis XML and is excluded.",
        }
        for code in selections["duplicate_codes"]
    ]
    issues.extend(
        {
            "product_code": row["product_code"],
            "issue": "missing_pending",
            "details": "Missing once; removal requires another consecutive valid feed.",
        }
        for row in latest_events
        if row["event_type"] == "missing_pending"
    )

    write_csv(current_csv, current_rows, CURRENT_COLUMNS)
    write_csv(events_csv, all_events, EVENT_COLUMNS)
    write_csv(latest_changes_csv, latest_events, EVENT_COLUMNS)
    write_csv(new_products_csv, added_rows, CURRENT_COLUMNS)
    write_csv(removed_products_csv, removed_rows, CURRENT_COLUMNS)
    write_csv(not_in_varle_csv, not_in_varle_rows, CURRENT_COLUMNS)
    write_xlsx(
        workbook,
        [
            ("Current Products", current_rows, CURRENT_COLUMNS),
            ("New Products", added_rows, CURRENT_COLUMNS),
            ("Removed Products", removed_rows, CURRENT_COLUMNS),
            ("Not in Varle", not_in_varle_rows, CURRENT_COLUMNS),
            ("Latest Changes", latest_events, EVENT_COLUMNS),
            ("Change History", all_events, EVENT_COLUMNS),
            ("Issues", issues, ["product_code", "issue", "details"]),
        ],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--varle", type=Path)
    parser.add_argument("--previous-state", type=Path)
    parser.add_argument("--output-state", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--current-csv", required=True, type=Path)
    parser.add_argument("--events-csv", required=True, type=Path)
    parser.add_argument("--latest-changes-csv", required=True, type=Path)
    parser.add_argument("--new-products-csv", required=True, type=Path)
    parser.add_argument("--removed-products-csv", required=True, type=Path)
    parser.add_argument("--not-in-varle-csv", required=True, type=Path)
    parser.add_argument("--workbook", required=True, type=Path)
    parser.add_argument("--missing-confirmations", type=int, default=2)
    parser.add_argument("--history-days", type=int, default=400)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    state, latest_events, selections = update_catalogue(
        args.source.resolve(),
        args.previous_state.resolve() if args.previous_state and args.previous_state.exists() else None,
        args.output_state.resolve(),
        varle_path=args.varle.resolve() if args.varle and args.varle.exists() else None,
        missing_confirmations=max(1, args.missing_confirmations),
        history_days=max(1, args.history_days),
    )
    build_outputs(
        state,
        latest_events,
        selections,
        current_csv=args.current_csv.resolve(),
        events_csv=args.events_csv.resolve(),
        latest_changes_csv=args.latest_changes_csv.resolve(),
        new_products_csv=args.new_products_csv.resolve(),
        removed_products_csv=args.removed_products_csv.resolve(),
        not_in_varle_csv=args.not_in_varle_csv.resolve(),
        workbook=args.workbook.resolve(),
    )
    report = selections["report"]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
