import unittest

from build_catalogue_xml import (
    cdata,
    product_attributes,
    select_barcode,
    valid_gtin,
)


class BuildCatalogueXmlTests(unittest.TestCase):
    def test_barcode_priority_and_validation(self) -> None:
        self.assertEqual(
            select_barcode("4250341315820", "2222222222222", "4250341315813"),
            ("4250341315820", "ean"),
        )
        self.assertEqual(
            select_barcode("", "source-ean2", "4250341315813"),
            ("4250341315813", "ean3_from_ean2"),
        )
        self.assertEqual(
            select_barcode("", "", "4250341315813"),
            ("4250341315813", "ean3"),
        )
        self.assertEqual(select_barcode("", "source-ean2", ""), ("", "blank"))
        self.assertTrue(valid_gtin("4250341315820"))
        self.assertFalse(valid_gtin("4250341315821"))

    def test_all_remaining_fields_become_attributes_but_eans_do_not(self) -> None:
        attributes = product_attributes(
            {
                "product_code": "A",
                "link": "https://example.test/a",
                "ean": "",
                "ean2": "source-only",
                "ean3": "4250341315820",
                "category_path_LT": "Vežimėliai",
                "name_LT": "Testas",
                "description_LT": "<p>Aprašymas</p>",
                "price_EUR_incl_vat": 10,
                "quantity": 2,
                "image_1": "https://example.test/a.jpg",
                "Gamintojas": "KinderPlanet",
                "color": "Mėlyna",
                "tax_pct": 21,
                "width": 30,
            }
        )
        by_column = {column: (title, value) for column, title, value in attributes}
        self.assertNotIn("ean", by_column)
        self.assertNotIn("ean2", by_column)
        self.assertNotIn("ean3", by_column)
        self.assertNotIn("image_1", by_column)
        self.assertEqual(by_column["Gamintojas"], ("Gamintojas", "KinderPlanet"))
        self.assertEqual(by_column["color"], ("Spalva", "Mėlyna"))
        self.assertEqual(by_column["tax_pct"], ("PVM, %", "21"))
        self.assertEqual(by_column["width"], ("Plotis", "30"))

    def test_cdata_safely_splits_closing_marker(self) -> None:
        self.assertEqual(cdata("a]]>b"), "<![CDATA[a]]]]><![CDATA[>b]]>")


if __name__ == "__main__":
    unittest.main()
