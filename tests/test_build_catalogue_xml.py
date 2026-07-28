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

    def test_only_color_through_before_images_become_attributes(self) -> None:
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
                "tax_pct": 21,
                "prime_cost_EUR": 4,
                "price_EUR_incl_vat": 10,
                "quantity": 2,
                "quantity_unit": "piece",
                "has_options": False,
                "color": "Mėlyna",
                "image_1": "https://example.test/a.jpg",
                "image_count": 1,
                "weight_kg": 2,
                "Gamintojas": "KinderPlanet",
            }
        )
        by_column = {column: (title, value) for column, title, value in attributes}
        self.assertNotIn("ean", by_column)
        self.assertNotIn("ean2", by_column)
        self.assertNotIn("ean3", by_column)
        self.assertNotIn("tax_pct", by_column)
        self.assertNotIn("prime_cost_EUR", by_column)
        self.assertNotIn("quantity_unit", by_column)
        self.assertNotIn("has_options", by_column)
        self.assertNotIn("image_1", by_column)
        self.assertNotIn("image_count", by_column)
        self.assertNotIn("weight_kg", by_column)
        self.assertEqual(by_column["color"], ("Spalva", "Mėlyna"))
        # This field is after image_1 in the synthetic header order and must
        # therefore stay outside the attribute block.
        self.assertNotIn("Gamintojas", by_column)

    def test_attribute_block_preserves_xlsx_order(self) -> None:
        attributes = product_attributes(
            {
                "has_options": False,
                "color": "Juoda",
                "Gamintojas": "Moon",
                "Prekės ženklas": "Moon",
                "Vežimėlio tipas": "2 in 1",
                "image_1": "https://example.test/a.jpg",
                "image_count": 1,
                "width": 60,
            }
        )
        self.assertEqual(
            attributes,
            (
                ("color", "Spalva", "Juoda"),
                ("Gamintojas", "Gamintojas", "Moon"),
                ("Prekės ženklas", "Prekės ženklas", "Moon"),
                ("Vežimėlio tipas", "Vežimėlio tipas", "2 in 1"),
            ),
        )

    def test_legacy_sheet_keeps_only_attr_prefixed_fields(self) -> None:
        attributes = product_attributes(
            {
                "tax_pct": 21,
                "attr_manufacturer": "KinderPlanet",
                "attr_type": "Paslauga",
                "image_1": "https://example.test/a.jpg",
                "image_count": 1,
                "options_attribute_names": "Dydis",
            }
        )
        self.assertEqual(
            attributes,
            (
                ("attr_manufacturer", "Gamintojas", "KinderPlanet"),
                ("attr_type", "Type", "Paslauga"),
            ),
        )

    def test_cdata_safely_splits_closing_marker(self) -> None:
        self.assertEqual(cdata("a]]>b"), "<![CDATA[a]]]]><![CDATA[>b]]>")


if __name__ == "__main__":
    unittest.main()
