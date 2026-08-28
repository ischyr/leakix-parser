#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import textwrap
import time
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

PLUGINS_URL = "https://leakix.net/plugins"
HOST_URL = "https://leakix.net/host/{ip}"
DOMAIN_URL = "https://leakix.net/domain/{domain}"
SUBDOMAINS_URL = "https://leakix.net/api/subdomains/{domain}"
BULK_SEARCH_URL = "https://leakix.net/bulk/search"
SEARCH_URL = "https://leakix.net/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LeakixScraper/1.0)"}

# Global rate limiter: ~1 request per 1.1 seconds (LeakIX limit is ~1/sec)
RATE_LIMIT_SECONDS = 1.1
_last_request_time = [0.0]

OUTPUT_DIR = "leakix_output"
BULK_OUTPUT_DIR = "bulk_output"

# ANSI colors
C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
    "blue": "\033[94m", "magenta": "\033[95m", "cyan": "\033[96m",
    "orange": "\033[38;5;208m", "gray": "\033[90m",
}

SEV_COLOR = {
    "critical": C["red"], "high": C["red"], "medium": C["yellow"],
    "low": C["blue"], "info": C["cyan"], "": C["gray"],
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def color(text, c):
    return f"{C.get(c, '')}{text}{C['reset']}"


def _rate_limit():
    now = time.monotonic()
    elapsed = now - _last_request_time[0]
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)
    _last_request_time[0] = time.monotonic()


# ---------------- Output capture ----------------
class Tee:
    """Capture printed output so it can be saved to file (ANSI stripped)."""
    def __init__(self):
        self.buffer = []

    def echo(self, text=""):
        print(text)
        self.buffer.append(text)

    def plain_text(self):
        return ANSI_RE.sub("", "\n".join(self.buffer))


def slugify(text, maxlen=60):
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return s[:maxlen] or "query"


def read_domains(path):
    """Read domains from a file, one per line. Blank lines and #comments skipped."""
    domains = []
    seen = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = line.strip()
            if not d or d.startswith("#"):
                continue
            if d not in seen:
                seen.add(d)
                domains.append(d)
    return domains


def save_output(kind, identifier, out, raw_text=None, output_dir=OUTPUT_DIR):
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base = f"{kind}_{slugify(identifier)}_{ts}"

    txt_path = os.path.join(output_dir, base + ".txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(out.plain_text() + "\n")

    saved = [txt_path]
    if raw_text is not None:
        raw_path = os.path.join(output_dir, base + ".raw.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(raw_text + "\n")
        saved.append(raw_path)

    sys.stderr.write(color(f"\n  Saved: {', '.join(saved)}\n", "green"))


# ---------------- Plugins ----------------
def scrape_plugins():
    resp = requests.get(PLUGINS_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    plugins = []
    for row in soup.find_all("div", class_="row"):
        c3 = row.find_all("div", class_="col-sm-3")
        c6 = row.find("div", class_="col-sm-6")
        if len(c3) == 2 and c6 is not None:
            name = c3[0].get_text(strip=True)
            desc = c6.get_text(strip=True)
            access = c3[1].get_text(strip=True)
            if name:
                plugins.append((name, desc, access))
    return plugins


def print_plugins_table(plugins, out):
    if not plugins:
        out.echo("No plugins found.")
        return
    headers = ("Plugin", "Description", "Access")
    rows = [headers] + plugins
    w0 = max(len(r[0]) for r in rows)
    w1 = max(len(r[1]) for r in rows)
    w2 = max(len(r[2]) for r in rows)

    def line(l, m, r):
        return f"{l}{'─'*(w0+2)}{m}{'─'*(w1+2)}{m}{'─'*(w2+2)}{r}"

    def fmt(a, b, c, colored=False):
        if colored:
            acol = "green" if c.lower() == "public" else "orange"
            return (f"│ {color(a.ljust(w0),'cyan')} │ {b.ljust(w1)} │ "
                    f"{color(c.ljust(w2), acol)} │")
        return f"│ {a:<{w0}} │ {b:<{w1}} │ {c:<{w2}} │"

    out.echo(line("┌", "┬", "┐"))
    out.echo(fmt(*headers))
    out.echo(line("├", "┼", "┤"))
    for name, desc, access in plugins:
        out.echo(fmt(name, desc, access, colored=True))
    out.echo(line("└", "┴", "┘"))
    out.echo(f"\nTotal plugins: {len(plugins)}")


# ---------------- Generic table ----------------
def _table(headers, rows, colors=None):
    all_rows = [headers] + rows
    widths = [max(len(str(r[i])) for r in all_rows) for i in range(len(headers))]

    def line(l, m, r):
        return l + m.join("─" * (w + 2) for w in widths) + r

    def fmt(cells, colored=False):
        parts = []
        for i, cell in enumerate(cells):
            s = str(cell).ljust(widths[i])
            if colored and colors and colors[i]:
                s = color(s, colors[i])
            parts.append(" " + s + " ")
        return "│" + "│".join(parts) + "│"

    out_lines = [line("┌", "┬", "┐"), fmt(headers), line("├", "┼", "┤")]
    for r in rows:
        out_lines.append(fmt(r, colored=True))
    out_lines.append(line("└", "┴", "┘"))
    return "\n".join(out_lines)


# ---------------- API queries ----------------
def query_api(url, api_key, params=None, timeout=30):
    _rate_limit()
    headers = dict(HEADERS)
    headers["accept"] = "application/json"
    if api_key:
        headers["api-key"] = api_key
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp


def query_host(ip, api_key):
    return query_api(HOST_URL.format(ip=ip), api_key).json()


def query_domain(domain, api_key):
    return query_api(DOMAIN_URL.format(domain=domain), api_key).json()


def query_subdomains(domain, api_key):
    return query_api(SUBDOMAINS_URL.format(domain=domain), api_key).json()


def query_bulk_search(query, api_key, page=0):
    params = {"q": query}
    if page:
        params["page"] = page
    return query_api(BULK_SEARCH_URL, api_key, params=params, timeout=120)


def query_search(query, api_key, scope="leak", page=0):
    params = {"q": query, "scope": scope, "page": page}
    return query_api(SEARCH_URL, api_key, params=params, timeout=60)


def parse_records(resp):
    text = resp.text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        return [data]
    except json.JSONDecodeError:
        pass
    objs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            objs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return objs


# ---------------- Multi-page fetchers ----------------
def _bulk_key(r):
    fps = r.get("fingerprints")
    if fps:
        return ("fp", tuple(sorted(fps)))
    return ("res", r.get("resource_id"), r.get("ip"),
            tuple(r.get("open_ports") or []))


def fetch_bulk_all(query, api_key, max_pages=None, debug=False):
    seen = set()
    all_records = []
    raw_chunks = []
    HARD_CAP = 200
    page = 0
    empty_streak = 0

    while True:
        if max_pages is not None and page >= max_pages:
            break
        if page >= HARD_CAP:
            sys.stderr.write(f"\n  Reached safety cap of {HARD_CAP} pages.\n")
            break

        resp = query_bulk_search(query, api_key, page=page)
        recs = parse_records(resp)
        if not recs:
            break

        first_id = recs[0].get("resource_id") if recs else None
        new_count = 0
        for r in recs:
            k = _bulk_key(r)
            if k in seen:
                continue
            seen.add(k)
            all_records.append(r)
            raw_chunks.append(json.dumps(r))
            new_count += 1

        if debug:
            sys.stderr.write(
                f"\n  bulk page {page}: {len(recs)} recs, +{new_count} new, "
                f"first_resource={first_id}")
        else:
            sys.stderr.write(
                f"\r  Bulk page {page}: got {len(recs)}, +{new_count} new "
                f"({len(all_records)} unique total)   ")
        sys.stderr.flush()

        if new_count == 0:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0

        page += 1

    sys.stderr.write("\n")
    return all_records, "\n".join(raw_chunks)


def fetch_search_all(query, api_key, scope="leak", max_pages=None, debug=False):
    seen = set()
    all_events = []
    raw_chunks = []
    HARD_CAP = 5000
    page = 0
    raw_total = 0
    pages_fetched = 0

    while True:
        if max_pages is not None and page >= max_pages:
            break
        if page >= HARD_CAP:
            sys.stderr.write(f"\n  Reached safety cap of {HARD_CAP} pages.\n")
            break

        resp = query_search(query, api_key, scope=scope, page=page)
        events = parse_records(resp)

        if not events:
            if debug:
                sys.stderr.write(f"\n  page {page}: EMPTY -> stop\n")
            break

        pages_fetched += 1
        raw_total += len(events)

        new_count = 0
        for e in events:
            key = (
                e.get("event_fingerprint"),
                e.get("ip"),
                e.get("host"),
                e.get("port"),
                e.get("time"),
            )
            if key in seen:
                continue
            seen.add(key)
            all_events.append(e)
            raw_chunks.append(json.dumps(e))
            new_count += 1

        if debug:
            first_ip = events[0].get("ip")
            sys.stderr.write(
                f"\n  page {page}: {len(events)} events, +{new_count} new "
                f"(unique={len(all_events)}, raw={raw_total}), "
                f"first_ip={first_ip}")
        else:
            sys.stderr.write(
                f"\r  Page {page}: raw={raw_total} unique={len(all_events)}   ")
        sys.stderr.flush()

        page += 1

    sys.stderr.write("\n")
    stats = {
        "raw_total": raw_total,
        "unique_total": len(all_events),
        "pages_fetched": pages_fetched,
        "duplicates": raw_total - len(all_events),
    }
    return all_events, "\n".join(raw_chunks), stats


# ---------------- Display helpers ----------------
def hr(char="─", width=78):
    return C["gray"] + char * width + C["reset"]


def kv(key, value, kcolor="gray", vcolor=None):
    v = color(value, vcolor) if vcolor else str(value)
    return f"  {color(key + ':', kcolor)} {v}"


def build_url(ev):
    """Construct the accessible URL for an l9event."""
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


def _dedupe(events):
    seen = set()
    out = []
    for ev in events:
        key = (ev.get("event_fingerprint"),
               ev.get("host"),
               ev.get("ip"),
               ev.get("port"),
               (ev.get("leak") or {}).get("type"))
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def print_service(svc, out):
    out.echo(hr("═"))
    plugin = svc.get("event_source", "?")
    port = svc.get("port", "?")
    proto = svc.get("protocol", "")
    host = svc.get("host", "")
    out.echo(f"{color(' ' + plugin, 'bold')}  "
             f"{color(f'{proto}/{port}', 'orange')}"
             + (f"  {color(host, 'cyan')}" if host else ""))
    out.echo(hr())

    out.echo(kv("IP", svc.get("ip", "-"), vcolor="orange"))
    if svc.get("host") and svc.get("host") != svc.get("ip"):
        out.echo(kv("Domain", svc.get("host"), vcolor="cyan"))
    out.echo(kv("Port", svc.get("port", "-"), vcolor="orange"))
    out.echo(kv("URL", build_url(svc), vcolor="blue"))

    geoip = svc.get("geoip") or {}
    loc = ", ".join(x for x in [geoip.get("city_name"),
                                geoip.get("country_name")] if x)
    if loc:
        out.echo(kv("Location", loc))

    net = svc.get("network") or {}
    if net.get("organization_name"):
        out.echo(kv("Org", f"{net['organization_name']} "
                     f"(AS{net.get('asn','?')})"))

    sw = ((svc.get("service") or {}).get("software") or {})
    if sw.get("name"):
        ver = f" {sw.get('version')}" if sw.get("version") else ""
        out.echo(kv("Software", f"{sw['name']}{ver}"))

    if svc.get("time"):
        out.echo(kv("Time", svc["time"]))
    if svc.get("event_fingerprint"):
        out.echo(kv("Fingerprint", svc["event_fingerprint"], vcolor="magenta"))

    leak = svc.get("leak") or {}
    sev = (leak.get("severity") or "").lower()
    if sev:
        out.echo("  " + color("Severity:", "gray") + " " +
                 SEV_COLOR.get(sev, "") + f" {sev.upper()} " + C["reset"])
    if leak.get("type"):
        out.echo(kv("Leak Type", leak["type"]))
    ds = leak.get("dataset") or {}
    if ds.get("files") or ds.get("rows"):
        parts = []
        if ds.get("files"):
            parts.append(f"{ds['files']} files")
        if ds.get("rows"):
            parts.append(f"{ds['rows']} rows")
        out.echo(kv("Dataset", ", ".join(parts)))

    ssl = svc.get("ssl") or {}
    cert = ssl.get("certificate") or {}
    if cert.get("cn"):
        out.echo(kv("TLS CN", cert["cn"]))
    if cert.get("domain"):
        out.echo(kv("TLS Domains", ", ".join(cert["domain"])))

    summary = (svc.get("summary") or "").strip()
    if summary:
        out.echo("\n  " + color("Summary:", "gray"))
        wrapped = "\n".join(
            "    " + line for raw in summary.splitlines()
            for line in (textwrap.wrap(raw, 72) or [""])
        )
        out.echo(color(wrapped, "dim"))
    out.echo()


def print_services_table(services, out):
    if not services:
        return
    out.echo(color("\n  ── All Services ──", "bold"))
    headers = ("Plugin", "Proto/Port", "Software", "Host", "IP", "Country")
    rows = []
    for s in services:
        sw = ((s.get("service") or {}).get("software") or {})
        swname = sw.get("name", "")
        if swname and sw.get("version"):
            swname += f" {sw['version']}"
        geo = s.get("geoip") or {}
        rows.append((
            s.get("event_source", "-"),
            f"{s.get('protocol','')}/{s.get('port','')}",
            swname or "-",
            s.get("host") or "-",
            s.get("ip") or "-",
            geo.get("country_name") or "-",
        ))
    out.echo(_table(headers, rows,
                    colors=["cyan", "orange", "green", "cyan", "gray", None]))


def print_subdomain_table(services, out):
    if not services:
        return
    by_host = {}
    for s in services:
        h = s.get("host") or s.get("ip") or "-"
        by_host.setdefault(h, {"ips": set(), "ports": set()})
        if s.get("ip"):
            by_host[h]["ips"].add(s.get("ip"))
        by_host[h]["ports"].add(f"{s.get('protocol','')}/{s.get('port','')}")

    out.echo(color("\n  ── Subdomains ──", "bold"))
    headers = ("Subdomain", "IPs", "Ports")
    rows = []
    for host, info in sorted(by_host.items()):
        rows.append((
            host,
            str(len(info["ips"])),
            ", ".join(sorted(info["ports"])),
        ))
    out.echo(_table(headers, rows, colors=["cyan", "yellow", "orange"]))


def print_leaks_table(leak_groups, out):
    if not leak_groups:
        return
    out.echo(color("\n  ── All Leaks ──", "bold"))
    headers = ("Plugin(s)", "Severity", "Port(s)", "Leak Events", "Host")
    rows = []
    for g in leak_groups:
        events = g.get("events") or []
        plugins = g.get("plugins") or sorted(
            {e.get("event_source", "") for e in events if e.get("event_source")})
        severities = sorted(
            {(e.get("leak") or {}).get("severity", "")
             for e in events if (e.get("leak") or {}).get("severity")})
        ports = g.get("open_ports") or sorted(
            {str(e.get("port", "")) for e in events if e.get("port")})
        host = g.get("Ip") or (events[0].get("host") if events else "-")
        rows.append((
            ", ".join(plugins) or "-",
            ", ".join(s.upper() for s in severities) or "-",
            ", ".join(ports) or "-",
            str(g.get("leak_event_count", len(events))),
            host or "-",
        ))
    out.echo(_table(headers, rows,
                    colors=["cyan", "yellow", "orange", "magenta", "cyan"]))


def _group_leak_urls(g):
    """Return deduped [(plugin, severity, url)] for a leak group,
    substituting the resource_id hostname when the event host is only an IP."""
    events = g.get("events") or []
    resource_host = g.get("resource_id")
    seen = set()
    rows = []
    for e in events:
        ev = dict(e)
        if resource_host and (not ev.get("host") or
                              ev.get("host") == ev.get("ip")):
            ev["host"] = resource_host
        u = build_url(ev)
        plug = e.get("event_source", "?")
        sev = ((e.get("leak") or {}).get("severity") or "").upper()
        key = (plug, u)
        if key in seen:
            continue
        seen.add(key)
        rows.append((plug, sev, u))
    return rows


def print_leak_group(g, out):
    events = g.get("events") or []
    rep = events[0] if events else {}
    plugins = g.get("plugins") or []
    ports = g.get("open_ports") or []

    out.echo(hr("═"))
    title = ", ".join(plugins) or rep.get("event_source", "Leak")
    out.echo(f"{color(' ' + title, 'bold')}  " + color("/".join(ports), "orange"))
    out.echo(hr())

    out.echo(kv("IP", g.get("Ip", "-"), vcolor="orange"))
    if g.get("resource_id"):
        out.echo(kv("Resource", g.get("resource_id"), vcolor="cyan"))
    out.echo(kv("Open Ports", ", ".join(ports) or "-", vcolor="orange"))
    out.echo(kv("Leak Count", g.get("leak_count", "-")))
    out.echo(kv("Leak Events", g.get("leak_event_count", len(events))))

    leak = rep.get("leak") or {}
    sev = (leak.get("severity") or "").lower()
    if sev:
        out.echo("  " + color("Severity:", "gray") + " " +
                 SEV_COLOR.get(sev, "") + f" {sev.upper()} " + C["reset"])

    geo = g.get("geoip") or {}
    loc = ", ".join(x for x in [geo.get("city_name"),
                                geo.get("country_name")] if x)
    if loc:
        out.echo(kv("Location", loc))

    net = g.get("network") or {}
    if net.get("organization_name"):
        out.echo(kv("Org", f"{net['organization_name']} "
                     f"(AS{net.get('asn','?')})"))

    if g.get("update_date"):
        out.echo(kv("Last Seen", g["update_date"]))
    if g.get("creation_date"):
        out.echo(kv("First Seen", g["creation_date"]))

    # Vulnerable URLs per plugin for this leak group.
    url_rows = _group_leak_urls(g)
    if url_rows:
        out.echo("\n  " + color("Vulnerable URLs:", "gray"))
        for plug, sev_u, u in url_rows:
            sev_tag = ""
            if sev_u:
                sc = SEV_COLOR.get(sev_u.lower(), "")
                sev_tag = f" {sc}[{sev_u}]{C['reset']}"
            out.echo(f"    {color(plug, 'magenta')}{sev_tag} "
                     f"{color(u, 'blue')}")

    summary = (g.get("Summary") or rep.get("summary") or "").strip()
    if summary:
        out.echo("\n  " + color("Summary:", "gray"))
        wrapped = "\n".join(
            "    " + line for raw in summary.splitlines()
            for line in (textwrap.wrap(raw, 72) or [""])
        )
        out.echo(color(wrapped, "dim"))
    out.echo()


def print_vulnerable_urls_table(leak_groups, out):
    """Consolidated table of every distinct vulnerable URL across all groups."""
    seen = set()
    rows = []
    for g in leak_groups:
        for plug, sev, u in _group_leak_urls(g):
            key = (plug, u)
            if key in seen:
                continue
            seen.add(key)
            rows.append((plug, sev or "-", u))
    if not rows:
        return
    out.echo(color("\n  ── Vulnerable URLs ──", "bold"))
    headers = ("Plugin", "Severity", "URL")
    out.echo(_table(headers, rows, colors=["magenta", "yellow", "blue"]))


def print_result(data, label, out, is_domain=False):
    services = data.get("Services") or []
    leak_groups = data.get("Leaks") or []
    uniq_services = _dedupe(services)

    out.echo()
    out.echo(color(f"  {label}", "bold"))
    out.echo(color(f"  Services: {len(services)} (unique {len(uniq_services)})"
                   f"   Leak groups: {len(leak_groups)}", "gray"))

    if is_domain:
        distinct_hosts = sorted({s.get("host") for s in services if s.get("host")})
        distinct_ips = sorted({s.get("ip") for s in services if s.get("ip")})
        out.echo(color(f"  Subdomains: {len(distinct_hosts)}   "
                       f"Distinct IPs: {len(distinct_ips)}", "gray"))

    if not uniq_services and not leak_groups:
        out.echo(color("\n  No records found.", "yellow"))
        return

    if leak_groups:
        out.echo(color("\n  ═══ LEAKS ═══", "red"))
        for g in leak_groups:
            print_leak_group(g, out)

    if uniq_services:
        out.echo(color("\n  ═══ SERVICES ═══", "cyan"))
        for ev in uniq_services:
            print_service(ev, out)

    print_services_table(uniq_services, out)
    if is_domain:
        print_subdomain_table(uniq_services, out)
    print_leaks_table(leak_groups, out)
    print_vulnerable_urls_table(leak_groups, out)

    total_leak_events = sum(g.get("leak_event_count", 0) for g in leak_groups)
    total_distinct_leaks = sum(g.get("leak_count", 0) for g in leak_groups)
    out.echo(color("\n  ── Summary ──", "bold"))
    out.echo(kv("Total Services", f"{len(services)} "
                f"(unique {len(uniq_services)})", vcolor="cyan"))
    if is_domain:
        distinct_hosts = sorted({s.get("host") for s in services if s.get("host")})
        distinct_ips = sorted({s.get("ip") for s in services if s.get("ip")})
        out.echo(kv("Subdomains", len(distinct_hosts), vcolor="cyan"))
        out.echo(kv("Distinct IPs", len(distinct_ips), vcolor="cyan"))
    out.echo(kv("Leak Groups", len(leak_groups), vcolor="red"))
    out.echo(kv("Distinct Leaks", total_distinct_leaks, vcolor="red"))
    out.echo(kv("Total Leak Events", total_leak_events, vcolor="yellow"))
    out.echo()


def print_subdomains(data, domain, out):
    subs = data or []
    out.echo()
    out.echo(color(f"  Subdomains of {domain}", "bold"))
    out.echo(color(f"  Total: {len(subs)}", "gray"))

    if not subs:
        out.echo(color("\n  No subdomains found.", "yellow"))
        return

    subs = sorted(subs, key=lambda s: s.get("last_seen") or "", reverse=True)

    out.echo()
    headers = ("Subdomain", "Distinct IPs", "Last Seen")
    rows = []
    for s in subs:
        last_seen = s.get("last_seen") or "-"
        if "T" in last_seen:
            date_part, _, time_part = last_seen.partition("T")
            time_part = time_part.split(".")[0].rstrip("Z")
            last_seen = f"{date_part} {time_part}"
        rows.append((
            s.get("subdomain", "-"),
            str(s.get("distinct_ips", "-")),
            last_seen,
        ))
    out.echo(_table(headers, rows, colors=["cyan", "yellow", "gray"]))
    out.echo()


def _tally_events(records):
    plugin_counts, sev_counts, country_counts = {}, {}, {}
    distinct_ips = set()
    for r in records:
        pl = r.get("event_source") or "unknown"
        plugin_counts[pl] = plugin_counts.get(pl, 0) + 1
        sv = ((r.get("leak") or {}).get("severity") or "none").lower()
        sev_counts[sv] = sev_counts.get(sv, 0) + 1
        cn = (r.get("geoip") or {}).get("country_name") or "unknown"
        country_counts[cn] = country_counts.get(cn, 0) + 1
        if r.get("ip"):
            distinct_ips.add(r["ip"])
    return plugin_counts, sev_counts, country_counts, distinct_ips


def print_search(records, query, scope, out, stats=None, show_cards=True):
    out.echo()
    out.echo(color(f"  Search: {query}", "bold"))
    out.echo(color(f"  Scope: {scope}", "gray"))

    if not records:
        out.echo(color("\n  No results found.", "yellow"))
        return

    if show_cards:
        out.echo(color("\n  ═══ RESULTS ═══", "cyan"))
        for r in records:
            print_service(r, out)

    out.echo(color("\n  ── All Results ──", "bold"))
    headers = ("Plugin", "Severity", "Proto/Port", "IP", "Host", "Country")
    rows = []
    for r in records:
        leak = r.get("leak") or {}
        geo = r.get("geoip") or {}
        rows.append((
            r.get("event_source", "-"),
            (leak.get("severity") or "-").upper(),
            f"{r.get('protocol','')}/{r.get('port','')}",
            r.get("ip") or "-",
            r.get("host") or "-",
            geo.get("country_name") or "-",
        ))
    out.echo(_table(headers, rows,
                    colors=["cyan", "yellow", "orange", "gray", "cyan", None]))

    # Vulnerable URLs table for search results too
    seen = set()
    url_rows = []
    for r in records:
        u = build_url(r)
        plug = r.get("event_source", "-")
        sev = ((r.get("leak") or {}).get("severity") or "-").upper()
        key = (plug, u)
        if key in seen:
            continue
        seen.add(key)
        url_rows.append((plug, sev, u))
    if url_rows:
        out.echo(color("\n  ── Vulnerable URLs ──", "bold"))
        out.echo(_table(("Plugin", "Severity", "URL"), url_rows,
                        colors=["magenta", "yellow", "blue"]))

    plugin_counts, sev_counts, country_counts, distinct_ips = \
        _tally_events(records)
    distinct_hosts = len({r.get("host") for r in records if r.get("host")})

    out.echo(color("\n  ── Summary ──", "bold"))
    if stats:
        out.echo(kv("Raw Results (all pages)", stats["raw_total"], vcolor="cyan"))
        out.echo(kv("Unique Results", stats["unique_total"], vcolor="green"))
        out.echo(kv("Duplicates Removed", stats["duplicates"], vcolor="gray"))
        out.echo(kv("Pages Fetched", stats["pages_fetched"], vcolor="gray"))
    else:
        out.echo(kv("Total Results", len(records), vcolor="cyan"))
    out.echo(kv("Distinct IPs", len(distinct_ips), vcolor="cyan"))
    out.echo(kv("Distinct Hosts", distinct_hosts, vcolor="cyan"))
    out.echo(kv("By Severity", ", ".join(
        f"{k.upper()}={v}" for k, v in sorted(sev_counts.items())),
        vcolor="yellow"))
    out.echo(kv("By Plugin", ", ".join(
        f"{k}={v}" for k, v in sorted(plugin_counts.items(),
                                      key=lambda x: -x[1])),
        vcolor="magenta"))
    out.echo(kv("By Country", ", ".join(
        f"{k}={v}" for k, v in sorted(country_counts.items(),
                                      key=lambda x: -x[1])), vcolor="green"))
    out.echo()


def print_bulk(records, query, out):
    out.echo()
    out.echo(color(f"  Bulk Search: {query}", "bold"))
    out.echo(color(f"  Unique records: {len(records)}", "gray"))

    if not records:
        out.echo(color("\n  No results found.", "yellow"))
        return

    out.echo()
    headers = ("Resource / IP", "Plugin(s)", "Severity",
               "Ports", "Leak Events", "Country")
    rows = []
    for r in records:
        events = r.get("events") or []
        plugins = r.get("plugins") or []
        sevs = sorted({(e.get("leak") or {}).get("severity", "")
                       for e in events if (e.get("leak") or {}).get("severity")})
        ports = r.get("open_ports") or []
        geo = r.get("geoip") or {}
        resource = r.get("resource_id") or r.get("ip") or "-"
        rows.append((
            resource,
            ", ".join(plugins) or "-",
            ", ".join(s.upper() for s in sevs) or "-",
            ", ".join(ports) or "-",
            str(r.get("leak_event_count", r.get("event_count", len(events)))),
            geo.get("country_name") or "-",
        ))
    out.echo(_table(headers, rows,
                    colors=["cyan", "magenta", "yellow", "orange", "gray", None]))

    # Vulnerable URLs across all bulk records
    seen = set()
    url_rows = []
    for r in records:
        for plug, sev, u in _group_leak_urls(r):
            key = (plug, u)
            if key in seen:
                continue
            seen.add(key)
            url_rows.append((plug, sev or "-", u))
    if url_rows:
        out.echo(color("\n  ── Vulnerable URLs ──", "bold"))
        out.echo(_table(("Plugin", "Severity", "URL"), url_rows,
                        colors=["magenta", "yellow", "blue"]))

    plugin_counts, sev_counts, country_counts = {}, {}, {}
    total_leak_events = 0
    distinct_ips = set()
    for r in records:
        for p in (r.get("plugins") or ["unknown"]):
            plugin_counts[p] = plugin_counts.get(p, 0) + 1
        for e in (r.get("events") or []):
            sv = ((e.get("leak") or {}).get("severity") or "none").lower()
            sev_counts[sv] = sev_counts.get(sv, 0) + 1
        cn = (r.get("geoip") or {}).get("country_name") or "unknown"
        country_counts[cn] = country_counts.get(cn, 0) + 1
        total_leak_events += r.get("leak_event_count", 0)
        if r.get("ip"):
            distinct_ips.add(r["ip"])

    distinct_resources = len({r.get("resource_id") for r in records
                              if r.get("resource_id")})

    out.echo(color("\n  ── Summary ──", "bold"))
    out.echo(kv("Unique Records", len(records), vcolor="cyan"))
    out.echo(kv("Distinct Resources", distinct_resources, vcolor="cyan"))
    out.echo(kv("Distinct IPs", len(distinct_ips), vcolor="cyan"))
    out.echo(kv("Total Leak Events", total_leak_events, vcolor="yellow"))
    out.echo(kv("By Plugin", ", ".join(
        f"{k}={v}" for k, v in sorted(plugin_counts.items(),
                                      key=lambda x: -x[1])), vcolor="magenta"))
    out.echo(kv("By Country", ", ".join(
        f"{k}={v}" for k, v in sorted(country_counts.items(),
                                      key=lambda x: -x[1])), vcolor="green"))
    out.echo()


# ---------------- CLI ----------------
def _parse_pages(value):
    if str(value).lower() == "all":
        return None
    try:
        return max(1, int(value))
    except ValueError:
        print("Error: --pages must be a number or 'all'", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="LeakIX scraper/parser")
    parser.add_argument("--show-plugins", action="store_true",
                        help="Scrape and display all plugins in a table")
    parser.add_argument("--host", metavar="IP",
                        help="Query the LeakIX host endpoint for an IP")
    parser.add_argument("--domain", metavar="DOMAIN",
                        help="Query the LeakIX domain endpoint for a domain")
    parser.add_argument("--domains", metavar="FILE",
                        help="Read domains from FILE (one per line) and run a "
                             "domain query on each; saves to "
                             "bulk_output/<domain>/")
    parser.add_argument("--subdomains", metavar="DOMAIN",
                        help="List known subdomains for a domain")
    parser.add_argument("--bulk-search", metavar="QUERY",
                        help="Bulk search (grouped results)")
    parser.add_argument("--search", metavar="QUERY",
                        help="Search (one l9event per leak)")
    parser.add_argument("--scope", default="leak", choices=["leak", "service"],
                        help="Scope for --search (default: leak)")
    parser.add_argument("--pages", default="all", metavar="N",
                        help="Pages to fetch for --search/--bulk-search: "
                             "a number, or 'all' (default: all)")
    parser.add_argument("--no-cards", action="store_true",
                        help="For --search: skip detailed per-result cards")
    parser.add_argument("--no-save", action="store_true",
                        help="Do not save output to a file")
    parser.add_argument("--api-key", metavar="KEY",
                        default=os.environ.get("LEAKIX_API_KEY"),
                        help="LeakIX API key (or set LEAKIX_API_KEY env var)")
    parser.add_argument("--raw", action="store_true",
                        help="Dump raw JSON/NDJSON of the response")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose per-page diagnostics for search/bulk")
    args = parser.parse_args()

    def need_key():
        if not args.api_key:
            print("Error: no API key. Use --api-key KEY or set "
                  "LEAKIX_API_KEY environment variable.", file=sys.stderr)
            sys.exit(1)

    out = Tee()

    if args.show_plugins:
        try:
            plugins = scrape_plugins()
        except requests.RequestException as e:
            print(f"Error fetching plugins: {e}", file=sys.stderr)
            sys.exit(1)
        print_plugins_table(plugins, out)
        if not args.no_save:
            save_output("plugins", "all", out)

    elif args.search:
        need_key()
        max_pages = _parse_pages(args.pages)
        try:
            records, raw, stats = fetch_search_all(
                args.search, args.api_key, scope=args.scope,
                max_pages=max_pages, debug=args.debug)
        except requests.RequestException as e:
            print(f"Error querying API: {e}", file=sys.stderr)
            sys.exit(1)
        if args.raw:
            print(raw)
        else:
            print_search(records, args.search, args.scope, out,
                         stats=stats, show_cards=not args.no_cards)
        if not args.no_save:
            save_output("search", args.search, out, raw_text=raw)

    elif args.bulk_search:
        need_key()
        max_pages = _parse_pages(args.pages)
        try:
            records, raw = fetch_bulk_all(args.bulk_search, args.api_key,
                                          max_pages=max_pages, debug=args.debug)
        except requests.RequestException as e:
            print(f"Error querying API: {e}", file=sys.stderr)
            sys.exit(1)
        if args.raw:
            print(raw)
        else:
            print_bulk(records, args.bulk_search, out)
        if not args.no_save:
            save_output("bulk", args.bulk_search, out, raw_text=raw)

    elif args.subdomains:
        need_key()
        try:
            data = query_subdomains(args.subdomains, args.api_key)
        except requests.RequestException as e:
            print(f"Error querying API: {e}", file=sys.stderr)
            sys.exit(1)
        if args.raw:
            print(json.dumps(data, indent=2))
        else:
            print_subdomains(data, args.subdomains, out)
        if not args.no_save:
            save_output("subdomains", args.subdomains, out,
                        raw_text=json.dumps(data, indent=2))

    elif args.domains:
        need_key()
        try:
            domains = read_domains(args.domains)
        except OSError as e:
            print(f"Error reading domains file: {e}", file=sys.stderr)
            sys.exit(1)
        if not domains:
            print(f"No domains found in {args.domains}", file=sys.stderr)
            sys.exit(1)

        total = len(domains)
        sys.stderr.write(color(
            f"\n  Loaded {total} domain(s) from {args.domains}\n", "cyan"))

        for i, domain in enumerate(domains, 1):
            sys.stderr.write(color(
                f"\n[{i}/{total}] Querying domain: {domain}\n", "bold"))
            try:
                data = query_domain(domain, args.api_key)
            except requests.RequestException as e:
                sys.stderr.write(color(
                    f"  Error querying {domain}: {e}\n", "red"))
                continue

            dom_out = Tee()  # fresh buffer so each file holds one domain only
            if args.raw:
                print(json.dumps(data, indent=2))
            else:
                print_result(data, f"Domain {domain}", dom_out, is_domain=True)

            if not args.no_save:
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", domain)
                domain_dir = os.path.join(BULK_OUTPUT_DIR, safe)
                save_output("domain", domain, dom_out,
                            raw_text=json.dumps(data, indent=2),
                            output_dir=domain_dir)

    elif args.host or args.domain:
        need_key()
        try:
            if args.host:
                data = query_host(args.host, args.api_key)
                label = f"Host {args.host}"
                ident = args.host
                is_domain = False
            else:
                data = query_domain(args.domain, args.api_key)
                label = f"Domain {args.domain}"
                ident = args.domain
                is_domain = True
        except requests.RequestException as e:
            print(f"Error querying API: {e}", file=sys.stderr)
            sys.exit(1)
        if args.raw:
            print(json.dumps(data, indent=2))
        else:
            print_result(data, label, out, is_domain=is_domain)
        if not args.no_save:
            kind = "domain" if is_domain else "host"
            save_output(kind, ident, out, raw_text=json.dumps(data, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
