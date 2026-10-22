"""Read-only probe of the Wallex API for milestone 2 (task 2.1 and 2.2).

The script collects the facts that `WallexBroker` needs and writes them to
one JSON file. It sends GET requests only. It never places or cancels an
order.

What it collects:

* the public market list (price and quantity precision, minimum sizes),
* the response headers (rate limit headers, if Wallex sends them),
* the answer of the account endpoints to the API key: status code and body
  shape (balances, open orders, order history),
* the start pages of the API documentation.

The API key comes from the environment variable WALLEX_API_KEY. The script
replaces the key with "<KEY>" in all output. Without the key, it runs the
public part only.

Some account paths below are candidates, not facts. A 404 answer for a path
is a useful result too.

Usage (standard library only, run where Wallex answers):
    WALLEX_API_KEY=... python3 -I research/scripts/wallex_api_probe.py OUT.json
Run it once in Iran and once on the shadow-test server (task 2.2).
"""

import json
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.wallex.ir"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
# Bypass the dead ALL_PROXY of the developer machine (AGENTS.md section 5).
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PUBLIC = ["/v1/markets", "/v2/markets", "/v1/currencies/stats",
          "/v1/depth?symbol=BTCTMN", "/v1/trades?symbol=BTCTMN"]
# Candidate account paths. GET only, so none of them can trade.
ACCOUNT = ["/v1/account/balances", "/v1/account/openOrders",
           "/v1/account/orders", "/v1/account/trades",
           "/v1/account/profile", "/v1/account/fee",
           "/v2/account/balances", "/v2/account/openOrders"]
# Header names to try for the key. The probe sends one name per request.
KEY_HEADERS = ["x-api-key", "X-API-Key", "Authorization"]
DOCS = ["https://developers.wallex.ir/", "https://api.wallex.ir/docs",
        "https://wallex.ir/app/api-docs", "https://api-docs.wallex.ir/"]
BODY_LIMIT = 200_000


def redact(text: str, key: str | None) -> str:
    """Replaces the API key in `text`."""
    return text.replace(key, "<KEY>") if key else text


def get(url: str, headers: dict, key: str | None) -> dict:
    """Sends one GET request and returns status, headers, time and body."""
    request = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    started = time.time()
    try:
        with OPENER.open(request, timeout=20) as response:
            status, head, body = response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        status, head, body = error.code, dict(error.headers), error.read()
    except (OSError, ValueError) as error:
        return {"url": url, "error": f"{type(error).__name__}: {error}"}
    text = body.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    result = {"url": url, "status": status, "ms": round(1000 * (time.time() - started)),
              "headers": head, "bytes": len(body)}
    if parsed is not None:
        result["json"] = parsed
    else:
        result["text"] = text[:BODY_LIMIT]
    return json.loads(redact(json.dumps(result), key))


def main(out_path: str) -> None:
    """Runs all probes and writes the JSON file."""
    key = os.environ.get("WALLEX_API_KEY") or None
    report = {"time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "host": socket.gethostname(), "python": platform.python_version(),
              "has_key": key is not None, "public": [], "account": [], "docs": []}
    try:
        report["public_ip"] = get("https://api.ipify.org?format=json", {}, key).get("json")
    except Exception as error:  # Only for the record. Not needed.
        report["public_ip"] = str(error)
    for path in PUBLIC:
        print("public", path, file=sys.stderr)
        report["public"].append(get(BASE + path, {}, key))
    if key:
        for path in ACCOUNT:
            for header in KEY_HEADERS:
                value = f"Bearer {key}" if header == "Authorization" else key
                print("account", path, header, file=sys.stderr)
                result = get(BASE + path, {header: value}, key)
                result["key_header"] = header
                report["account"].append(result)
                if result.get("status") == 200:
                    break
                time.sleep(0.5)
    for url in DOCS:
        print("docs", url, file=sys.stderr)
        report["docs"].append(get(url, {}, key))
    text = redact(json.dumps(report, ensure_ascii=False, indent=1), key)
    with open(out_path, "w", encoding="utf-8") as out_file:
        out_file.write(text)
    print(f"wrote {out_path} ({len(text) // 1024} KB). Check it before you share it.",
          file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "wallex_probe.json")
