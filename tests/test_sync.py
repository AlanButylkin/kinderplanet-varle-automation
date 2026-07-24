from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sync_varle import synchronize


SOURCE = b'''<?xml version="1.0" encoding="UTF-8"?>
<Products>
  <Product code="A"><Tax>21</Tax><Price currency="EUR" priceListName="A">10</Price><Quantity>3</Quantity></Product>
  <Product code="DUP"><Tax>21</Tax><Price currency="EUR" priceListName="A">5</Price><Quantity>1</Quantity></Product>
  <Product code="DUP"><Tax>21</Tax><Price currency="EUR" priceListName="A">6</Price><Quantity>2</Quantity></Product>
</Products>'''

MAIN = b'''<?xml version="1.0" encoding="UTF-8"?>
<root><products>
<product><id>A</id><title><![CDATA[Keep <b>exactly</b>]]></title><price>1</price><quantity>1</quantity></product>
<product><id>MISSING</id><title><![CDATA[Missing]]></title><price>2</price><quantity>4</quantity></product>
<product><id>DUP</id><title><![CDATA[Duplicate]]></title><price>3</price><quantity>5</quantity></product>
</products></root>'''


class SyncTests(unittest.TestCase):
    def run_sync(self, state_data=None):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        source = root / "source.xml"
        main = root / "main.xml"
        candidate = root / "candidate.xml"
        state = root / "state.json"
        report = root / "report.json"
        source.write_bytes(SOURCE)
        main.write_bytes(MAIN)
        if state_data is not None:
            state.write_text(json.dumps(state_data), encoding="utf-8")
        try:
            result = synchronize(
                source,
                main,
                candidate,
                state,
                report,
                minimum_main_products=3,
                minimum_match_rate=0.3,
                maximum_changed_fraction=1.0,
            )
        except Exception:
            temp.cleanup()
            raise
        return temp, root, candidate.read_text(encoding="utf-8"), result

    def test_updates_only_price_and_quantity(self):
        temp, _, candidate, result = self.run_sync()
        self.addCleanup(temp.cleanup)
        self.assertIn("<![CDATA[Keep <b>exactly</b>]]>", candidate)
        self.assertIn("<id>A</id><title><![CDATA[Keep <b>exactly</b>]]></title><price>12.1</price><quantity>3</quantity>", candidate)
        self.assertEqual(result["price_changes"], 1)
        self.assertEqual(result["quantity_changes"], 1)
        self.assertEqual(result["source_only_products"], 0)

    def test_missing_requires_two_runs_and_duplicate_is_untouched(self):
        temp1, root1, first, _ = self.run_sync()
        self.addCleanup(temp1.cleanup)
        first_state = json.loads((root1 / "state.json").read_text(encoding="utf-8"))
        self.assertIn("<id>MISSING</id><title><![CDATA[Missing]]></title><price>2</price><quantity>4</quantity>", first)
        self.assertIn("<id>DUP</id><title><![CDATA[Duplicate]]></title><price>3</price><quantity>5</quantity>", first)

        temp2, _, second, result = self.run_sync(first_state)
        self.addCleanup(temp2.cleanup)
        self.assertIn("<id>MISSING</id><title><![CDATA[Missing]]></title><price>2</price><quantity>0</quantity>", second)
        self.assertEqual(result["duplicate_source_codes_in_main"], 1)

    def test_rejects_unexpected_source_catalogue_drop(self):
        with self.assertRaisesRegex(RuntimeError, "unexpectedly shrank"):
            self.run_sync({"source_unique_codes": 100})


if __name__ == "__main__":
    unittest.main()
