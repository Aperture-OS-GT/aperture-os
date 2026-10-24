"""Versioned DUT wiring and expectation profiles shipped as JSON resources."""
import json
from pathlib import Path


def list_profiles():
    return [json.loads(path.read_text()) for path in sorted(Path(__file__).parent.glob("*.json"))]


def load_profile(name):
    for profile in list_profiles():
        if profile["id"] == name:
            return profile
    raise ValueError("unknown profile")
