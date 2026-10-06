#!/usr/bin/env python3
"""
Update DHCP relay servers on LAN interfaces for specified sites.

Usage:
    python update_dhcp_relay.py --csv relay_updates.csv [--dry-run]

CSV format (no header row assumed, or with header "site_name,relay_ips"):
    SiteName,10.0.0.1,10.0.0.2,...
    or with a header row:
    site_name,relay_ips   (then data rows: SiteName,"10.0.0.1,10.0.0.2")

The script accepts two CSV layouts:
  1. Wide:  site_name, ip1, ip2, ip3, ...  (one IP per column)
  2. Narrow: site_name, "ip1,ip2,ip3"      (IPs comma-separated in second column)
"""

import prisma_sase
import argparse
import sys
import os
import csv
import ipaddress
import json

sys.path.append(os.getcwd())

try:
    from prismasase_settings import PRISMASASE_CLIENT_ID, PRISMASASE_CLIENT_SECRET, PRISMASASE_TSG_ID
except ImportError:
    PRISMASASE_CLIENT_ID = None
    PRISMASASE_CLIENT_SECRET = None
    PRISMASASE_TSG_ID = None


def parse_relay_csv(csv_path: str) -> dict:
    """
    Returns {site_name: [relay_ip, ...]} from the input CSV.
    Handles both wide (ip per column) and narrow ("ip1,ip2" in col 2) layouts.
    Header row is optional — if the first cell looks like an IP it's treated as data.
    """
    relay_map = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or not row[0].strip():
                continue
            site_name = row[0].strip()
            # Skip a header row whose first column is not an IP and looks like a label
            if site_name.lower() in {"site_name", "site", "name"}:
                continue
            raw_ips = []
            if len(row) == 2:
                # Narrow layout: second column may be "10.0.0.1,10.0.0.2"
                raw_ips = [ip.strip() for ip in row[1].split(",") if ip.strip()]
            elif len(row) > 2:
                # Wide layout: ip1, ip2, ip3 ...
                raw_ips = [cell.strip() for cell in row[1:] if cell.strip()]
            # Validate IPs
            valid_ips = []
            for ip in raw_ips:
                try:
                    ipaddress.IPv4Address(ip)
                    valid_ips.append(ip)
                except ipaddress.AddressValueError:
                    print(f"  [WARN] Skipping invalid IP '{ip}' for site '{site_name}'")
            if valid_ips:
                relay_map[site_name] = valid_ips
            else:
                print(f"  [WARN] No valid relay IPs for site '{site_name}' — skipping")
    return relay_map


def build_dhcp_relay_config(relay_ips: list) -> dict:
    """Return the dhcp_relay payload expected by the Prisma SASE API."""
    return {
        "enabled": True,
        "server_ips": relay_ips,
    }


def update_dhcp_relays(cgx, relay_map: dict, dry_run: bool = False):
    """
    Iterate all sites, find LAN interfaces that have a dhcp_relay configured,
    and update the relay server IPs to what is specified in relay_map.
    """
    matched_sites = set()
    results = []

    print(f"\nFetching sites...")
    sites_resp = cgx.get.sites()
    sites = sites_resp.cgx_content.get("items", [])
    print(f"  Found {len(sites)} sites")

    print(f"Fetching elements...")
    elements_resp = cgx.get.elements()
    elements = elements_resp.cgx_content.get("items", [])

    # Build element lookup by site_id
    elements_by_site = {}
    for el in elements:
        elements_by_site.setdefault(el["site_id"], []).append(el)

    for site in sites:
        site_name = site["name"]
        site_id = site["id"]

        if site_name not in relay_map:
            continue

        matched_sites.add(site_name)
        new_relay_ips = relay_map[site_name]
        print(f"\n[{site_name}] Relay IPs to set: {new_relay_ips}")

        for element in elements_by_site.get(site_id, []):
            element_id = element["id"]
            element_name = element.get("name") or "no-name-element"

            ifaces_resp = cgx.get.interfaces(site_id=site_id, element_id=element_id)
            interfaces = ifaces_resp.cgx_content.get("items", [])

            for iface in interfaces:
                if iface.get("used_for") != "lan":
                    continue
                if not iface.get("dhcp_relay"):
                    continue

                iface_name = iface.get("name", "unknown")
                iface_id = iface["id"]
                current_relay = iface["dhcp_relay"]
                current_ips = current_relay.get("server_ips", [])

                if sorted(current_ips) == sorted(new_relay_ips):
                    print(f"  [{element_name}/{iface_name}] Already up to date — skipping")
                    results.append({
                        "site": site_name, "element": element_name,
                        "interface": iface_name, "status": "no_change",
                        "old_ips": current_ips, "new_ips": new_relay_ips,
                    })
                    continue

                print(f"  [{element_name}/{iface_name}] {current_ips} -> {new_relay_ips}", end="")

                if dry_run:
                    print("  [DRY RUN — no change made]")
                    results.append({
                        "site": site_name, "element": element_name,
                        "interface": iface_name, "status": "dry_run",
                        "old_ips": current_ips, "new_ips": new_relay_ips,
                    })
                    continue

                # Patch dhcp_relay in the interface payload
                iface["dhcp_relay"]["server_ips"] = new_relay_ips

                put_resp = cgx.put.interfaces(
                    site_id=site_id,
                    element_id=element_id,
                    interface_id=iface_id,
                    data=iface,
                )
                if put_resp.cgx_status:
                    print("  [OK]")
                    results.append({
                        "site": site_name, "element": element_name,
                        "interface": iface_name, "status": "updated",
                        "old_ips": current_ips, "new_ips": new_relay_ips,
                    })
                else:
                    print(f"  [FAILED] {put_resp.cgx_content}")
                    results.append({
                        "site": site_name, "element": element_name,
                        "interface": iface_name, "status": "failed",
                        "old_ips": current_ips, "new_ips": new_relay_ips,
                        "error": str(put_resp.cgx_content),
                    })

    # Report any sites from the CSV that weren't found in the tenant
    missing = set(relay_map.keys()) - matched_sites
    if missing:
        print(f"\n[WARN] The following sites from the CSV were NOT found in the tenant:")
        for s in sorted(missing):
            print(f"  - {s}")

    # Summary
    print("\n--- Summary ---")
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    for status, count in counts.items():
        print(f"  {status}: {count}")

    return results


def go():
    parser = argparse.ArgumentParser(
        description="Update DHCP relay IPs on LAN interfaces for specified branch sites."
    )
    parser.add_argument(
        "--csv", required=True,
        help=(
            "Path to CSV file. Format: site_name, relay_ip1, relay_ip2, ... "
            "(wide) or site_name, \"ip1,ip2\" (narrow). Optional header row."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without making any API calls.",
    )
    args = parser.parse_args()

    relay_map = parse_relay_csv(args.csv)
    if not relay_map:
        print("No valid entries found in CSV — exiting.")
        sys.exit(1)

    print(f"\nLoaded {len(relay_map)} site(s) from CSV:")
    for site, ips in relay_map.items():
        print(f"  {site}: {ips}")

    sase_session = prisma_sase.API()
    sase_session.interactive.login_secret(
        client_id=PRISMASASE_CLIENT_ID,
        client_secret=PRISMASASE_CLIENT_SECRET,
        tsg_id=PRISMASASE_TSG_ID,
    )

    update_dhcp_relays(sase_session, relay_map, dry_run=args.dry_run)


if __name__ == "__main__":
    go()
