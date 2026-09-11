"""Offline tests for enrich gating, check_stock safety, KiCad helpers."""
import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from digikey_kicad import bom_ops
from digikey_kicad.kicad_bom import (
    candidate_queries,
    generic_hardware_reason,
    group_entries,
    parse_csv_bom,
)


def raw_product(**kw):
    """Raw DigiKey keyword-search product shape (pre-simplify)."""
    base = {
        "DigiKeyPartNumber": "311-10KCRCT-ND",
        "ManufacturerProductNumber": "RC0603FR-0710KL",
        "Manufacturer": {"Name": "Yageo"},
        "Description": {"ProductDescription": "10 kOhms ±1% Chip Resistor 0603 (1608 Metric)"},
        "Category": {"Name": "Chip Resistor - Surface Mount"},
        "QuantityAvailable": 50000,
        "UnitPrice": 0.002,
        "Parameters": [
            {"Parameter": "Resistance", "Value": "10 kOhms"},
            {"Parameter": "Package / Case", "Value": "0603 (1608 Metric)"},
        ],
        "ProductVariations": [
            {"DigiKeyProductNumber": "311-10KCRCT-ND",
             "PackageType": {"Name": "Cut Tape (CT)"},
             "QuantityAvailableforPackageType": 40000,
             "MinimumOrderQuantity": 10,
             "MarketPlace": False},
        ],
    }
    base.update(kw)
    return base


FAKE_SETTINGS = object()  # enrich only passes it through to the mocked API


class TestEnrichGate(unittest.TestCase):
    def run_enrich(self, rows, products):
        with mock.patch.object(
            bom_ops.dk, "keyword_search", return_value={"Products": products}
        ):
            return bom_ops.enrich_bom(rows, FAKE_SETTINGS, delay_s=0)

    def test_exact_mpn_found(self):
        rows = [{"Reference": "R1", "Value": "10k",
                 "Footprint": "Resistor_SMD:R_0603_1608Metric", "Qty": "2",
                 "MPN": "RC0603FR-0710KL", "Manufacturer": "Yageo"}]
        out = self.run_enrich(rows, [raw_product()])
        self.assertEqual(out[0]["dk_status"], "found")
        self.assertEqual(out[0]["digikey_pn"], "311-10KCRCT-ND")

    def test_wrong_top_hit_not_selected(self):
        # Top stock hit is a 4k7 part: must NOT be auto-selected for a 10k row.
        wrong = raw_product(
            DigiKeyPartNumber="311-4K7CRCT-ND",
            ManufacturerProductNumber="RC0603FR-074K7L",
            Description={"ProductDescription": "4.7 kOhms ±1% Chip Resistor 0603 (1608 Metric)"},
            Parameters=[
                {"Parameter": "Resistance", "Value": "4.7 kOhms"},
                {"Parameter": "Package / Case", "Value": "0603 (1608 Metric)"},
            ],
            ProductVariations=[
                {"DigiKeyProductNumber": "311-4K7CRCT-ND",
                 "PackageType": {"Name": "Cut Tape (CT)"},
                 "QuantityAvailableforPackageType": 999999,
                 "MinimumOrderQuantity": 10, "MarketPlace": False},
            ],
        )
        rows = [{"Reference": "R1", "Value": "10k",
                 "Footprint": "Resistor_SMD:R_0603_1608Metric", "Qty": "2"}]
        out = self.run_enrich(rows, [wrong])
        self.assertEqual(out[0]["dk_status"], "needs_review")
        self.assertEqual(out[0]["digikey_pn"], "")  # nothing selected
        self.assertIn("value_mismatch", out[0]["dk_review_reason"])
        self.assertTrue(out[0]["dk_suggested_pn"])  # suggestion kept for review

    def test_connector_never_auto_selected(self):
        conn = raw_product(
            DigiKeyPartNumber="999-XT60CT-ND",
            ManufacturerProductNumber="XT60U-F",
            Manufacturer={"Name": "Amphenol"},
            Description={"ProductDescription": "XT60 cable-mount connector"},
            Parameters=[],
            ProductVariations=[
                {"DigiKeyProductNumber": "999-XT60CT-ND",
                 "PackageType": {"Name": "Bulk"},
                 "QuantityAvailableforPackageType": 5000,
                 "MinimumOrderQuantity": 1, "MarketPlace": False},
            ],
        )
        rows = [{"Reference": "J1", "Value": "XT60PW-M",
                 "Footprint": "Connector:XT60PW-M", "Qty": "1"}]
        out = self.run_enrich(rows, [conn])
        self.assertEqual(out[0]["dk_status"], "needs_review")
        self.assertEqual(out[0]["digikey_pn"], "")

    def test_dnp_never_looked_up(self):
        with mock.patch.object(
            bom_ops.dk, "keyword_search",
            side_effect=AssertionError("API must not be called for DNP rows"),
        ):
            out = bom_ops.enrich_bom(
                [{"Reference": "R9", "Value": "10k",
                  "Footprint": "R_0603", "Qty": "1", "DNP": "yes"}],
                FAKE_SETTINGS, delay_s=0)
        self.assertEqual(out[0]["dk_status"], "ignored")
        self.assertIn("DNP", out[0]["dk_review_reason"])


class TestCheckStockSafety(unittest.TestCase):
    def test_no_dkpn_needs_pick_without_api(self):
        with mock.patch.object(
            bom_ops.dk, "product_details",
            side_effect=AssertionError("no API call without a DKPN")), \
            mock.patch.object(
                bom_ops.dk, "keyword_search",
                side_effect=AssertionError("no keyword fallback")):
            out = bom_ops.check_stock(
                [{"Reference": "R1", "Value": "10k", "dk_status": "needs_review"}],
                FAKE_SETTINGS)
        self.assertFalse(out[0]["ok"])
        self.assertIn("needs_pick", out[0]["stock_status"])
        self.assertNotIn("digikey_pn", out[0])

    def test_details_failure_is_error_not_guess(self):
        with mock.patch.object(
            bom_ops.dk, "product_details", side_effect=RuntimeError("boom")), \
            mock.patch.object(
                bom_ops.dk, "keyword_search",
                side_effect=AssertionError("must not keyword-guess stock")):
            out = bom_ops.check_stock(
                [{"Reference": "R1", "digikey_pn": "311-10KCRCT-ND", "Qty": "1"}],
                FAKE_SETTINGS)
        self.assertFalse(out[0]["ok"])
        self.assertTrue(out[0]["stock_status"].startswith("error"))


class TestKicadHelpers(unittest.TestCase):
    def test_kicad_cli_headers_normalized(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bom.csv"
            with p.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Reference", "Value", "Footprint", "${QUANTITY}",
                            "${DNP}", "MPN", "Manufacturer"])
                w.writerow(["R1,R2", "10k", "R_0603", "2", "", "RC0603FR-0710KL", "Yageo"])
            rows = parse_csv_bom(p)
        self.assertEqual(rows[0]["Qty"], "2")
        self.assertIn("DNP", rows[0])

    def test_dnp_grouping_not_merged(self):
        entries = [
            {"Reference": "R1", "Value": "10k", "Footprint": "R_0603",
             "MPN": "", "Manufacturer": "", "DNP": "", "Digikey_PN": ""},
            {"Reference": "R2", "Value": "10k", "Footprint": "R_0603",
             "MPN": "", "Manufacturer": "", "DNP": "yes", "Digikey_PN": ""},
        ]
        rows = group_entries(entries)
        self.assertEqual(len(rows), 2)

    def test_generic_hardware(self):
        self.assertTrue(generic_hardware_reason(
            {"Value": "Conn_01x02", "Footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"}))
        self.assertTrue(generic_hardware_reason({"Value": "10k", "DNP": "1"}))
        self.assertFalse(generic_hardware_reason({"Value": "10k", "Footprint": "R_0603"}))

    def test_candidate_queries_mpn_first(self):
        self.assertEqual(candidate_queries({"Value": "10k", "MPN": "RC0603FR-0710KL"}),
                         ["RC0603FR-0710KL"])


if __name__ == "__main__":
    unittest.main()
