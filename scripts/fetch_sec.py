"""Download Alphabet (and peer) filings data from SEC EDGAR into data/raw/.

SEC requires a descriptive User-Agent with a contact address:
https://www.sec.gov/os/accessing-edgar-data
Override it with the SEC_USER_AGENT environment variable.
"""
import gzip
import json
import os
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
USER_AGENT = os.environ.get("SEC_USER_AGENT", "SEC_Alphabet Dashboard starbucks@snu.ac.kr")

ALPHABET_CIK = "0001652044"

# SEC filers among Alphabet's global peers (non-SEC filers are described in peers.py only)
PEER_CIKS = {
    "MSFT": "0000789019",
    "AAPL": "0000320193",
    "AMZN": "0001018724",
    "META": "0001326801",
    "ORCL": "0001341439",
    "NVDA": "0001045810",
    "BIDU": "0001329099",
    "BABA": "0001577552",
}


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return json.loads(body.decode("utf-8"))
        except Exception as exc:  # retry transient SEC throttling / network errors
            if attempt == 3:
                raise
            print(f"  retry {attempt + 1} for {url}: {exc}")
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def save(name: str, data: dict) -> None:
    path = RAW / name
    path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"  saved {path.relative_to(ROOT)} ({path.stat().st_size / 1024:,.0f} KB)")


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    print("Alphabet companyfacts / submissions")
    save("alphabet_companyfacts.json", get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{ALPHABET_CIK}.json"))
    time.sleep(0.3)
    save("alphabet_submissions.json", get_json(f"https://data.sec.gov/submissions/CIK{ALPHABET_CIK}.json"))

    peers_dir = RAW / "peers"
    peers_dir.mkdir(exist_ok=True)
    for ticker, cik in PEER_CIKS.items():
        time.sleep(0.3)
        print(f"Peer {ticker}")
        try:
            data = get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        except Exception as exc:
            print(f"  skipped {ticker}: {exc}")
            continue
        save(f"peers/{ticker}_companyfacts.json", data)


if __name__ == "__main__":
    main()
