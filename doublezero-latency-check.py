#!/usr/bin/env python3
import argparse
import subprocess, re, sys, shutil, time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================== Tunables ==================
PING_COUNT = 2            # echo requests per IP
PING_TIMEOUT_S = 1        # per-echo timeout (seconds)
MAX_WORKERS = 32          # concurrent pings
TOGGLE_TUNNEL = True      # default behavior (can be disabled with --no-toggle)
WAIT_TIMEOUT_S = 180      # max time to wait for DZ status to change (seconds)
POLL_INTERVAL_S = 2       # polling interval for DZ status (seconds)
# ==============================================

IPV4 = re.compile(r'^\d+\.\d+\.\d+\.\d+$')

# ---------- requirement checks ----------
def check_requirements():
    missing = []
    if shutil.which("solana") is None:
        missing.append("solana (Solana CLI)")
    if shutil.which("doublezero") is None:
        missing.append("doublezero (Doublezero CLI)")
    if shutil.which("ping") is None:
        missing.append("ping (system ping tool)")
    if shutil.which("curl") is None:
        missing.append("curl (for external IP detection)")
    if missing:
        print("ERROR: Missing required tools:")
        for m in missing:
            print(f"  - {m}")
        sys.exit(1)

# ---------- helpers ----------
def run(cmd, timeout=None, check=False):
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=check)

def get_external_ip():
    curl = shutil.which("curl")
    if not curl:
        return "curl-not-found"
    try:
        p = run([curl, "-sS", "--max-time", "3", "ifconfig.me"], timeout=5)
        ip = (p.stdout or "").strip()
        return ip if IPV4.match(ip) else "unknown"
    except Exception:
        return "unknown"

# ---------- doublezero status ----------
def parse_dz_status_table(out: str):
    lines = [ln for ln in out.splitlines() if ln.strip()]
    header_idx = None
    for i, ln in enumerate(lines):
        if "|" in ln and "Tunnel status" in ln:
            header_idx = i
            break
    if header_idx is None:
        return None
    headers = [h.strip().lower() for h in lines[header_idx].split("|")]
    for j in range(header_idx + 1, len(lines)):
        rowline = lines[j]
        if set(rowline.strip()) <= set(" -+|"):
            continue
        parts = [p.strip() for p in rowline.split("|")]
        return dict(zip(headers, parts))
    return None

def get_dz_status():
    try:
        p = run(["doublezero", "status"], timeout=12)
        row = parse_dz_status_table(p.stdout)
        if not row:
            return {"status": "unknown", "is_up": False}
        status = row.get("tunnel status", "").lower()
        return {
            "status": status,
            "is_up": (status == "up"),
        }
    except Exception:
        return {"status": "unknown", "is_up": False}

def dz_connect():
    try:
        run(["doublezero", "connect", "ibrl"], timeout=20)
    except Exception:
        pass

def dz_disconnect():
    try:
        run(["doublezero", "disconnect"], timeout=20)
    except Exception:
        pass

def wait_for_status(target_status: str, timeout_s: int = WAIT_TIMEOUT_S, poll_interval_s: int = POLL_INTERVAL_S) -> bool:
    """
    Wait until `doublezero status` equals target_status ('up' or 'disconnected').
    Prints progress while waiting. Returns True on success, False on timeout.
    """
    assert target_status in ("up", "disconnected")
    verb = "to be connected (up)" if target_status == "up" else "to be disconnected"
    start = time.time()
    while True:
        st = get_dz_status()
        curr = st.get("status", "unknown")
        if curr == target_status:
            return True
        elapsed = time.time() - start
        if elapsed >= timeout_s:
            print(f"Timeout while waiting {verb}. Last seen status: {curr}")
            return False
        print(f"Waiting for DZ {verb}... current status: {curr}")
        time.sleep(poll_interval_s)

# ---------- data collection ----------
def get_client_ips():
    out = subprocess.check_output(["doublezero", "user", "list"], text=True, errors="ignore")
    ips = set()
    for line in out.splitlines():
        parts = line.split('|')
        if len(parts) >= 7:
            ip = parts[6].strip()
            if IPV4.match(ip):
                ips.add(ip)
    return ips

def get_gossip_pairs(gossip_cmd):
    """
    gossip_cmd: list, e.g. ["solana","gossip","-um"] for mainnet
                          or ["solana","gossip","-ut"] for testnet
    """
    out = subprocess.check_output(gossip_cmd, text=True, errors="ignore")
    pairs = []
    for line in out.splitlines():
        if '|' not in line or line.startswith('-') or line.strip().startswith('IP Address'):
            continue
        parts = [p.strip() for p in line.split('|')]
        if len(parts) >= 2 and IPV4.match(parts[0]):
            pairs.append((parts[0], parts[1]))
    return pairs

def parse_ping_avg_ms(stdout: str):
    m = re.search(r'rtt .* = [\d\.]+/([\d\.]+)/[\d\.]+/[\d\.]+ ms', stdout)
    if m: return float(m.group(1))
    m = re.search(r'round-trip .* = [\d\.]+/([\d\.]+)/[\d\.]+ ms', stdout)
    if m: return float(m.group(1))
    times = [float(x) for x in re.findall(r'time[=<]([\d\.]+)\s*ms', stdout)]
    return sum(times)/len(times) if times else None

def ping_ip(ip: str) -> str:
    ping_bin = shutil.which("ping")
    if not ping_bin:
        return "ping not found"
    cmd = [ping_bin, "-n", "-c", str(PING_COUNT), "-W", str(PING_TIMEOUT_S), ip]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PING_COUNT*(PING_TIMEOUT_S+1)+2)
    except subprocess.TimeoutExpired:
        return "timeout"
    avg = parse_ping_avg_ms(proc.stdout or "")
    if avg is not None:
        return f"{avg:.2f} ms"
    combined = (proc.stdout + proc.stderr).lower()
    if "unreachable" in combined: return "unreachable"
    if "permission denied" in combined or "icmp" in combined: return "icmp blocked"
    if proc.returncode != 0: return "timeout"
    return "icmp blocked"

def collect_matches(gossip_cmd):
    client_ips = get_client_ips()
    pairs = get_gossip_pairs(gossip_cmd)
    return [(ip, ident) for ip, ident in pairs if ip in client_ips]

def run_latency_test(label: str, gossip_cmd):
    matches = collect_matches(gossip_cmd)
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        fut_to_ip = {ex.submit(ping_ip, ip): ip for ip, _ in matches}
        for fut in as_completed(fut_to_ip):
            results[fut_to_ip[fut]] = fut.result()
    return {ip: {"identity": ident, "latency": results[ip]} for ip, ident in matches}

# ---------- compare / print ----------
def parse_ms(s: str):
    if not s: return None, s
    m = re.match(r"^(\d+(?:\.\d+)?)\s*ms$", s.strip(), re.IGNORECASE)
    return (float(m.group(1)), None) if m else (None, s.lower())

def print_single_run(label: str, data: dict):
    print(f"\nOnly '{label}' measurements were taken (--no-toggle)")
    print(f"{'ip_address':<16}  {'identity':<44}  latency")
    for ip in sorted(data.keys()):
        print(f"{ip:<16}  {data[ip].get('identity','')[:44]:<44}  {data[ip].get('latency','n/a')}")

def compare_and_print(conn_data: dict, disc_data: dict):
    all_ips = sorted(set(conn_data) | set(disc_data))
    comparable, skipped = [], []
    only_connected, only_disconnected, both_non_numeric = 0, 0, 0

    for ip in all_ips:
        conn = conn_data.get(ip, {})
        disc = disc_data.get(ip, {})
        ident = conn.get("identity") or disc.get("identity") or ""

        conn_raw = conn.get("latency", "")
        disc_raw = disc.get("latency", "")
        conn_ms, _ = parse_ms(conn_raw)
        disc_ms, _ = parse_ms(disc_raw)

        if conn_ms is None or disc_ms is None:
            if conn_ms is not None and disc_ms is None: only_connected += 1
            elif conn_ms is None and disc_ms is not None: only_disconnected += 1
            else: both_non_numeric += 1
            skipped.append({"ip": ip,"identity": ident,
                            "latency_connected": conn_raw or "n/a",
                            "latency_disconnected": disc_raw or "n/a"})
            continue

        delta = conn_ms - disc_ms
        pct = (delta/disc_ms)*100 if disc_ms>0 else 0
        comparable.append({"ip": ip,"identity": ident,
                           "conn_ms": conn_ms,"disc_ms": disc_ms,
                           "delta": delta,"pct": pct})

    better = [r for r in comparable if r["delta"]<0]
    same   = [r for r in comparable if r["delta"]==0]
    worse  = [r for r in comparable if r["delta"]>0]
    better.sort(key=lambda r:r["delta"])
    same.sort(key=lambda r:(r["conn_ms"],r["ip"]))
    worse.sort(key=lambda r:r["delta"], reverse=True)

    total = len(comparable)+len(skipped)
    print("\n=== Latency comparison summary ===")
    print(f"Total peers: {total}")
    print(f"Better (connected < disconnected): {len(better)}")
    print(f"Same   (equal values)            : {len(same)}")
    print(f"Worse  (connected > disconnected): {len(worse)}")
    print(f"Skipped (non-numeric, ICMP blocked/timeout): {len(skipped)} "
          f"[only connected measured: {only_connected}; "
          f"only disconnected measured: {only_disconnected}; "
          f"both: {both_non_numeric}]")

    def fmt(ms): return f"{ms:.2f}"
    def fmt_delta(d): return f"{d:+.2f}"
    def fmt_pct(p): return f"{p:+.2f}"

    def print_block(title, rows):
        print(f"\n{title}")
        if not rows: print("(no entries)"); return
        print(f"{'ip_address':<16}{'conn_ms':>10}{'disc_ms':>10}{'delta_ms':>10}{'pct':>8}  identity")
        for r in rows:
            print(f"{r['ip']:<16}{fmt(r['conn_ms']):>10}{fmt(r['disc_ms']):>10}{fmt_delta(r['delta']):>10}{fmt_pct(r['pct']):>8}  {r['identity'][:40]}")

    print_block("improvements (connected faster):", better)
    print_block("same (exactly equal):",           same)
    print_block("regressions (connected slower):", worse)

    if skipped:
        print("\nSkipped peers (could not measure with ICMP):")
        print(f"{'ip_address':<16}{'lat_conn':<20}{'lat_disc':<20}identity")
        for r in skipped:
            print(f"{r['ip']:<16}{r['latency_connected']:<20}{r['latency_disconnected']:<20}{r['identity'][:40]}")

# ---------- main ----------
def main():
    check_requirements()

    parser = argparse.ArgumentParser(description="Measure Solana peer latencies with and without Doublezero and compare.")
    net = parser.add_mutually_exclusive_group(required=True)
    net.add_argument("--mainnet", action="store_true", help="Use Solana mainnet gossip (-um)")
    net.add_argument("--testnet", action="store_true", help="Use Solana testnet gossip (-ut)")
    parser.add_argument("--no-toggle", action="store_true", help="Do not toggle DZ; measure only current state")
    args = parser.parse_args()

    gossip_cmd = ["solana", "gossip", "-um"] if args.mainnet else ["solana", "gossip", "-ut"]
    toggle = TOGGLE_TUNNEL and (not args.no_toggle)

    # Safety confirmation before touching the tunnel
    if toggle:
        print("\n⚠️  WARNING: This script will disconnect and reconnect your Doublezero tunnel")
        print("   to measure latencies in both states.")
        print("   This may interrupt validator or RPC traffic temporarily.\n")
        confirm = input("Do you want to continue? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Aborting on user request.")
            sys.exit(0)

    external_ip = get_external_ip()
    initial_dz = get_dz_status()
    print(f"External IP: {external_ip}")
    print(f"DZ status: {initial_dz.get('status')} (is_up={initial_dz.get('is_up')})")

    conn_data, disc_data = {}, {}
    if initial_dz.get("is_up"):
        conn_data = run_latency_test("connected", gossip_cmd)
        if toggle:
            print("\nDisconnecting DZ...")
            dz_disconnect()
            ok = wait_for_status("disconnected")
            if not ok:
                print("WARN: Could not confirm DZ is disconnected; skipping disconnected test.")
            else:
                disc_data = run_latency_test("disconnected", gossip_cmd)
            print("Reconnecting DZ...")
            dz_connect()
            ok2 = wait_for_status("up")
            if not ok2:
                print("WARN: Could not confirm DZ is back up. Please check manually.")
    else:
        disc_data = run_latency_test("disconnected", gossip_cmd)
        if toggle:
            print("\nConnecting DZ...")
            dz_connect()
            ok = wait_for_status("up")
            if not ok:
                print("WARN: Could not confirm DZ is connected; skipping connected test.")
            else:
                conn_data = run_latency_test("connected", gossip_cmd)
            print("Restoring DZ disconnected...")
            dz_disconnect()
            ok2 = wait_for_status("disconnected")
            if not ok2:
                print("WARN: Could not confirm DZ is disconnected again. Please check manually.")

    # Output
    if conn_data and disc_data:
        compare_and_print(conn_data, disc_data)
    else:
        # --no-toggle path: print the single run table
        if conn_data:
            print_single_run("connected", conn_data)
        elif disc_data:
            print_single_run("disconnected", disc_data)
        else:
            print("\nNo measurements completed.")

if __name__ == "__main__":
    main()
