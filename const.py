"""Constants for the cat-generator-j1939 integration."""

import os

DOMAIN = "cat-generator-j1939"
DECODA_SPEC_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "decoda_spec.json"
)
