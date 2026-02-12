"""Quick regression test for legal-form regex patterns."""
import sys
sys.path.insert(0, r"C:\Users\roberth\Desktop\org-classifier\src")

from org_classifier.constants import LEGAL_FORMS

TESTS = [
    ("Siemens AG", "AG"),
    ("Deutsche Bank AG", "AG"),
    ("Boruscher Sportverein Dortmund e.V.", "e.V."),
    ("REWE Markt GmbH", "GmbH"),
    ("Stiftung Warentest", "Stiftung"),
    ("SAP SE", "SE"),
    ("BASF SE", "SE"),
    ("Bayer AG", "AG"),
    ("Allianz SE", "SE"),
    ("BMW AG", "AG"),
    ("Robert Bosch GmbH", "GmbH"),
    ("Lidl Stiftung & Co. KG", "Stiftung & Co. KG"),
    ("Edeka Zentrale AG & Co. KG", "AG & Co. KG"),
    ("Deutsche Telekom AG", "AG"),
    ("E.ON SE", "SE"),
    ("Hannover Rück SE", "SE"),
    ("ThyssenKrupp AG", "AG"),
    ("Münchener Rückversicherungs-Gesellschaft AG", "AG"),
    ("Technische Universität München", None),
    ("Aldi Süd", None),
    ("Deutsche Post DHL Group", None),
    ("Volkswagen AG", "AG"),
    ("Daimler AG", "AG"),
    ("Metro AG", "AG"),
    ("RWE AG", "AG"),
    ("Continental AG", "AG"),
    ("Talanx AG", "AG"),
    ("Adidas AG", "AG"),
]

def match(name):
    for lf, pat in LEGAL_FORMS:
        if pat.search(name):
            return lf
    return None

passed = 0
failed = 0
for name, expected in TESTS:
    result = match(name)
    if result == expected:
        passed += 1
    else:
        failed += 1
        print(f'  FAIL "{name}" -> got "{result}", expected "{expected}"')

print(f"\n{passed}/{len(TESTS)} passed, {failed} failed")
if failed:
    sys.exit(1)
