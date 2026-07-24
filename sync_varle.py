#!/usr/bin/env python3
"""Synchronize Varle price and quantity fields from a Verskis full XML export.

The Varle XML is edited as text so every field other than ``price`` and
``quantity`` remains byte-for-byte unchanged. Both the source and candidate are
parsed as XML before a candidate is accepted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


PRODUCT_BLOCK_RE = re.compile(r"<product>(?P<body>.*?)</product>", re.DOTALL)
ID_RE = re.compile(r"(<id>)(?P<value>.*?)(</id>)", re.DOTALL)
PRICE_RE = re.compile(r"(<price>)(?P<value>.*?)(</price>)", re.DOTALL)
QUANTITY_RE = re.compile(r"(<quantity>)(?P<value>.*?)(</quantity>)", re.DOTALL)


@dataclass(frozen=True)
class SourceRecord:
    code: str
    price_no_vat: Decimal | None
    old_price_no_vat: Decimal | None
    quantity: Decimal | None
    vat: Decimal
    kind: str

    @property
    def price_incl_vat(self) -> Decimal | None:
        if self.price_no_vat is None:
            return None
        return self.price_no_vat * (Decimal("1") + self.vat / Decimal("100"))


def decimal_or_none(value: str | None) -> Decimal | None:
    text = (value or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def child_text(element: ET.Element, tag: str) -> str:
    child = element.find(tag)
    return (child.text or "").strip() if child is not None else ""


def price_list_value(element: ET.Element, tag: str, price_list: str = "A") -> Decimal | None:
    for child in element.findall(tag):
        if child.get("priceListName", "A") == price_list:
            return decimal_or_none(child.text)
    return None


def parse_source(path: Path) -> tuple[dict[str, SourceRecord], dict[str, list[SourceRecord]], dict[str, int]]:
    records_by_code: dict[str, list[SourceRecord]] = defaultdict(list)
    products = 0
    options = 0

    for _, product in ET.iterparse(path, events=("end",)):
        if product.tag != "Product":
            continue
        products += 1
        vat = decimal_or_none(child_text(product, "Tax")) or Decimal("0")
        code = (product.get("code") or "").strip()
        if code:
            records_by_code[code].append(
                SourceRecord(
                    code=code,
                    price_no_vat=price_list_value(product, "Price"),
                    old_price_no_vat=price_list_value(product, "OldPrice"),
                    quantity=decimal_or_none(child_text(product, "Quantity")),
                    vat=vat,
                    kind="product",
                )
            )

        for option in product.findall("Option"):
            options += 1
            option_code = (option.get("code") or "").strip()
            if option_code:
                records_by_code[option_code].append(
                    SourceRecord(
                        code=option_code,
                        price_no_vat=price_list_value(option, "Price"),
                        old_price_no_vat=price_list_value(option, "OldPrice"),
                        quantity=decimal_or_none(child_text(option, "Quantity")),
                        vat=vat,
                        kind="option",
                    )
                )
        product.clear()

    duplicates = {code: rows for code, rows in records_by_code.items() if len(rows) > 1}
    unique = {code: rows[0] for code, rows in records_by_code.items() if len(rows) == 1}
    stats = {
        "source_products": products,
        "source_options": options,
        "source_unique_codes": len(unique),
        "source_duplicate_codes": len(duplicates),
    }
    return unique, duplicates, stats


def format_number(value: Decimal, places: int = 2) -> str:
    quantum = Decimal("1").scaleb(-places)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def load_state(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"missing_counts": {}}
    with path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict):
        raise ValueError("State must be a JSON object")
    state.setdefault("missing_counts", {})
    return state


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_xml_bytes(data: bytes) -> None:
    parser = ET.XMLPullParser(events=("start", "end"))
    chunk_size = 1024 * 1024
    for offset in range(0, len(data), chunk_size):
        parser.feed(data[offset : offset + chunk_size])
    parser.close()


def synchronize(
    source_path: Path,
    main_path: Path,
    candidate_path: Path,
    state_path: Path | None,
    report_path: Path,
    *,
    missing_confirmations: int = 2,
    minimum_match_rate: float = 0.95,
    maximum_changed_fraction: float = 0.25,
    minimum_main_products: int = 4000,
    minimum_source_fraction_previous: float = 0.90,
) -> dict[str, Any]:
    unique_source, duplicate_source, source_stats = parse_source(source_path)
    state = load_state(state_path)
    previous_source_codes = int(state.get("source_unique_codes", 0) or 0)
    if previous_source_codes:
        source_fraction_previous = source_stats["source_unique_codes"] / previous_source_codes
        if source_fraction_previous < minimum_source_fraction_previous:
            raise RuntimeError(
                "Source catalogue unexpectedly shrank to "
                f"{source_fraction_previous:.3%} of its previous size; refusing to publish"
            )
    previous_missing: dict[str, int] = {
        str(code): int(count) for code, count in state.get("missing_counts", {}).items()
    }

    main_bytes = main_path.read_bytes()
    validate_xml_bytes(main_bytes)
    main_text = main_bytes.decode("utf-8-sig")
    blocks = list(PRODUCT_BLOCK_RE.finditer(main_text))
    if len(blocks) < minimum_main_products:
        raise RuntimeError(f"Main XML contains only {len(blocks)} products; minimum is {minimum_main_products}")

    main_ids: list[str] = []
    for match in blocks:
        id_match = ID_RE.search(match.group("body"))
        main_ids.append((id_match.group("value") if id_match else "").strip())

    duplicate_main_ids = [code for code, count in Counter(main_ids).items() if code and count > 1]
    if duplicate_main_ids:
        raise RuntimeError(f"Main XML contains duplicate product IDs: {duplicate_main_ids[:10]}")
    if any(not code for code in main_ids):
        raise RuntimeError("Main XML contains a product without an ID")

    matched_count = sum(code in unique_source for code in main_ids)
    match_rate = matched_count / len(main_ids)
    source_only_codes = sorted(set(unique_source) - set(main_ids))
    if match_rate < minimum_match_rate:
        raise RuntimeError(
            f"Source match rate {match_rate:.3%} is below the required {minimum_match_rate:.3%}"
        )

    new_missing_counts: dict[str, int] = {}
    price_changes: list[dict[str, str]] = []
    quantity_changes: list[dict[str, str]] = []
    missing_codes: list[str] = []
    duplicate_codes_in_main: list[str] = []
    missing_source_price: list[str] = []
    missing_source_quantity: list[str] = []
    untouched = 0

    output_parts: list[str] = []
    cursor = 0

    for match, code in zip(blocks, main_ids):
        output_parts.append(main_text[cursor : match.start()])
        original_block = match.group(0)
        body = match.group("body")
        updated_body = body
        record = unique_source.get(code)

        if code in duplicate_source:
            duplicate_codes_in_main.append(code)
            untouched += 1
        elif record is None:
            missing_codes.append(code)
            misses = previous_missing.get(code, 0) + 1
            new_missing_counts[code] = misses
            quantity_match = QUANTITY_RE.search(updated_body)
            if quantity_match and misses >= missing_confirmations:
                old_quantity = quantity_match.group("value").strip()
                if decimal_or_none(old_quantity) != Decimal("0"):
                    updated_body = QUANTITY_RE.sub(r"\g<1>0\g<3>", updated_body, count=1)
                    quantity_changes.append(
                        {"code": code, "old": old_quantity, "new": "0", "reason": "missing_from_source"}
                    )
        else:
            price_match = PRICE_RE.search(updated_body)
            quantity_match = QUANTITY_RE.search(updated_body)

            if record.price_incl_vat is None:
                missing_source_price.append(code)
            elif price_match:
                old_price = price_match.group("value").strip()
                new_price = format_number(record.price_incl_vat)
                if decimal_or_none(old_price) != decimal_or_none(new_price):
                    updated_body = PRICE_RE.sub(
                        lambda item: item.group(1) + new_price + item.group(3), updated_body, count=1
                    )
                    price_changes.append({"code": code, "old": old_price, "new": new_price})

            if record.quantity is None:
                missing_source_quantity.append(code)
            elif quantity_match:
                old_quantity = quantity_match.group("value").strip()
                new_quantity = format_number(record.quantity, places=4)
                if decimal_or_none(old_quantity) != decimal_or_none(new_quantity):
                    updated_body = QUANTITY_RE.sub(
                        lambda item: item.group(1) + new_quantity + item.group(3), updated_body, count=1
                    )
                    quantity_changes.append(
                        {"code": code, "old": old_quantity, "new": new_quantity, "reason": "source"}
                    )

        if updated_body == body:
            output_parts.append(original_block)
        else:
            output_parts.append("<product>" + updated_body + "</product>")
        cursor = match.end()

    output_parts.append(main_text[cursor:])
    candidate_text = "".join(output_parts)
    candidate_bytes = candidate_text.encode("utf-8")
    validate_xml_bytes(candidate_bytes)

    changed_codes = {row["code"] for row in price_changes} | {row["code"] for row in quantity_changes}
    changed_fraction = len(changed_codes) / len(main_ids)
    if changed_fraction > maximum_changed_fraction:
        raise RuntimeError(
            f"Changes affect {changed_fraction:.3%} of products; maximum is {maximum_changed_fraction:.3%}"
        )

    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_bytes(candidate_bytes)

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    report: dict[str, Any] = {
        "generated_at": generated_at,
        "source": str(source_path),
        "main": str(main_path),
        "candidate": str(candidate_path),
        **source_stats,
        "main_products": len(main_ids),
        "matched_unique_source": matched_count,
        "match_rate": round(match_rate, 6),
        "source_only_products": len(source_only_codes),
        "missing_from_source": len(missing_codes),
        "duplicate_source_codes_in_main": len(duplicate_codes_in_main),
        "missing_source_price": len(missing_source_price),
        "missing_source_quantity": len(missing_source_quantity),
        "price_changes": len(price_changes),
        "quantity_changes": len(quantity_changes),
        "changed_products": len(changed_codes),
        "changed_fraction": round(changed_fraction, 6),
        "main_sha256": sha256_bytes(main_bytes),
        "candidate_sha256": sha256_bytes(candidate_bytes),
        "main_bytes": len(main_bytes),
        "candidate_bytes": len(candidate_bytes),
        "samples": {
            "price_changes": price_changes[:25],
            "quantity_changes": quantity_changes[:25],
            "missing_codes": missing_codes[:50],
            "duplicate_source_codes": duplicate_codes_in_main[:50],
            "missing_source_price": missing_source_price[:50],
            "missing_source_quantity": missing_source_quantity[:50],
            "source_only_codes": source_only_codes[:100],
        },
        "publish_allowed": True,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if state_path is not None:
        new_state = {
            "updated_at": generated_at,
            "source_unique_codes": source_stats["source_unique_codes"],
            "main_products": len(main_ids),
            "match_rate": match_rate,
            "missing_counts": new_missing_counts,
            "last_candidate_sha256": report["candidate_sha256"],
        }
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(new_state, ensure_ascii=False, indent=2), encoding="utf-8")

    return report


def download_source(source: str, destination: Path) -> None:
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": "KinderPlanet-Varle-Sync/1.0"})
        with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    else:
        shutil.copyfile(Path(source).resolve(), destination)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Verskis XML path or HTTPS URL")
    parser.add_argument("--main", required=True, type=Path, help="Current Varle XML")
    parser.add_argument("--output", required=True, type=Path, help="Validated candidate XML")
    parser.add_argument("--state", type=Path, help="Persistent missing-product state JSON")
    parser.add_argument("--report", required=True, type=Path, help="Synchronization report JSON")
    parser.add_argument("--missing-confirmations", type=int, default=2)
    parser.add_argument("--minimum-match-rate", type=float, default=0.95)
    parser.add_argument("--maximum-changed-fraction", type=float, default=0.25)
    parser.add_argument("--minimum-main-products", type=int, default=4000)
    parser.add_argument("--minimum-source-fraction-previous", type=float, default=0.90)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    with tempfile.TemporaryDirectory(prefix="varle-sync-") as temp_dir:
        source_path = Path(temp_dir) / "source.xml"
        download_source(args.source, source_path)
        report = synchronize(
            source_path,
            args.main.resolve(),
            args.output.resolve(),
            args.state.resolve() if args.state else None,
            args.report.resolve(),
            missing_confirmations=max(args.missing_confirmations, 1),
            minimum_match_rate=args.minimum_match_rate,
            maximum_changed_fraction=args.maximum_changed_fraction,
            minimum_main_products=args.minimum_main_products,
            minimum_source_fraction_previous=args.minimum_source_fraction_previous,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
