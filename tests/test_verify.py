import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from digikey_kicad.verify import (
    classify_kind,
    close_enough,
    footprint_tokens,
    manufacturers_match,
    normalize_mpn,
    package_mentioned,
    parse_capacitance,
    parse_current,
    parse_frequency,
    parse_inductance,
    parse_resistance,
    parse_voltage_rating,
    verify_match,
)


def sp(**kw):
    """Minimal simplified product."""
    base = {
        "digikey_pn": "311-TESTCT-ND",
        "mpn": "TEST123",
        "manufacturer": "TestCorp",
        "description": "test part",
        "detailed_description": "",
        "datasheet_url": "",
        "product_url": "",
        "category": "",
        "quantity_available": 100,
        "stock_status": "In Stock",
        "unit_price": 0.01,
        "price_breaks": [],
        "variations": [],
        "parameters": [],
        "packaging": "",
    }
    base.update(kw)
    return base


class TestParsers(unittest.TestCase):
    def test_resistance(self):
        self.assertEqual(parse_resistance("10k"), 10000.0)
        self.assertEqual(parse_resistance("10K"), 10000.0)
        self.assertEqual(parse_resistance("4k7"), 4700.0)
        self.assertEqual(parse_resistance("4K7"), 4700.0)
        self.assertAlmostEqual(parse_resistance("R030"), 0.03)
        self.assertAlmostEqual(parse_resistance("R10"), 0.10)
        self.assertAlmostEqual(parse_resistance("30 mR"), 0.03)
        self.assertAlmostEqual(parse_resistance("30mR"), 0.03)
        self.assertEqual(parse_resistance("1M"), 1e6)
        self.assertEqual(parse_resistance("4M7"), 4.7e6)
        self.assertEqual(parse_resistance("10R"), 10.0)
        self.assertEqual(parse_resistance("4R7"), 4.7)
        self.assertIsNone(parse_resistance("10"))  # bare number: ambiguous
        self.assertIsNone(parse_resistance(""))
        self.assertIsNone(parse_resistance("100n"))  # cap value, no ohm marker
        self.assertIsNone(parse_resistance("abc"))

    def test_capacitance(self):
        self.assertAlmostEqual(parse_capacitance("100n"), 100e-9)
        self.assertAlmostEqual(parse_capacitance("100nF"), 100e-9)
        self.assertAlmostEqual(parse_capacitance("10u"), 10e-6)
        self.assertAlmostEqual(parse_capacitance("10µF"), 10e-6)
        self.assertAlmostEqual(parse_capacitance("4.7pF"), 4.7e-12)
        self.assertIsNone(parse_capacitance("10"))  # ambiguous without unit
        self.assertIsNone(parse_capacitance(""))

    def test_inductance(self):
        self.assertAlmostEqual(parse_inductance("10uH"), 10e-6)
        self.assertAlmostEqual(parse_inductance("100n"), 100e-9)
        self.assertIsNone(parse_inductance("10"))

    def test_voltage(self):
        self.assertEqual(parse_voltage_rating("100n 50V"), 50.0)
        self.assertEqual(parse_voltage_rating("6.3V"), 6.3)
        self.assertEqual(parse_voltage_rating("5V1"), 5.1)
        self.assertIsNone(parse_voltage_rating("10k"))

    def test_frequency_current(self):
        self.assertEqual(parse_frequency("8MHz"), 8e6)
        self.assertEqual(parse_frequency("32.768kHz"), 32768.0)
        self.assertIsNone(parse_frequency("8M"))
        self.assertEqual(parse_current("2A"), 2.0)
        self.assertEqual(parse_current("500mA"), 0.5)


class TestManufacturers(unittest.TestCase):
    def test_aliases(self):
        self.assertTrue(manufacturers_match("TI", "Texas Instruments"))
        self.assertTrue(manufacturers_match("STMicroelectronics", "ST"))
        self.assertTrue(manufacturers_match("Samsung", "Samsung Electro-Mechanics"))
        self.assertTrue(manufacturers_match("Yageo", "YAGEO"))
        self.assertFalse(manufacturers_match("Yageo", "Samsung"))
        self.assertTrue(manufacturers_match("", "Samsung"))  # unknown: neutral
        self.assertTrue(manufacturers_match("Yageo", ""))


class TestPackages(unittest.TestCase):
    def test_tokens(self):
        toks = footprint_tokens("Resistor_SMD:R_0603_1608Metric")
        self.assertIn("0603", toks)
        self.assertIn("1608", toks)
        self.assertEqual(footprint_tokens("SOT-23"), ["SOT23"])
        self.assertEqual(footprint_tokens(""), [])

    def test_mentioned(self):
        blob = "10 kOhms ±1% Chip Resistor 0603 (1608 Metric)"
        self.assertTrue(package_mentioned("0603", blob))
        self.assertTrue(package_mentioned("1608", blob))
        # SOT-23 must NOT match SOT-223
        self.assertFalse(package_mentioned("SOT23", "Transistor SOT-223"))
        self.assertTrue(package_mentioned("SOT223", "Transistor SOT-223"))
        # reversed pin-count form
        self.assertTrue(package_mentioned("SOIC8", "IC 8-SOIC"))
        # metric/imperial alias
        self.assertTrue(package_mentioned("0402", "Capacitor 1005 (0402 Metric)"))


def resistor_sp():
    return sp(
        digikey_pn="311-10KCRCT-ND",
        mpn="RC0603FR-0710KL",
        manufacturer="Yageo",
        description="10 kOhms ±1% 0.1W, 1/10W Chip Resistor 0603 (1608 Metric)",
        parameters=[
            {"name": "Resistance", "value": "10 kOhms"},
            {"name": "Package / Case", "value": "0603 (1608 Metric)"},
        ],
    )


class TestVerifyMatch(unittest.TestCase):
    def test_mpn_exact_wins(self):
        row = {"Reference": "R1", "Value": "10k", "Footprint": "x",
               "MPN": "RC0603FR-0710KL", "Manufacturer": "Yageo"}
        ok, why = verify_match(row, resistor_sp())
        self.assertTrue(ok, why)

    def test_mpn_mismatch_fails(self):
        row = {"Reference": "R1", "Value": "10k", "Footprint": "x",
               "MPN": "RC0603FR-074K7L", "Manufacturer": "Yageo"}
        ok, why = verify_match(row, resistor_sp())
        self.assertFalse(ok)
        self.assertIn("mpn_mismatch", why)

    def test_mpn_manufacturer_mismatch_fails(self):
        row = {"Reference": "R1", "Value": "10k", "Footprint": "x",
               "MPN": "RC0603FR-0710KL", "Manufacturer": "Samsung"}
        ok, why = verify_match(row, resistor_sp())
        self.assertFalse(ok)
        self.assertIn("manufacturer_mismatch", why)

    def test_passive_happy_path(self):
        row = {"Reference": "R1", "Value": "10k",
               "Footprint": "Resistor_SMD:R_0603_1608Metric"}
        ok, why = verify_match(row, resistor_sp())
        self.assertTrue(ok, why)

    def test_passive_wrong_value_fails(self):
        row = {"Reference": "R1", "Value": "4k7",
               "Footprint": "Resistor_SMD:R_0603_1608Metric"}
        ok, why = verify_match(row, resistor_sp())
        self.assertFalse(ok)
        self.assertIn("value_mismatch", why)

    def test_passive_wrong_package_fails(self):
        row = {"Reference": "R1", "Value": "10k",
               "Footprint": "Resistor_SMD:R_0805_2012Metric"}
        ok, why = verify_match(row, resistor_sp())
        self.assertFalse(ok)
        self.assertIn("package_mismatch", why)

    def test_passive_unit_trap(self):
        # 30 milliohm sense resistor must not match a 30 ohm part
        row = {"Reference": "R5", "Value": "30 mR",
               "Footprint": "Resistor_SMD:R_0603_1608Metric"}
        ok, why = verify_match(row, resistor_sp())
        self.assertFalse(ok)
        self.assertIn("value_mismatch", why)

    def test_ic_exact_required(self):
        row = {"Reference": "U1", "Value": "STM32G431KBT6",
               "Footprint": "LQFP-32"}
        good = sp(mpn="STM32G431KBT6", manufacturer="STMicroelectronics",
                  description="ARM MCU LQFP-32")
        ok, _ = verify_match(row, good)
        self.assertTrue(ok)
        bad = sp(mpn="STM32G431KBU6", manufacturer="STMicroelectronics",
                 description="ARM MCU")
        ok, why = verify_match(row, bad)
        self.assertFalse(ok)
        self.assertIn("mpn_mismatch", why)

    def test_connector_without_mpn_fails(self):
        row = {"Reference": "J1", "Value": "XT60PW-M",
               "Footprint": "Connector:XT60PW-M"}
        cand = sp(mpn="XT60U-F", manufacturer="Amphenol",
                  description="XT60 cable mount connector")
        ok, why = verify_match(row, cand)
        self.assertFalse(ok)
        self.assertIn("needs_exact_mpn", why)

    def test_dkpn_exact(self):
        row = {"Reference": "R1", "Value": "10k", "Digikey_PN": "311-10KCRCT-ND"}
        ok, why = verify_match(row, resistor_sp())
        self.assertTrue(ok, why)
        row2 = {"Reference": "R1", "Value": "10k", "Digikey_PN": "311-4K7CRCT-ND"}
        ok, why = verify_match(row2, resistor_sp())
        self.assertFalse(ok)
        self.assertIn("dkpn_mismatch", why)

    def test_cap_voltage_derating(self):
        row = {"Reference": "C1", "Value": "100n 50V",
               "Footprint": "Capacitor_SMD:C_0603_1608Metric"}
        weak = sp(mpn="CL10B104KB8NNNC", manufacturer="Samsung",
                  description="100nF ±10% 25V Ceramic Capacitor 0603 (1608 Metric)",
                  parameters=[{"name": "Capacitance", "value": "100 nF"},
                              {"name": "Voltage - Rated", "value": "25V"}])
        ok, why = verify_match(row, weak)
        self.assertFalse(ok)
        self.assertIn("voltage_rating_insufficient", why)

    def test_classify(self):
        self.assertEqual(classify_kind("R1", "10k"), "resistor")
        self.assertEqual(classify_kind("D3", "LED red"), "led")
        self.assertEqual(classify_kind("U1", "STM32"), "ic")


if __name__ == "__main__":
    unittest.main()
