#!/usr/bin/env python3
"""Download BFCL test data"""
import urllib.request
import json
import os

DATA_DIR = os.environ.get(
    "BENCH_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench_data", "bfcl")
)
os.makedirs(DATA_DIR, exist_ok=True)

# BFCL v3 simple - from GitHub gorilla repo
urls = {
    "BFCL_v3_simple.json": "https://raw.githubusercontent.com/ShishirPatil/gorilla/main/berkeley-function-call-leaderboard/data/BFCL_v3_simple.json",
    "BFCL_v3_multiple.json": "https://raw.githubusercontent.com/ShishirPatil/gorilla/main/berkeley-function-call-leaderboard/data/BFCL_v3_multiple.json",
}

for fname, url in urls.items():
    path = os.path.join(DATA_DIR, fname)
    try:
        print(f"Downloading {fname}...")
        urllib.request.urlretrieve(url, path)
        with open(path) as f:
            data = json.load(f)
        print(f"  OK: {len(data)} items, {os.path.getsize(path)} bytes")
    except Exception as e:
        print(f"  FAILED: {e}")
        # Attempt to construct a small amount of mock data for testing
        if "simple" in fname:
            mock = [
                {
                    "id": 0,
                    "question": "What is the weather in Tokyo?",
                    "function": [{"name": "get_weather", "description": "Get weather for a city", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}],
                    "expected": {"name": "get_weather", "arguments": {"city": "Tokyo"}}
                },
                {
                    "id": 1,
                    "question": "Find flights from NYC to London on 2024-12-25",
                    "function": [{"name": "find_flights", "description": "Search flights", "parameters": {"type": "object", "properties": {"origin": {"type": "string"}, "destination": {"type": "string"}, "date": {"type": "string"}}}}],
                    "expected": {"name": "find_flights", "arguments": {"origin": "NYC", "destination": "London", "date": "2024-12-25"}}
                },
            ]
            with open(path, "w") as f:
                json.dump(mock, f, indent=2)
            print(f"  Created mock data: {len(mock)} items")
