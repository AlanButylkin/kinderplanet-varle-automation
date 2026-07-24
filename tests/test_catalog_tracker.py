from __future__ import annotations

import csv
import datetime as dt
import tempfile
import unittest
import zipfile
from pathlib import Path

from catalog_tracker import (
    CURRENT_COLUMNS,
    build_outputs,
    parse_source,
    read_state,
    update_catalogue,
)


def source_xml(*products: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Products>'
        + "".join(products)
        + "</Products>"
    ).encode("utf-8")


PRODUCT_A = """
<Product code="A">
  <CategoryPath locale="LT">Žaislai / Laukas</CategoryPath>
  <Name locale="LT">Produktas A</Name>
  <Description locale="LT"><![CDATA[<p>Aprašymas</p>]]></Description>
  <ShortDescription locale="LT">Trumpas</ShortDescription>
  <Tax>21</Tax>
  <PrimeCost currency="EUR">5</PrimeCost>
  <Price currency="EUR" priceListName="A">10</Price>
  <OldPrice currency="EUR" priceListName="A">12</OldPrice>
  <Quantity unit="piece">3</Quantity>
  <Weight>1.5</Weight>
  <Dimensions><Width>10</Width><Height>20</Height><Length>30</Length></Dimensions>
  <Cart><MinimumQuantity>1</MinimumQuantity></Cart>
  <Publish locale="LT">true</Publish>
  <Image>https://example.test/a.jpg</Image>
  <Attribute><Name locale="LT">Gamintojas</Name><Value locale="LT">Gamintojas A</Value></Attribute>
</Product>
"""

PRODUCT_B = """
<Product code="B">
  <CategoryPath locale="LT">Kita</CategoryPath>
  <Name locale="LT">Produktas B</Name>
  <Tax>21</Tax>
  <PrimeCost currency="EUR">2</PrimeCost>
  <Price currency="EUR" priceListName="A">4</Price>
  <OldPrice currency="EUR" priceListName="A">0</OldPrice>
  <Quantity unit="piece">2</Quantity>
  <Publish locale="LT">true</Publish>
</Product>
"""

PRODUCT_WITH_OPTION = """
<Product code="PARENT">
  <CategoryPath locale="LT">Vežimėliai</CategoryPath>
  <Name locale="LT">Tėvinis</Name>
  <Tax>21</Tax>
  <OptionsAttribute><Name locale="LT">Spalva</Name></OptionsAttribute>
  <Option code="OPTION-1">
    <Attribute><Value locale="LT">Raudona</Value></Attribute>
    <PrimeCost currency="EUR">8</PrimeCost>
    <Price currency="EUR" priceListName="A">10</Price>
    <OldPrice currency="EUR" priceListName="A">11</OldPrice>
    <Quantity unit="piece">6</Quantity>
    <Publish locale="LT">true</Publish>
    <Image>https://example.test/option.jpg</Image>
  </Option>
</Product>
"""


class CatalogueTrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.t0 = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)

    def write_source(self, data: bytes, name: str = "source.xml") -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def update(self, source: Path, previous: Path | None, output: str, now: dt.datetime):
        output_path = self.root / output
        return (
            output_path,
            *update_catalogue(
                source,
                previous,
                output_path,
                missing_confirmations=2,
                now=now,
            ),
        )

    def test_parses_scraper_fields_and_options(self):
        source = self.write_source(source_xml(PRODUCT_A, PRODUCT_WITH_OPTION))
        records, duplicates, stats = parse_source(source)
        self.assertEqual(duplicates, [])
        self.assertEqual(stats["source_products"], 2)
        self.assertEqual(stats["source_options"], 1)
        self.assertEqual(records["A"]["price_EUR_incl_vat"], "12.1")
        self.assertEqual(records["A"]["attr_manufacturer"], "Gamintojas A")
        self.assertEqual(records["A"]["image_1"], "https://example.test/a.jpg")
        self.assertEqual(records["OPTION-1"]["parent_code"], "PARENT")
        self.assertEqual(records["OPTION-1"]["record_kind"], "option")
        self.assertIn('"Spalva": "Raudona"', records["OPTION-1"]["attributes_json"])

    def test_baseline_then_added_and_changed(self):
        first = self.write_source(source_xml(PRODUCT_A), "first.xml")
        state1_path, _, first_events, first_selection = self.update(first, None, "state1.json.gz", self.t0)
        self.assertEqual(first_events, [])
        self.assertTrue(first_selection["report"]["baseline_created"])

        changed_a = PRODUCT_A.replace("<Quantity unit=\"piece\">3</Quantity>", "<Quantity unit=\"piece\">7</Quantity>")
        second = self.write_source(source_xml(changed_a, PRODUCT_B), "second.xml")
        state2_path, state2, second_events, selection = self.update(
            second, state1_path, "state2.json.gz", self.t0 + dt.timedelta(hours=1)
        )
        self.assertEqual(selection["report"]["added_products"], 1)
        self.assertEqual(state2["records"]["A"]["quantity"], "7")
        self.assertTrue(any(row["event_type"] == "added" and row["product_code"] == "B" for row in second_events))
        self.assertTrue(
            any(
                row["event_type"] == "changed"
                and row["product_code"] == "A"
                and row["field_name"] == "quantity"
                for row in second_events
            )
        )
        self.assertEqual(read_state(state2_path)["records"]["B"]["active"], True)

    def test_removal_requires_two_runs_and_restores(self):
        initial = self.write_source(source_xml(PRODUCT_A, PRODUCT_B), "initial.xml")
        state1_path, _, _, _ = self.update(initial, None, "state1.json.gz", self.t0)
        missing = self.write_source(source_xml(PRODUCT_A), "missing.xml")
        state2_path, state2, events2, _ = self.update(
            missing, state1_path, "state2.json.gz", self.t0 + dt.timedelta(hours=1)
        )
        self.assertTrue(state2["records"]["B"]["active"])
        self.assertEqual(state2["records"]["B"]["quantity"], "2")
        self.assertTrue(any(row["event_type"] == "missing_pending" for row in events2))

        state3_path, state3, events3, selection3 = self.update(
            missing, state2_path, "state3.json.gz", self.t0 + dt.timedelta(hours=2)
        )
        self.assertFalse(state3["records"]["B"]["active"])
        self.assertEqual(state3["records"]["B"]["quantity"], "0")
        self.assertEqual(selection3["report"]["removed_products"], 1)
        self.assertTrue(any(row["event_type"] == "removed" for row in events3))

        _, state4, events4, selection4 = self.update(
            initial, state3_path, "state4.json.gz", self.t0 + dt.timedelta(hours=3)
        )
        self.assertTrue(state4["records"]["B"]["active"])
        self.assertEqual(state4["records"]["B"]["quantity"], "2")
        self.assertEqual(selection4["report"]["restored_products"], 1)
        self.assertTrue(any(row["event_type"] == "restored" for row in events4))

    def test_generates_valid_csv_and_xlsx_package(self):
        source = self.write_source(source_xml(PRODUCT_A), "source.xml")
        _, state, latest_events, selections = self.update(source, None, "state.json.gz", self.t0)
        outputs = {
            "current_csv": self.root / "current.csv",
            "events_csv": self.root / "events.csv",
            "latest_changes_csv": self.root / "latest.csv",
            "new_products_csv": self.root / "new.csv",
            "removed_products_csv": self.root / "removed.csv",
            "not_in_varle_csv": self.root / "not-in-varle.csv",
            "workbook": self.root / "catalog.xlsx",
        }
        build_outputs(state, latest_events, selections, **outputs)
        with outputs["current_csv"].open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["product_code"], "A")
        self.assertEqual(list(rows[0]), CURRENT_COLUMNS)
        with zipfile.ZipFile(outputs["workbook"]) as archive:
            self.assertIn("xl/workbook.xml", archive.namelist())
            self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())

    def test_marks_source_products_not_in_varle(self):
        source = self.write_source(source_xml(PRODUCT_A, PRODUCT_B), "source.xml")
        varle = self.root / "varle.xml"
        varle.write_text(
            "<root><products><product><id>A</id><price>1</price><quantity>1</quantity></product></products></root>",
            encoding="utf-8",
        )
        state_path = self.root / "state.json.gz"
        state, _, selections = update_catalogue(
            source,
            None,
            state_path,
            varle_path=varle,
            now=self.t0,
        )
        self.assertEqual(state["records"]["A"]["in_varle"], "true")
        self.assertEqual(state["records"]["B"]["in_varle"], "false")
        self.assertEqual(selections["report"]["not_in_varle"], 1)
        self.assertEqual(selections["not_in_varle_codes"], {"B"})


if __name__ == "__main__":
    unittest.main()
