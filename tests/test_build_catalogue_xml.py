import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from build_catalogue_xml import (
    WorkbookRule,
    select_barcode,
    transform_xml,
    valid_gtin,
)


class BuildCatalogueXmlTests(unittest.TestCase):
    def test_barcode_priority(self) -> None:
        self.assertEqual(
            select_barcode("1111111111111", "2222222222222", "3333333333333"),
            ("1111111111111", "ean"),
        )
        self.assertEqual(
            select_barcode("", "2222222222222", "3333333333333"),
            ("3333333333333", "ean3_from_ean2"),
        )
        self.assertEqual(
            select_barcode("", "", "3333333333333"),
            ("3333333333333", "ean3"),
        )
        self.assertTrue(valid_gtin("4250341315820"))
        self.assertFalse(valid_gtin("4250341315821"))

    def test_replaces_barcodes_and_omits_ineligible_rows(self) -> None:
        source = """<?xml version="1.0" encoding="UTF-8"?>
<root><products>
<product><id>A</id><title><![CDATA[Vežimėlis]]></title><barcode_format></barcode_format><barcode></barcode></product>
<product><id>B</id><title><![CDATA[Keep bytes]]></title><barcode_format>EAN</barcode_format><barcode>old</barcode></product>
</products></root>
"""
        rules = {
            "A": WorkbookRule("A", "4250341315820", "ean3_from_ean2", True, ()),
            "B": WorkbookRule("B", "4250341315813", "ean", False, ("blank_price",)),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.xml"
            output_path = root / "output.xml"
            report_path = root / "report.json"
            source_path.write_text(source, encoding="utf-8")

            report = transform_xml(
                source_path, output_path, rules, report_path, {"expected_products": 1}
            )

            xml_root = ET.parse(output_path).getroot()
            products = xml_root.findall("./products/product")
            self.assertEqual(len(products), 1)
            self.assertEqual(products[0].findtext("id"), "A")
            self.assertEqual(products[0].findtext("barcode"), "4250341315820")
            self.assertEqual(products[0].findtext("barcode_format"), "EAN")
            self.assertIn("<![CDATA[Vežimėlis]]>", output_path.read_text("utf-8"))
            self.assertEqual(report["excluded_products"], 1)
            self.assertTrue(json.loads(report_path.read_text("utf-8"))["valid"])
