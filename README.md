# DHCP Relay Updater

Updates DHCP relay server IPs on LAN interfaces across Prisma SD-WAN branch sites using a CSV input file.

---

## Scripts

| Script | Purpose |
|--------|---------|
| `update_dhcp_relay.py` | Reads a CSV and updates DHCP relay IPs on matching LAN interfaces |
| `get_ips.py` | Exports all interface IPs to `interface_ip.csv` (read-only audit) |

---

## Prerequisites

- Python 3.8+
- `prisma_sase` SDK installed (`pip install prisma-sase`)
- A `prismasase_settings.py` file in the same directory with your credentials:

```python
PRISMASASE_CLIENT_ID     = "your-client-id"
PRISMASASE_CLIENT_SECRET = "your-client-secret"
PRISMASASE_TSG_ID        = "your-tsg-id"
```

---

## CSV Format

Create a CSV file with one row per site. Two layouts are supported:

### Wide format (one IP per column)
```
site_name,relay_ip1,relay_ip2
Branch-Austin,10.10.1.1,10.10.1.2
Branch-Dallas,10.20.1.1,10.20.1.2
Branch-Houston,10.30.1.1,
```

### Narrow format (IPs in a single quoted cell)
```
site_name,relay_ips
Branch-Austin,"10.10.1.1,10.10.1.2"
Branch-Dallas,"10.20.1.1,10.20.1.2"
Branch-Houston,10.30.1.1
```

**Notes:**
- The header row is optional and auto-detected
- Site names must match the tenant exactly (case-sensitive)
- Trailing empty columns and blank cells are ignored
- Invalid IPs are flagged and skipped with a warning

---

## Usage

### 1. Dry run (recommended first step)

Preview all changes without writing anything to the tenant:

```bash
python update_dhcp_relay.py --csv relay_updates.csv --dry-run
```

### 2. Apply changes

```bash
python update_dhcp_relay.py --csv relay_updates.csv
```

---

## What the Script Does

1. Parses and validates the CSV
2. Fetches all sites and elements from the tenant
3. For each site listed in the CSV, iterates every ION element's interfaces
4. Targets only interfaces where `used_for = LAN` **and** a DHCP relay is already configured
5. Skips interfaces already set to the target relay IPs
6. PUTs the updated interface payload with the new relay server IPs
7. Prints a summary of outcomes and warns on any CSV sites not found in the tenant

> **Note:** The script will not add a DHCP relay to an interface that doesn't already have one configured. It only updates existing relay configurations.

---

## Output

Console output shows progress per site and interface:

```
[Branch-Austin] Relay IPs to set: ['10.10.1.1', '10.10.1.2']
  [ION-Austin-1/LAN1] ['10.0.0.1'] -> ['10.10.1.1', '10.10.1.2']  [OK]
  [ION-Austin-1/LAN2] Already up to date — skipping

--- Summary ---
  updated: 1
  no_change: 1
```

### Status codes

| Status | Meaning |
|--------|---------|
| `updated` | Relay IPs successfully changed |
| `no_change` | Interface already had the correct relay IPs |
| `dry_run` | Would have been updated (dry run mode) |
| `failed` | API call failed — check the error message |

---

## Troubleshooting

**Site not found warning**
> The site name in your CSV doesn't match the tenant. Check for typos or leading/trailing spaces.

**Interface skipped silently**
> The interface either isn't `used_for = LAN` or has no existing DHCP relay configured.

**API call failed**
> Check your credentials in `prismasase_settings.py` and that your service account has write access to element interfaces.
