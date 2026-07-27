import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from zero_stock import zero_stock


class ZeroStockTests(unittest.TestCase):
    def test_only_quantity_values_change(self) -> None:
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<products>"
            "<product><id>A</id><quantity>7</quantity>"
            "<description><![CDATA[Vežimėlis & <b>tekstas</b>]]></description></product>"
            "<product><id>B</id><quantity> 0 </quantity><price>12.50</price></product>"
            "</products>"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "main.xml"
            candidate = root / "candidate.xml"
            report_path = root / "report.json"
            state = root / "state.json"
            main.write_text(xml, encoding="utf-8")
            state.write_text('{"missing_counts":{"A":1}}', encoding="utf-8")

            report = zero_stock(
                main, candidate, report_path, state, minimum_main_products=2
            )

            result = candidate.read_text(encoding="utf-8")
            self.assertIn("<quantity>0</quantity>", result)
            self.assertEqual(result.count("<quantity>0</quantity>"), 2)
            self.assertIn(
                "<![CDATA[Vežimėlis & <b>tekstas</b>]]>",
                result,
            )
            self.assertIn("<price>12.50</price>", result)
            self.assertEqual(report["main_products"], 2)
            self.assertEqual(report["changed_products"], 1)
            self.assertTrue(report["all_quantities_zero"])
            ET.fromstring(candidate.read_bytes())
            self.assertEqual(json.loads(state.read_text())["missing_counts"], {"A": 1})
            self.assertTrue(json.loads(state.read_text())["emergency_zero_stock"])

    def test_rejects_product_without_exactly_one_quantity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "main.xml"
            main.write_text(
                "<products><product><id>A</id></product></products>",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "exactly one quantity"):
                zero_stock(
                    main,
                    root / "candidate.xml",
                    root / "report.json",
                    minimum_main_products=1,
                )

    def test_rejects_unexpectedly_small_feed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "main.xml"
            main.write_text(
                "<products><product><id>A</id><quantity>1</quantity></product></products>",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "expected at least"):
                zero_stock(
                    main,
                    root / "candidate.xml",
                    root / "report.json",
                    minimum_main_products=2,
                )
