#!/usr/bin/env python3
"""
Fetch one example result per LeakIX plugin and save each to
plugins_examples/{PluginName}.txt with full details.

Usage:
  export LEAKIX_API_KEY="YOUR_KEY"
  python3 leakix_plugin_examples.py
  python3 leakix_plugin_examples.py --scope leak --only DotEnvConfigPlugin,HttpNTLM
  python3 leakix_plugin_examples.py --overwrite
"""
import argparse
import os
import re
import sys
import textwrap
import time
import requests
from bs4 import BeautifulSoup

PLUGINS_URL = "https://leakix.net/plugins"
SEARCH_URL = "https://leakix.net/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LeakixScraper/1.0)"}

RATE_LIMIT_SECONDS = 1.1
_last_request_time = [0.0]

OUTPUT_DIR = "plugins_examples"


def _rate_limit():
    now = time.monotonic()
    elapsed = now - _last_request_time[0]
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)
    _last_request_time[0] = time.monotonic()


# ---------------- API ----------------
def query_api(url, api_key, params=None, timeout=60):
    _rate_limit()
    headers = dict(HEADERS)
    headers["accept"] = "application/json"
    if api_key:
        headers["api-key"] = api_key
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp


def scrape_plugin_names():
    resp = requests.get(PLUGINS_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    names = []
    for row in soup.find_all("div", class_="row"):
        c3 = row.find_all("div", class_="col-sm-3")
        c6 = row.find("div", class_="col-sm-6")
        if len(c3) == 2 and c6 is not None:
            name = c3[0].get_text(strip=True)
            if name:
                names.append(name)
    return names


def search_first(plugin, api_key, scope="leak"):
    """Return the first result l9event for a plugin, or None."""
    params = {"q": f"+plugin:{plugin}", "scope": scope, "page": 0}
    resp = query_api(SEARCH_URL, api_key, params=params)
    try:
        data = resp.json()
    except ValueError:
        # NDJSON fallback
        for line in resp.text.splitlines():
            line = line.strip()
            if line:
                import json
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        return None
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        return data
    return None


# ---------------- Rendering ----------------
def build_url(ev):
    proto = ev.get("protocol") or "http"
    if proto not in ("http", "https"):
        proto = "https" if str(ev.get("port")) == "443" else "http"
    host = ev.get("host") or ev.get("ip") or ""
    port = str(ev.get("port") or "")
    path = (ev.get("http") or {}).get("url") or ""
    netloc = host
    if port and not ((proto == "http" and port == "80") or
                     (proto == "https" and port == "443")):
        netloc = f"{host}:{port}"
    return f"{proto}://{netloc}{path}"


def render_event(plugin, ev):
    """Build the plain-text detailed card for one event."""
    lines = []
    lines.append(f"Plugin: {plugin}")
    lines.append(f"Event Source: {ev.get('event_source', '-')}")
    lines.append("")

    lines.append(f"  IP: {ev.get('ip', '-')}")
    if ev.get("host") and ev.get("host") != ev.get("ip"):
        lines.append(f"  Domain: {ev.get('host')}")
    lines.append(f"  Port: {ev.get('port', '-')}")
    lines.append(f"  URL: {build_url(ev)}")

    geoip = ev.get("geoip") or {}
    loc = ", ".join(x for x in [geoip.get("city_name"),
                                geoip.get("country_name")] if x)
    if loc:
        lines.append(f"  Location: {loc}")

    net = ev.get("network") or {}
    if net.get("organization_name"):
        lines.append(f"  Org: {net['organization_name']} "
                     f"(AS{net.get('asn', '?')})")

    sw = ((ev.get("service") or {}).get("software") or {})
    if sw.get("name"):
        ver = f" {sw.get('version')}" if sw.get("version") else ""
        lines.append(f"  Software: {sw['name']}{ver}")

    if ev.get("time"):
        lines.append(f"  Time: {ev['time']}")
    if ev.get("event_fingerprint"):
        lines.append(f"  Fingerprint: {ev['event_fingerprint']}")

    leak = ev.get("leak") or {}
    sev = (leak.get("severity") or "").upper()
    if sev:
        lines.append(f"  Severity: {sev}")
    if leak.get("type"):
        lines.append(f"  Leak Type: {leak['type']}")
    ds = leak.get("dataset") or {}
    if ds.get("files") or ds.get("rows"):
        parts = []
        if ds.get("files"):
            parts.append(f"{ds['files']} files")
        if ds.get("rows"):
            parts.append(f"{ds['rows']} rows")
        lines.append(f"  Dataset: {', '.join(parts)}")

    ssl = ev.get("ssl") or {}
    cert = ssl.get("certificate") or {}
    if cert.get("cn"):
        lines.append(f"  TLS CN: {cert['cn']}")
    if cert.get("domain"):
        lines.append(f"  TLS Domains: {', '.join(cert['domain'])}")

    summary = (ev.get("summary") or "").strip()
    if summary:
        lines.append("  Summary:")
        for raw in summary.splitlines():
            for wl in (textwrap.wrap(raw, 72) or [""]):
                lines.append("    " + wl)

    return "\n".join(lines) + "\n"


def safe_filename(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "plugin"


def main():
    parser = argparse.ArgumentParser(
        description="Save one example result per LeakIX plugin.")
    parser.add_argument("--api-key", metavar="KEY",
                        default=os.environ.get("LEAKIX_API_KEY"),
                        help="LeakIX API key (or set LEAKIX_API_KEY env var)")
    parser.add_argument("--scope", default="leak",
                        choices=["leak", "service"],
                        help="Search scope (default: leak)")
    parser.add_argument("--only", metavar="LIST",
                        help="Comma-separated subset of plugin names to fetch")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-fetch and overwrite existing files")
    args = parser.parse_args()

    if not args.api_key:
        print("Error: no API key. Use --api-key KEY or set LEAKIX_API_KEY.",
              file=sys.stderr)
        sys.exit(1)

    try:
        plugins = scrape_plugin_names()
    except requests.RequestException as e:
        print(f"Error fetching plugin list: {e}", file=sys.stderr)
        sys.exit(1)

    if args.only:
        wanted = {p.strip() for p in args.only.split(",") if p.strip()}
        plugins = [p for p in plugins if p in wanted]
        missing = wanted - set(plugins)
        if missing:
            print(f"Warning: not in plugin index: {', '.join(sorted(missing))}",
                  file=sys.stderr)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    total = len(plugins)
    found = 0
    empty = 0
    skipped = 0
    errors = 0

    print(f"Fetching one example for {total} plugins "
          f"(scope={args.scope})...\n", file=sys.stderr)

    for i, plugin in enumerate(plugins, 1):
        path = os.path.join(OUTPUT_DIR, safe_filename(plugin) + ".txt")

        if os.path.exists(path) and not args.overwrite:
            skipped += 1
            sys.stderr.write(f"\r[{i}/{total}] {plugin}: skipped (exists)   ")
            sys.stderr.flush()
            continue

        try:
            ev = search_first(plugin, args.api_key, scope=args.scope)
        except requests.HTTPError as e:
            errors += 1
            sys.stderr.write(f"\n[{i}/{total}] {plugin}: HTTP error {e}\n")
            continue
        except requests.RequestException as e:
            errors += 1
            sys.stderr.write(f"\n[{i}/{total}] {plugin}: error {e}\n")
            continue

        if not ev:
            empty += 1
            # Write a stub so you know it was checked and had no results
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"Plugin: {plugin}\n\n  No results found.\n")
            sys.stderr.write(f"\r[{i}/{total}] {plugin}: no results        ")
            sys.stderr.flush()
            continue

        text = render_event(plugin, ev)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        found += 1
        sys.stderr.write(f"\r[{i}/{total}] {plugin}: saved            ")
        sys.stderr.flush()

    sys.stderr.write("\n\n")
    print(f"Done. saved={found}  no_results={empty}  "
          f"skipped={skipped}  errors={errors}", file=sys.stderr)
    print(f"Files in: {OUTPUT_DIR}/", file=sys.stderr)


if __name__ == "__main__":
    main()
