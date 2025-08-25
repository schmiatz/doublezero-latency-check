# Doublezero Latency Check

Measure Solana peer **latency with and without Doublezero (DZ)** and compare the results.  
The script pings only the peers that appear in **both**:
- `doublezero user list` (takes the `client_ip` column), and
- `solana gossip` (takes `IP Address` + `Identity` for the chosen network)

It then:
- runs ICMP pings to those matched peers,
- (optionally) toggles your DZ tunnel to measure **connected vs. disconnected**,
- prints a clear summary: **improvements**, **same**, **regressions**, and **skipped** (ICMP blocked/timeout).

> ⚠️ By default the script will **disconnect and reconnect** your DZ tunnel once to run both measurements. You can disable this with `--no-toggle` (see [Modes](#modes)).

---

## Requirements

- **Linux** recommended (uses `ping` flags compatible with iputils/BusyBox).
- **Solana CLI** (`solana`) installed and in `$PATH`.
- **Doublezero CLI** (`doublezero`) installed and in `$PATH`.
- Ability to **connect to Doublezero** (you need a valid DZ setup and a **whitelisted key** that can connect, e.g., IBRL).
- **curl** (for external IP detection).
- **ping** (system ICMP ping tool).

The script checks for these tools on startup and exits with an error if any are missing.

---

## Install

1. Clone the Repo. :
   ```bash
   git clone git@github.com:schmiatz/doublezero-latency-check.git
   ```
2. Make it executable:
   ```bash
   cd doublezero-latency-check && chmod +x dz-latencies.py
   ```
3. run it:
   ```bash
   ./dz-latencies.py --mainnet
   ```

## Usage

Choose the Solana network and whether to toggle the DZ tunnel.

## Mainnet
```bash
# Compare connected vs. disconnected (will toggle DZ, with confirmation)
./dz-latencies.py --mainnet

# Measure only current state (no tunnel changes)
./dz-latencies.py --mainnet --no-toggle
```
## Testnet
```bash
# Compare connected vs. disconnected (will toggle DZ, with confirmation)
./dz-latencies.py --testnet

# Measure only current state (no tunnel changes)
./dz-latencies.py --testnet --no-toggle
```

## What the script does (step by step)

1. **Requirement checks**  
   Verifies `solana`, `doublezero`, `ping`, and `curl` are available.

2. **Network selection**  
   - `--mainnet` → uses `solana gossip -um`  
   - `--testnet` → uses `solana gossip -ut`

3. **Discover current state**  
   - Detects external IP via `curl ifconfig.me`.  
   - Reads Doublezero status via `doublezero status`.

4. **Confirm before toggling (if not `--no-toggle`)**  
   Prints a clear warning that DZ will be disconnected/reconnected and asks you to confirm.  
   If you answer `y/yes`, it proceeds; otherwise it exits without changes.

5. **Build the target set of peers**  
   - Runs `doublezero user list`, collects all `client_ip` values.  
   - Runs `solana gossip (-um | -ut)`, extracts `IP Address` + `Identity`.  
   - Intersects the two sets → only peers present in **both** are probed.

6. **Run the latency tests**  
   - **If DZ is currently UP:**
     - Measure **connected** first (ICMP ping to each matched peer).
     - If toggling is enabled:
       - `doublezero disconnect`, actively wait until `doublezero status` reports **disconnected**.
       - Measure **disconnected**.
       - `doublezero connect ibrl`, actively wait until `doublezero status` reports **up**.
   - **If DZ is currently DOWN:**
     - Measure **disconnected** first.
     - If toggling is enabled:
       - `doublezero connect ibrl`, actively wait until **up**.
       - Measure **connected**.
       - `doublezero disconnect`, actively wait until **disconnected**.

   Pings are run **concurrently** (thread pool).  
   The script parses **average latency** from ping output. If no number can be parsed, the result is marked as `timeout`, `unreachable`, `icmp blocked`, or `ping not found`.

7. **Compare & print results (only when both runs exist)**  
   - **Better**: connected < disconnected  
   - **Same**: connected == disconnected  
   - **Worse**: connected > disconnected  
   - **Skipped**: one or both sides not numeric (blocked/timeout/etc.)  
     - **only connected measured**: connected numeric; disconnected not  
     - **only disconnected measured**: disconnected numeric; connected not  
     - **both**: neither numeric  

   The script prints:  
   - A **summary** with totals and the skipped breakdown.  
   - Full lists for **improvements**, **same**, **regressions** (sorted for readability).  
   - A **skipped table** with raw statuses for each side.

8. **Single-run output (when `--no-toggle`)**  
   - If DZ is **up**, prints: *“Only 'connected' measurements were taken (--no-toggle)”* and a table of IPs/identities/latencies.  
   - If DZ is **down**, prints: *“Only 'disconnected' measurements were taken (--no-toggle)”* and the same kind of table.  
   - No comparison is performed in this mode.
  
## Mode
## Modes

| Mode                   | Command example                | What happens                                                                                               | When to use                                                                 |
|------------------------|--------------------------------|------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| **Comparison (default)** | `./dz-latencies.py --mainnet`   | Measures **connected**, toggles DZ, measures **disconnected**, restores original state, then prints full comparison (better/same/worse/skipped). | When you want to objectively compare DZ vs. non-DZ latency.                  |
| **Single-run (no toggle)** | `./dz-latencies.py --mainnet --no-toggle` | Measures only the **current state** (connected if DZ is up; disconnected if DZ is down). Prints a simple table of latencies; **no** comparison. | Safe mode for production/cron or when you don’t want any tunnel changes.     |

## Output anatomy

- **Summary**
  ```text
  === Latency comparison summary ===
  Total peers: 129
  Better (connected < disconnected): 47
  Same   (equal values)            : 2
  Worse  (connected > disconnected): 69
  Skipped (non-numeric, ICMP blocked/timeout): 11 [only connected measured: 4; only disconnected measured: 0; both: 7]
  ```
- **Lists**
  - **improvements (connected faster)**: full list, most improved first.  
  - **same**: exact ties.  
  - **regressions (connected slower)**: full list, worst first.  
  - **skipped**: peers where ICMP wasn’t measurable on one/both sides, with the raw statuses shown for each side.

---

## Notes & caveats

- **ICMP only**: The script uses ICMP ping exclusively. If a peer blocks ICMP, it will show as `icmp blocked`/`timeout` and be counted under **Skipped**.  
- **Linux recommended**: `ping` options vary by OS. The script targets iputils/BusyBox semantics.  
- **Privileges**: On some systems, ICMP may require privileges/capabilities. If you see `permission denied`, adjust your environment accordingly.  
- **DZ actions**: With the default mode, the script **disconnects and reconnects** your DZ tunnel once. There’s a confirmation prompt; use `--no-toggle` to avoid any tunnel changes.  
- **Whitelisting**: You must already be able to run `doublezero connect ibrl` successfully (i.e., your key/user is whitelisted and configured).

---

## Troubleshooting

- **“ERROR: Missing required tools”** → Install the listed tools and ensure they are in `$PATH`.  
- **Ping shows `icmp blocked` / `timeout`** → The peer or network likely blocks ICMP; this is expected for some hosts.  
- **DZ never reaches the target state** → The script polls `doublezero status` until timeout; if it can’t confirm `up`/`disconnected`, it will warn and skip that leg. Check your DZ connectivity/config.


  
