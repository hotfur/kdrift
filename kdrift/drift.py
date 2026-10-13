#!/usr/bin/env python3
"""
kdrift State Reconciliation, Anti-Regression & Progressive Storage Engine (v3.1.0)
Manages the zero-trust kernel trial boot state machine, content-addressed generational isolation,
finite FIFO rotation queue pruning, and manual critical kernel pinning.
"""

import time
import sys
import os
import re
import json
import logging
import hashlib
import subprocess
import shutil
import argparse
from typing import Dict, List, Any, Tuple, Optional
from kdrift.kpatch import KPatchEngine, KPatchSession
from kdrift.toolchain import ToolchainEngine, ARCH_SPEC_MATRIX, normalize_host_arch

STATE_FILE = "/var/lib/kdrift/state.json"
STATE_DIR = "/var/lib/kdrift"
LOG_FILE = "/var/log/kdrift_reconcile.log"
WATCHDOG_LEASE_SECONDS = 300
DEFAULT_RETENTION_LIMIT = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("kdrift")


def ensure_root():
    if os.geteuid() != 0:
        logger.error("kdrift commands require root privileges.")
        sys.exit(1)


def compute_config_hash(config_path: str) -> str:
    hasher = hashlib.sha256()
    with open(config_path, "rb") as f:
        hasher.update(f.read())
    return hasher.hexdigest()[:8]


def detect_init_system() -> str:
    if os.path.exists("/run/systemd/system"):
        return "systemd"
    elif os.path.exists("/sbin/openrc") or os.path.exists("/etc/init.d"):
        return "openrc"
    return "unknown"


def resolve_reboot_command() -> list:
    init_sys = detect_init_system()
    if init_sys == "systemd":
        return ["systemctl", "reboot"]
    elif init_sys == "openrc":
        if shutil.which("reboot"):
            return ["reboot"]
        return ["reboot"]
    return ["reboot"]


def resolve_grub_tools() -> Tuple[str, str, str, str]:
    reboot_cmd = shutil.which("grub2-reboot") or shutil.which("grub-reboot")
    set_default_cmd = shutil.which("grub2-set-default") or shutil.which("grub-set-default")
    mkconfig_cmd = shutil.which("grub2-mkconfig") or shutil.which("grub-mkconfig")
    editenv_cmd = shutil.which("grub2-editenv") or shutil.which("grub-editenv")
    return reboot_cmd, set_default_cmd, mkconfig_cmd, editenv_cmd

# ==============================================================================
# INDESTRUCTIBLE GRUB ENVIRONMENT CONTROLLER (TIER 1 -> TIER 2 -> TIER 3 NATIVE)
# ==============================================================================

GRUBENV_SIZE = 1024
GRUBENV_HEADER = "# GRUB Environment Block\n"


def locate_grubenv() -> Optional[str]:
    """Locates the active grubenv file or appropriate directory target."""
    for p in ["/boot/grub/grubenv", "/boot/grub2/grubenv"]:
        if os.path.exists(p):
            return p
    for d in ["/boot/grub", "/boot/grub2"]:
        if os.path.isdir(d):
            return os.path.join(d, "grubenv")
    return None


def read_grubenv_native(path: str) -> Dict[str, str]:
    """Parses GRUB's 1024-byte environment block without external binaries."""
    data = {}
    if not os.path.exists(path):
        return data
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) != GRUBENV_SIZE:
            return data
        lines = raw.decode("utf-8", errors="ignore").split("\n")
        if not lines or lines[0] != "# GRUB Environment Block":
            return data
        for line in lines[1:]:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
    except Exception as e:
        logger.warning(f"Native grubenv read error on {path}: {e}")
    return data


def write_grubenv_native(path: str, updates: Dict[str, Optional[str]]) -> bool:
    """
    Direct in-place / atomic manipulation of GRUB's 1024-byte block.
    Operates safely in minimal initramfs or systems lacking grub userland tools.
    """
    env = read_grubenv_native(path)
    for k, v in updates.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v

    payload = GRUBENV_HEADER
    for k in sorted(env.keys()):
        payload += f"{k}={env[k]}\n"

    payload_bytes = payload.encode("utf-8")
    if len(payload_bytes) > GRUBENV_SIZE:
        logger.error(f"Cannot write grubenv: payload size ({len(payload_bytes)}) exceeds {GRUBENV_SIZE} bytes.")
        return False

    pad_len = GRUBENV_SIZE - len(payload_bytes)
    full_block = payload_bytes + (b"#" * pad_len)

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(full_block)
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception as e:
        logger.error(f"Native grubenv write failed on {path}: {e}")
        return False


def set_grub_saved_default(target_entry: str) -> bool:
    """
    Sets GRUB's permanent default (saved_entry) with automatic fallback:
    1. grub-set-default / grub2-set-default
    2. grub-editenv set saved_entry=...
    3. Native 1024-byte block writer (zero dependencies)
    """
    _, set_default_cmd, _, editenv_cmd = resolve_grub_tools()
    grubenv_path = locate_grubenv()

    # Tier 1: Dedicated wrapper binary
    if set_default_cmd:
        res = subprocess.run([set_default_cmd, target_entry], capture_output=True, text=True)
        if res.returncode == 0:
            logger.info(f"[GRUB SET-DEFAULT] Anchored via {os.path.basename(set_default_cmd)}: '{target_entry}'")
            return True

    # Tier 2: grub-editenv tool
    if editenv_cmd and grubenv_path:
        res = subprocess.run([editenv_cmd, grubenv_path, "set", f"saved_entry={target_entry}"], capture_output=True, text=True)
        if res.returncode == 0:
            logger.info(f"[GRUB SET-DEFAULT] Anchored via {os.path.basename(editenv_cmd)}: '{target_entry}'")
            return True

    # Tier 3: Pure Python native block writer (Initramfs / Rescue Mode)
    if grubenv_path:
        logger.info(f"[GRUB SET-DEFAULT] Using native block writer for: '{target_entry}'")
        return write_grubenv_native(grubenv_path, {"saved_entry": target_entry})

    logger.error("Failed to anchor GRUB saved_entry: No suitable tools or grubenv location found.")
    return False


def set_grub_one_shot(target_entry: str) -> bool:
    """
    Sets GRUB's one-shot trial (next_entry) with automatic fallback:
    1. grub-reboot / grub2-reboot
    2. grub-editenv set next_entry=...
    3. Native 1024-byte block writer
    """
    reboot_cmd, _, _, editenv_cmd = resolve_grub_tools()
    grubenv_path = locate_grubenv()

    # Tier 1: Dedicated wrapper binary
    if reboot_cmd:
        res = subprocess.run([reboot_cmd, target_entry], capture_output=True, text=True)
        if res.returncode == 0:
            logger.info(f"[GRUB ONE-SHOT] Armed via {os.path.basename(reboot_cmd)}: '{target_entry}'")
            return True

    # Tier 2: grub-editenv tool
    if editenv_cmd and grubenv_path:
        res = subprocess.run([editenv_cmd, grubenv_path, "set", f"next_entry={target_entry}"], capture_output=True, text=True)
        if res.returncode == 0:
            logger.info(f"[GRUB ONE-SHOT] Armed via {os.path.basename(editenv_cmd)}: '{target_entry}'")
            return True

    # Tier 3: Pure Python native block writer
    if grubenv_path:
        logger.info(f"[GRUB ONE-SHOT] Using native block writer for: '{target_entry}'")
        return write_grubenv_native(grubenv_path, {"next_entry": target_entry})

    logger.error("Failed to arm GRUB next_entry: No suitable tools or grubenv location found.")
    return False


def clear_grub_one_shot():
    """Unsets next_entry using grub-editenv or native block writer."""
    _, _, _, editenv_cmd = resolve_grub_tools()
    grubenv_path = locate_grubenv()

    if editenv_cmd and grubenv_path and os.path.exists(grubenv_path):
        subprocess.run([editenv_cmd, grubenv_path, "unset", "next_entry"], check=False)
    elif grubenv_path and os.path.exists(grubenv_path):
        write_grubenv_native(grubenv_path, {"next_entry": None})

def resolve_grub_entry(target: str) -> Optional[str]:
    """
    Parses /boot/grub/grub.cfg (or grub2) to resolve the exact GRUB menuentry ID
    (or title) mapped to the target kernel generation or binary name.
    Supports top-level entries as well as nested submenus ('submenu_id>entry_id').
    """
    cfg_paths = ["/boot/grub/grub.cfg", "/boot/grub2/grub.cfg"]
    cfg_path = next((p for p in cfg_paths if os.path.exists(p)), None)
    if not cfg_path:
        logger.warning("Could not locate grub.cfg to resolve menu entry.")
        return None

    target_clean = os.path.basename(target).strip()
    tokens = [target_clean]
    if target_clean.startswith("vmlinuz-"):
        tokens.append(target_clean[len("vmlinuz-"):])
    for prefix in ["candidate-", "stable-"]:
        if target_clean.startswith(prefix):
            tokens.append(target_clean[len(prefix):])
        elif target_clean.startswith(f"vmlinuz-{prefix}"):
            tokens.append(target_clean[len(f"vmlinuz-{prefix}"):])

    tokens = list(dict.fromkeys([t for t in tokens if t]))

    try:
        with open(cfg_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception as e:
        logger.warning(f"Failed to read {cfg_path}: {e}")
        return None

    submenu_stack: List[Dict[str, Any]] = []
    current_entry: Optional[Dict[str, Any]] = None
    brace_depth = 0

    re_menuentry = re.compile(r"^\s*menuentry\s+(['\"])(.*?)\1")
    re_submenu = re.compile(r"^\s*submenu\s+(['\"])(.*?)\1")
    re_id = re.compile(r"(?:\$menuentry_id_option|--id)\s+(['\"])(.*?)\1")

    for line in lines:
        # 1. Submenu declaration
        sub_match = re_submenu.match(line)
        if sub_match:
            sub_title = sub_match.group(2)
            id_m = re_id.search(line)
            sub_id = id_m.group(2) if id_m else sub_title

            brace_depth += line.count("{") - line.count("}")
            submenu_stack.append({
                "id": sub_id,
                "title": sub_title,
                "depth": brace_depth
            })
            continue

        # 2. Menuentry declaration
        entry_match = re_menuentry.match(line)
        if entry_match and current_entry is None:
            entry_title = entry_match.group(2)
            id_m = re_id.search(line)
            entry_id = id_m.group(2) if id_m else entry_title
            parents = [(s["id"] if s["id"] else s["title"]) for s in submenu_stack]

            brace_depth += line.count("{") - line.count("}")
            current_entry = {
                "id": entry_id,
                "title": entry_title,
                "parents": parents,
                "body": [line]
            }
            continue

        # 3. Inside a menuentry block
        if current_entry is not None:
            current_entry["body"].append(line)
            brace_depth += line.count("{") - line.count("}")

            parent_depth = submenu_stack[-1]["depth"] if submenu_stack else 0
            if brace_depth <= parent_depth:
                matched = False
                # Check binary call line (linux, linux16, linuxefi)
                for b_line in current_entry["body"]:
                    b_strip = b_line.strip()
                    if any(b_strip.startswith(k) for k in ("linux", "linux16", "linuxefi")):
                        if any(tok in b_strip for tok in tokens):
                            matched = True
                            break

                # Fallback to ID or title check
                if not matched:
                    if any(tok in current_entry["id"] or tok in current_entry["title"] for tok in tokens):
                        matched = True

                if matched:
                    entry_token = current_entry["id"] or current_entry["title"]
                    if current_entry["parents"]:
                        return ">".join(current_entry["parents"] + [entry_token])
                    return entry_token

                current_entry = None
            continue

        # 4. Outside menuentry: track submenu closures
        brace_depth += line.count("{") - line.count("}")
        while submenu_stack and brace_depth < submenu_stack[-1]["depth"]:
            submenu_stack.pop()

    return None


def clean_grubenv():
    clear_grub_one_shot()

def regenerate_grub():
    _, _, mkconfig, _ = resolve_grub_tools()
    if mkconfig:
        for cfg in ["/boot/grub/grub.cfg", "/boot/grub2/grub.cfg"]:
            if os.path.exists(cfg):
                logger.info(f"Re-synchronizing bootloader configuration: {cfg}...")
                subprocess.run([mkconfig, "-o", cfg], check=False)


def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
                state.setdefault("retention_limit", DEFAULT_RETENTION_LIMIT)
                state.setdefault("rotation_queue", [])
                state.setdefault("persisted_kernels", [])
                return state
        except Exception as e:
            logger.error(f"Failed to read state file: {e}")
    return {
        "status": "STABLE",
        "active_stable": None,
        "candidate": None,
        "fallback": None,
        "boot_timestamp": 0,
        "retention_limit": DEFAULT_RETENTION_LIMIT,
        "rotation_queue": [],
        "persisted_kernels": []
    }


def save_state(state: Dict[str, Any]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ==============================================================================
# PROGRESSIVE STORAGE ENGINE
# ==============================================================================

def normalize_generation_tag(input_str: Optional[str], state: Dict[str, Any]) -> str:
    running_krel = os.uname().release

    if not input_str:
        active = state.get("active_stable")
        if active and active.startswith(running_krel):
            return active

        all_cataloged = [state.get("active_stable")] + \
                        [k["id"] for k in state.get("rotation_queue", [])] + \
                        [k["id"] for k in state.get("persisted_kernels", [])]
        for gen in filter(None, all_cataloged):
            if gen.startswith(running_krel):
                return gen

        logger.error(
            f"Active kernel ({running_krel}) is not a recognized kdrift generation. "
            "Distro kernels cannot be persisted."
        )
        sys.exit(1)

    gen_id = input_str.strip()
    for prefix in ["vmlinuz-stable-", "vmlinuz-candidate-", "vmlinuz-"]:
        if gen_id.startswith(prefix):
            gen_id = gen_id[len(prefix):]

    all_registered = [k["id"] for k in state.get("rotation_queue", [])] + \
                     [k["id"] for k in state.get("persisted_kernels", [])]
    if state.get("active_stable"):
        all_registered.append(state["active_stable"])

    if gen_id in all_registered or os.path.exists(f"/boot/vmlinuz-stable-{gen_id}"):
        return gen_id

    logger.error(f"Kernel generation '{input_str}' not found in kdrift registry or /boot.")
    sys.exit(1)


def purge_generation_artifacts(gen_id: str):
    logger.warning(f"[STORAGE EVICTION] Purging artifacts for generation: {gen_id}")
    boot_files = [
        f"/boot/vmlinuz-stable-{gen_id}",
        f"/boot/initramfs-stable-{gen_id}.img",
        f"/boot/initramfs-stable-{gen_id}",
        f"/boot/System.map-stable-{gen_id}",
        f"/boot/config-stable-{gen_id}"
    ]
    for bf in boot_files:
        if os.path.exists(bf):
            try:
                os.remove(bf)
                logger.info(f"  Unlinked: {bf}")
            except OSError as e:
                logger.error(f"  Failed unlinking {bf}: {e}")

    for sym in ["/boot/vmlinuz-custom", "/boot/initramfs-custom.img"]:
        if os.path.islink(sym):
            try:
                target = os.readlink(sym)
                if gen_id in target:
                    os.remove(sym)
                    logger.warning(f"  Removed dangling symlink: {sym}")
            except OSError:
                pass


def garbage_collect_modules(state: Dict[str, Any]):
    active_krels = set()
    if state.get("active_stable"):
        active_krels.add(state["active_stable"].split("-")[0])
    if state.get("candidate"):
        active_krels.add(state["candidate"].split("-")[0])

    for entry in state.get("rotation_queue", []):
        active_krels.add(entry["krel"])
    for entry in state.get("persisted_kernels", []):
        active_krels.add(entry["krel"])

    modules_root = "/lib/modules"
    if not os.path.exists(modules_root):
        return

    for mod_dir in os.listdir(modules_root):
        mod_path = os.path.join(modules_root, mod_dir)
        if mod_dir == os.uname().release:
            continue
        if mod_dir not in active_krels and os.path.isdir(mod_path):
            matching_boot = [f for f in os.listdir("/boot") if mod_dir in f]
            if not matching_boot:
                logger.warning(f"[MODULE GC] Pruning unreferenced module tree: {mod_path}")
                try:
                    shutil.rmtree(mod_path)
                except Exception as e:
                    logger.error(f"Failed to remove {mod_path}: {e}")


def apply_retention_policy(state: Dict[str, Any]) -> bool:
    limit = state.get("retention_limit", DEFAULT_RETENTION_LIMIT)
    queue: List[Dict[str, Any]] = state.get("rotation_queue", [])

    queue.sort(key=lambda x: x.get("timestamp", 0), reverse=True)

    if len(queue) <= limit:
        state["rotation_queue"] = queue
        return False

    evicted = queue[limit:]
    state["rotation_queue"] = queue[:limit]

    logger.info(f"[RETENTION GC] Rotation capacity ({limit}) exceeded. Pruning {len(evicted)} generation(s)...")
    for item in evicted:
        gen_id = item["id"]
        if gen_id == state.get("active_stable"):
            logger.warning(f"Preserving active generation {gen_id} from deletion.")
            state["rotation_queue"].append(item)
            continue
        purge_generation_artifacts(gen_id)

    garbage_collect_modules(state)
    return True


def handle_persist(args):
    ensure_root()
    state = load_state()
    gen_id = normalize_generation_tag(args.kernel_name, state)

    persisted: List[Dict[str, Any]] = state.get("persisted_kernels", [])
    queue: List[Dict[str, Any]] = state.get("rotation_queue", [])

    if any(k["id"] == gen_id for k in persisted):
        logger.info(f"Generation '{gen_id}' is already marked persistent.")
        return

    krel = gen_id.split("-")[0]
    matched_entry = None
    for idx, entry in enumerate(queue):
        if entry["id"] == gen_id:
            matched_entry = queue.pop(idx)
            break

    orig_timestamp = matched_entry["timestamp"] if matched_entry else int(time.time())
    record = {
        "id": gen_id,
        "krel": krel,
        "timestamp": orig_timestamp,
        "pinned_at": int(time.time())
    }
    persisted.append(record)

    state["persisted_kernels"] = persisted
    state["rotation_queue"] = queue
    save_state(state)

    logger.info(
        f"[PERSISTENCE GRANTED] Generation '{gen_id}' isolated into persistent registry.\n"
        f"  Total Persistent Kernels: {len(persisted)}\n"
        f"  Queue Capacity Status: {len(queue)}/{state.get('retention_limit')}"
    )


def handle_unpersist(args):
    ensure_root()
    state = load_state()
    gen_id = normalize_generation_tag(args.kernel_name, state)

    persisted: List[Dict[str, Any]] = state.get("persisted_kernels", [])
    queue: List[Dict[str, Any]] = state.get("rotation_queue", [])

    unpinned_entry = None
    for idx, entry in enumerate(persisted):
        if entry["id"] == gen_id:
            unpinned_entry = persisted.pop(idx)
            break

    if not unpinned_entry:
        logger.error(f"Generation '{gen_id}' is not in the persistent registry.")
        sys.exit(1)

    queue.append({
        "id": unpinned_entry["id"],
        "krel": unpinned_entry["krel"],
        "timestamp": unpinned_entry.get("timestamp", int(time.time()))
    })

    state["persisted_kernels"] = persisted
    state["rotation_queue"] = queue
    logger.info(f"[PERSISTENCE REVOKED] Generation '{gen_id}' returned to temporal rotation queue.")

    mutated = apply_retention_policy(state)
    save_state(state)
    if mutated:
        regenerate_grub()


def handle_set_retention(args):
    ensure_root()
    if args.count < 1:
        logger.error("Retention limit must be >= 1.")
        sys.exit(1)

    state = load_state()
    state["retention_limit"] = args.count
    logger.info(f"Setting kernel retention limit: {args.count}")

    mutated = apply_retention_policy(state)
    save_state(state)
    if mutated:
        regenerate_grub()


def handle_list(args):
    state = load_state()
    active = state.get("active_stable", "None")
    limit = state.get("retention_limit", DEFAULT_RETENTION_LIMIT)
    queue = state.get("rotation_queue", [])
    persisted = state.get("persisted_kernels", [])
    running = os.uname().release

    print("\n=======================================================")
    print("kdrift Storage Topology & Invariant Kernel Allocations")
    print("=======================================================")
    print(f"Running Kernel (OS) : {running}")
    print(f"Active Stable Target : {active}")
    print(f"Queue Capacity (N)   : {limit}")
    print("-------------------------------------------------------")

    print(f"ROTATION QUEUE ({len(queue)}/{limit}):")
    if not queue:
        print("  (Empty)")
    for idx, k in enumerate(queue):
        mark = " [*]" if k["id"] == active else ""
        t_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(k.get("timestamp", 0)))
        print(f"  [{idx+1}] {k['id']:<32} | Compiled: {t_str}{mark}")

    print("\nPERSISTENT INVARIANT LIST (Critical Locked):")
    if not persisted:
        print("  (No kernels pinned)")
    for idx, k in enumerate(persisted):
        mark = " [*]" if k["id"] == active else ""
        t_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(k.get("pinned_at", 0)))
        print(f"  [P{idx+1}] {k['id']:<31} | Pinned: {t_str}{mark}")

    total_managed = len(queue) + len(persisted)
    print("-------------------------------------------------------")
    print(f"Total Custom Kernels Stored: {total_managed} (Queue: {len(queue)}, Pinned: {len(persisted)})\n")


def handle_prune(args):
    ensure_root()
    state = load_state()
    mutated = apply_retention_policy(state)
    save_state(state)
    if mutated:
        regenerate_grub()
    logger.info("Storage reconciliation complete.")


# ==============================================================================
# PROMOTION, WATCHDOG & ROLLBACK ENGINE
# ==============================================================================

def promote_candidate(candidate_gen: str):
    logger.info(f"Promoting candidate {candidate_gen} to STABLE")
    state = load_state()

    cand_vmlinuz = f"/boot/vmlinuz-candidate-{candidate_gen}"
    cand_initrd = f"/boot/initramfs-candidate-{candidate_gen}.img"

    stable_vmlinuz = f"/boot/vmlinuz-stable-{candidate_gen}"
    stable_initrd = f"/boot/initramfs-stable-{candidate_gen}.img"

    if os.path.exists(cand_vmlinuz):
        shutil.copy2(cand_vmlinuz, stable_vmlinuz)
        os.remove(cand_vmlinuz)

    if os.path.exists(cand_initrd):
        shutil.copy2(cand_initrd, stable_initrd)
        os.remove(cand_initrd)

    subprocess.run(["ln", "-sf", os.path.basename(stable_vmlinuz), "/boot/vmlinuz-custom"], cwd="/boot", check=True)
    if os.path.exists(stable_initrd):
        subprocess.run(["ln", "-sf", os.path.basename(stable_initrd), "/boot/initramfs-custom.img"], cwd="/boot", check=True)

    old_active = state.get("active_stable")
    state["active_stable"] = candidate_gen
    state["status"] = "STABLE"
    state["candidate"] = None
    state["fallback"] = old_active or os.uname().release
    state["boot_timestamp"] = 0

    queue: List[Dict[str, Any]] = state.get("rotation_queue", [])
    queue = [k for k in queue if k["id"] != candidate_gen]
    queue.insert(0, {
        "id": candidate_gen,
        "krel": candidate_gen.split("-")[0],
        "timestamp": int(time.time())
    })
    state["rotation_queue"] = queue

    apply_retention_policy(state)
    save_state(state)

    clean_grubenv()
    regenerate_grub()

    # Anchor the newly promoted stable kernel as the permanent default
    target_entry = (
        resolve_grub_entry(f"vmlinuz-stable-{candidate_gen}")
        or resolve_grub_entry(f"stable-{candidate_gen}")
        or resolve_grub_entry("vmlinuz-custom")
        or f"vmlinuz-stable-{candidate_gen}"
    )
    logger.info(f"[PROMOTION] Anchoring GRUB default to: {target_entry}")
    set_grub_saved_default(target_entry)

    logger.info(f"Candidate {candidate_gen} committed as active stable.")


def execute_rollback(reason: str):
    state = load_state()
    candidate = state.get("candidate")
    active_stable = state.get("active_stable")

    logger.warning(f"[ROLLBACK TRIGGERED] Reason: {reason}")

    if candidate:
        logger.info(f"Quarantining candidate generation: {candidate}...")
        for art in [
            f"/boot/vmlinuz-candidate-{candidate}",
            f"/boot/initramfs-candidate-{candidate}.img",
            f"/boot/initramfs-candidate-{candidate}"
        ]:
            if os.path.exists(art):
                os.remove(art)
                logger.info(f"Purged: {art}")

    recovery_kernel = None
    if active_stable and os.path.exists(f"/boot/vmlinuz-stable-{active_stable}"):
        recovery_kernel = f"vmlinuz-stable-{active_stable}"
        subprocess.run(["ln", "-sf", recovery_kernel, "/boot/vmlinuz-custom"], cwd="/boot", check=False)
        if os.path.exists(f"/boot/initramfs-stable-{active_stable}.img"):
            subprocess.run(["ln", "-sf", f"initramfs-stable-{active_stable}.img", "/boot/initramfs-custom.img"], cwd="/boot", check=False)
    else:
        # Fallback to the live distro kernel release
        recovery_kernel = f"vmlinuz-{os.uname().release}"
    clean_grubenv()
    regenerate_grub()

    if recovery_kernel:
        target_entry = resolve_grub_entry(recovery_kernel) or resolve_grub_entry(os.uname().release) or recovery_kernel
        logger.info(f"[ANTI-REGRESSION] Re-anchoring GRUB default to: {target_entry}")
        set_grub_saved_default(target_entry)

    save_state({
        "status": "ROLLED_BACK",
        "active_stable": active_stable,
        "candidate": None,
        "fallback": recovery_kernel,
        "last_failure_reason": reason,
        "retention_limit": state.get("retention_limit", DEFAULT_RETENTION_LIMIT),
        "rotation_queue": state.get("rotation_queue", []),
        "persisted_kernels": state.get("persisted_kernels", [])
    })

def generate_initramfs(kernel_release: str, out_img: str) -> bool:
    if shutil.which("mkinitfs"):
        return subprocess.run(["mkinitfs", "-k", "-o", out_img, kernel_release]).returncode == 0
    elif shutil.which("dracut"):
        return subprocess.run(["dracut", "--kver", kernel_release, "--force", out_img]).returncode == 0
    elif shutil.which("mkinitcpio"):
        return subprocess.run(["mkinitcpio", "-k", kernel_release, "-g", out_img]).returncode == 0
    elif shutil.which("update-initramfs"):
        return subprocess.run(["update-initramfs", "-c", "-k", kernel_release]).returncode == 0
    return True


def audit_runtime_health() -> Tuple[bool, str]:
    rw_test_path = "/var/lib/kdrift/.rw_assertion"
    try:
        with open(rw_test_path, "w") as f:
            f.write("OK")
        os.remove(rw_test_path)
    except Exception as e:
        return False, f"Root filesystem read-only or I/O failure: {e}"

    if detect_init_system() == "systemd":
        res = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True)
        status = res.stdout.strip()
        if status not in ("running", "degraded"):
            return False, f"Init system status unviable: {status}"

    dmesg_res = subprocess.run(["dmesg", "-l", "emerg,alert"], capture_output=True, text=True)
    if dmesg_res.returncode == 0 and dmesg_res.stdout.strip():
        first_line = dmesg_res.stdout.splitlines()[0]
        return False, f"Kernel emergency ring buffer signal: {first_line}"

    return True, "All functional runtime constraints satisfied."


def _dispatch_detached_posix_timer(kdrift_bin: str, lease_seconds: int):
    cmd = f"(sleep {lease_seconds} && {kdrift_bin} lease-promote) >/dev/null 2>&1 &"
    subprocess.Popen(
        cmd,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )


def schedule_async_wakeup(lease_seconds: int):
    init_sys = detect_init_system()
    kdrift_bin = shutil.which("kdrift") or "/usr/local/bin/kdrift"

    if init_sys == "systemd":
        logger.info(f"[ASYNC LEASE] Spawning systemd transient timer ({lease_seconds}s)...")
        cmd = [
            "systemd-run",
            f"--on-active={lease_seconds}s",
            "--description=kdrift Kernel Trial Lease Maturity Gate",
            kdrift_bin, "lease-promote"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            logger.warning("Failed to arm systemd transient timer. Falling back to subshell.")
            _dispatch_detached_posix_timer(kdrift_bin, lease_seconds)
    else:
        logger.info(f"[ASYNC LEASE] Spawning detached POSIX watchdog ({lease_seconds}s)...")
        _dispatch_detached_posix_timer(kdrift_bin, lease_seconds)


def check_dkms_status(kernel_release: str) -> bool:
    dkms_bin = shutil.which("dkms")
    if not dkms_bin:
        return True

    logger.info(f"Executing DKMS assertion gate for release {kernel_release}...")
    try:
        res = subprocess.run([dkms_bin, "status"], capture_output=True, text=True)
        if res.returncode != 0:
            return subprocess.run([dkms_bin, "autoinstall", "-k", kernel_release]).returncode == 0

        seen_modules = set()
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            first_part = line.split(":")[0].strip()
            parts = [p.strip() for p in first_part.split(",")]
            mod_name, mod_ver = "", ""
            if "/" in parts[0]:
                mod_name, mod_ver = parts[0].split("/", 1)
            elif len(parts) >= 2:
                mod_name, mod_ver = parts[0], parts[1]

            if not mod_name or not mod_ver:
                continue
            mod_key = (mod_name, mod_ver)
            if mod_key in seen_modules:
                continue
            seen_modules.add(mod_key)

            build_cmd = [dkms_bin, "build", "-m", mod_name, "-v", mod_ver, "-k", kernel_release]
            if subprocess.run(build_cmd, capture_output=True, text=True).returncode != 0:
                return False
            install_cmd = [dkms_bin, "install", "-m", mod_name, "-v", mod_ver, "-k", kernel_release]
            subprocess.run(install_cmd, capture_output=True, text=True)
        return True
    except Exception:
        return False

def handle_reconcile(args):
    """Compiles with generational isolation, armed trial boot, and atomic patch rollback."""
    ensure_root()
    kernel_src = args.kernel_dir
    profile = args.profile

    kpatch = KPatchEngine()

    # Wrap entire configuration, build, and staging lifecycle in RAII guard
    with KPatchSession(kpatch, kernel_src) as kpatch_session:
        # Step 1: Toolchain and ISA binding
        engine = ToolchainEngine()
        target_arch = engine.config.get("target_arch", normalize_host_arch())
        kernel_arch = ARCH_SPEC_MATRIX.get(target_arch, {}).get("kernel_arch", "x86")
        is_cross = (target_arch != normalize_host_arch())

        base_config = getattr(args, "base_config", None)
        if not base_config:
            default_boot_config = f"/boot/config-{os.uname().release}"
            if os.path.exists(default_boot_config):
                base_config = default_boot_config
            elif os.path.exists(os.path.join(kernel_src, ".config")):
                base_config = os.path.join(kernel_src, ".config")
            else:
                logger.error("Cannot locate baseline .config")
                sys.exit(1)

        logger.info(f"Reconciling configuration (Toolchain={engine.config.get('compiler_flavor')}, ARCH={kernel_arch})...")

        # Step 2: Invariant assertion (pure compiler)
        cmd_merge = [
            "kdrift-assert", "merge",
            "--kernel-dir", kernel_src,
            "--base-config", base_config,
            "--profile", profile,
            "--arch", kernel_arch
        ]
        if subprocess.run(cmd_merge).returncode != 0:
            logger.error("kdrift assertion failed. Aborting reconciliation.")
            sys.exit(1)  # Triggers kpatch_session.rollback()

        # Step 3: Compilation Pipeline (with_ccache=True)
        build_env, build_make_args, binary_relpath = engine.resolve_effective_toolchain(with_ccache=True)

        cores = str(os.cpu_count() or 4)
        arch_target = ARCH_SPEC_MATRIX.get(target_arch, {}).get("make_target", "bzImage")

        # Generate unique config hash early
        cfg_hash = compute_config_hash(os.path.join(kernel_src, ".config"))
        kdrift_tag = f"-kdrift-{cfg_hash}"
        
        # Inject LOCALVERSION into build arguments
        build_make_args_isolated = build_make_args + [f"LOCALVERSION={kdrift_tag}"]

        logger.info(f"Compiling kernel target [{arch_target}] with isolation tag: {kdrift_tag}...")
        comp_cmd = ["make", "-j" + cores] + build_make_args_isolated + [arch_target]
        if subprocess.run(comp_cmd, cwd=kernel_src, env=build_env).returncode != 0:
            logger.error("Kernel compilation failed.")
            sys.exit(1)

        krel_cmd = ["make", "-s"] + build_make_args_isolated + ["kernelrelease"]
        krel_res = subprocess.run(krel_cmd, cwd=kernel_src, env=build_env, capture_output=True, text=True)
        if krel_res.returncode != 0:
            logger.error("Failed to query kernelrelease.")
            sys.exit(1)
        krel = krel_res.stdout.strip()

        candidate_gen = krel  # Now naturally includes the unique hash
        logger.info(f"[GENERATION ALLOCATED] Unique Candidate ID: {candidate_gen}")

        with open(os.path.join(kernel_src, ".config"), "r") as f:
            config_text = f.read()

        if "CONFIG_MODULES=y" in config_text:
            logger.info(f"Compiling and installing modules into /lib/modules/{krel}...")
            if subprocess.run(["make", "-j" + cores] + build_make_args_isolated + ["modules"], cwd=kernel_src, env=build_env).returncode != 0:
                logger.error("Module compilation failed.")
                sys.exit(1)

            if subprocess.run(["make"] + build_make_args_isolated + ["modules_install"], cwd=kernel_src, env=build_env).returncode != 0:
                logger.error("Module installation failed.")
                sys.exit(1)


        if is_cross:
            logger.warning("[CROSS] Skipping host DKMS verification.")
        else:
            if not check_dkms_status(krel):
                logger.error("ABORTING STAGING: DKMS module compilation failed.")
                sys.exit(1)  # Triggers kpatch_session.rollback()

        src_bin = os.path.join(kernel_src, binary_relpath)
        if not os.path.exists(src_bin):
            logger.error(f"Target binary artifact missing at: {src_bin}")
            sys.exit(1)  # Triggers kpatch_session.rollback()

        cand_vmlinuz = f"/boot/vmlinuz-candidate-{candidate_gen}"
        cand_initrd = f"/boot/initramfs-candidate-{candidate_gen}.img"

        logger.info(f"Staging to isolated candidate: {cand_vmlinuz}")
        shutil.copy2(src_bin, cand_vmlinuz)

        if "CONFIG_BLK_DEV_INITRD=y" in config_text:
            generate_initramfs(krel, cand_initrd)

        regenerate_grub()

        state = load_state()
        state["status"] = "ARMED_TRIAL"
        state["candidate"] = candidate_gen
        state["boot_timestamp"] = 0
        save_state(state)

        # 1. ANCHOR THE FALLBACK KERNEL (PREVENTS CANDIDATE FROM BECOMING DEFAULT)
        active_stable = state.get("active_stable")
        if active_stable and os.path.exists(f"/boot/vmlinuz-stable-{active_stable}"):
            fallback_target = f"vmlinuz-stable-{active_stable}"
        else:
            fallback_target = f"vmlinuz-{os.uname().release}"

        fallback_entry = resolve_grub_entry(fallback_target) or resolve_grub_entry(os.uname().release) or fallback_target
        logger.info(f"[ANTI-REGRESSION] Anchoring permanent default to: {fallback_entry}")
        set_grub_saved_default(fallback_entry)

        # 2. RESERVE THE ONE-SHOT CANDIDATE TRIAL BOOT
        candidate_file = f"vmlinuz-candidate-{candidate_gen}"
        candidate_entry = resolve_grub_entry(candidate_file) or resolve_grub_entry(f"candidate-{candidate_gen}") or candidate_file
        logger.info(f"Reserving one-shot trial boot for: {candidate_entry}...")
        set_grub_one_shot(candidate_entry)

        # Step 4: Staging Complete & Invariants Satisfied -> Disarm rollback
        kpatch_session.mark_successful()

    logger.info("Reconciliation pipeline completed successfully. Candidate armed.")


def handle_boot_check(args):
    ensure_root()
    state = load_state()
    current_krel = os.uname().release
    status = state.get("status")

    if status == "ARMED_TRIAL":
        candidate = state.get("candidate")
        if candidate and candidate.startswith(current_krel):
            logger.info(f"[TRIAL VERIFIED] System entered candidate {candidate}.")
            state["status"] = "TRIAL_PROBING"
            state["boot_timestamp"] = int(time.time())
            save_state(state)
            schedule_async_wakeup(WATCHDOG_LEASE_SECONDS)
            sys.exit(0)
        else:
            logger.error(f"[INTEGRITY FAULT] Booted '{current_krel}' instead of candidate '{candidate}'.")
            execute_rollback(f"Boot failure into candidate {candidate}.")
            sys.exit(0)

    elif status == "TRIAL_PROBING":
        candidate = state.get("candidate")
        if candidate and not candidate.startswith(current_krel):
            logger.warning("[TRIAL INTERRUPTED] Unexpected reboot during trial period.")
            execute_rollback("Unexpected reboot during trial period.")
        else:
            schedule_async_wakeup(WATCHDOG_LEASE_SECONDS)


def handle_lease_promote(args):
    ensure_root()
    state = load_state()
    if state.get("status") != "TRIAL_PROBING":
        return

    candidate = state.get("candidate")
    healthy, reason = audit_runtime_health()
    if not healthy:
        logger.error(f"[PROMOTION DENIED] Health check failed: {reason}")
        state["status"] = "DEGRADED_TRIAL"
        state["degrade_reason"] = reason
        save_state(state)
        return

    logger.info(f"[HEALTH ASSERTION PASSED] Committing generation {candidate} to permanent stable.")
    promote_candidate(candidate)


def handle_shutdown_hook(args):
    state = load_state()
    status = state.get("status")
    current_krel = os.uname().release
    candidate = state.get("candidate")

    if status == "ARMED_TRIAL":
        return

    if status == "TRIAL_PROBING":
        if candidate and candidate.startswith(current_krel):
            healthy, reason = audit_runtime_health()
            if healthy:
                logger.info(f"[SHUTDOWN] Clean shutdown on candidate {candidate}. Promoting to STABLE.")
                promote_candidate(candidate)
            else:
                logger.warning(f"[SHUTDOWN] Candidate degraded ({reason}). Refusing promotion.")


def handle_commit(args):
    ensure_root()
    state = load_state()
    cand = state.get("candidate")
    if not cand:
        logger.error("No active candidate identified in state.")
        sys.exit(1)
    promote_candidate(cand)


def handle_rollback(args):
    ensure_root()
    execute_rollback("Manual explicit rollback invoked by administrator.")
    reboot_cmd = resolve_reboot_command()
    logger.info("Rollback complete. Rebooting into fallback...")
    subprocess.run(reboot_cmd, check=False)


# ==============================================================================
# GIT RECONCILE & HOOK LOGIC
# ==============================================================================

def get_git_head_commit(kernel_dir: str) -> str:
    if not os.path.exists(os.path.join(kernel_dir, ".git")):
        return ""
    try:
        res = subprocess.run(["git", "-C", kernel_dir, "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception:
        return ""


def calculate_git_delta(kernel_dir: str, last_commit: str) -> int:
    if not last_commit or not os.path.exists(os.path.join(kernel_dir, ".git")):
        return 1
    try:
        res = subprocess.run(["git", "-C", kernel_dir, "rev-list", "--count", f"{last_commit}..HEAD"], capture_output=True, text=True, check=True)
        return int(res.stdout.strip())
    except Exception:
        return 1


def handle_git_reconcile(args):
    ensure_root()
    kernel_src = args.kernel_dir
    # Before calculating git delta or pulling, revert active kpatches so working tree is clean
    kpatch = KPatchEngine()
    kpatch.revert_active_transaction(kernel_src)

    min_commits = args.min_commits
    min_age = args.min_age_seconds

    current_head = get_git_head_commit(kernel_src)
    if not current_head:
        logger.error(f"Directory '{kernel_src}' is not a valid Git repository.")
        sys.exit(1)

    state = load_state()
    last_built_commit = state.get("last_built_commit", "")
    last_build_time = state.get("last_build_timestamp", 0)

    if current_head == last_built_commit and not args.force:
        logger.info(f"Git HEAD ({current_head[:8]}) matches last built commit. No changes.")
        sys.exit(0)

    delta_commits = calculate_git_delta(kernel_src, last_built_commit)
    elapsed_time = int(time.time()) - last_build_time

    logger.info(f"Git Delta Detected: {delta_commits} new commits ({last_built_commit[:8]} -> {current_head[:8]}).")

    if not args.force:
        if delta_commits < min_commits:
            logger.info(f"Commit threshold not met ({delta_commits} < {min_commits}). Throttling.")
            sys.exit(0)
        if elapsed_time < min_age:
            logger.info(f"Temporal throttle active ({elapsed_time}s < {min_age}s). Throttling.")
            sys.exit(0)

    handle_reconcile(args)

    state = load_state()
    state["last_built_commit"] = current_head
    state["last_build_timestamp"] = int(time.time())
    save_state(state)


def handle_install_git_hook(args):
    kernel_src = args.kernel_dir
    git_hooks_dir = os.path.join(kernel_src, ".git", "hooks")
    if not os.path.exists(git_hooks_dir):
        logger.error(f"Git hooks directory does not exist: {git_hooks_dir}")
        sys.exit(1)

    hook_script = f"""#!/bin/sh
echo "[+] kdrift: Git transaction detected. Evaluating reconciliation delta..."
/usr/local/bin/kdrift git-reconcile --kernel-dir "{kernel_src}" --profile "{args.profile}" --min-commits {args.min_commits} --min-age-seconds {args.min_age_seconds}
"""
    for hook_name in ["post-merge", "post-commit"]:
        hook_path = os.path.join(git_hooks_dir, hook_name)
        with open(hook_path, "w") as f:
            f.write(hook_script)
        os.chmod(hook_path, 0o755)
        logger.info(f"Installed native Git hook: {hook_path}")


def main():
    parser = argparse.ArgumentParser(description="kdrift: Zero-Trust State Reconciler & Progressive Storage Engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Reconcile
    p_rec = subparsers.add_parser("reconcile", help="Reconcile source, build, and stage candidate kernel")
    p_rec.add_argument("--kernel-dir", required=True)
    p_rec.add_argument("--profile", required=True)
    p_rec.add_argument("--base-config")
    p_rec.set_defaults(func=handle_reconcile)

    # Git Reconcile
    p_git = subparsers.add_parser("git-reconcile", help="Reconcile based on Git deltas")
    p_git.add_argument("--kernel-dir", required=True)
    p_git.add_argument("--profile", required=True)
    p_git.add_argument("--base-config")
    p_git.add_argument("--min-commits", type=int, default=1)
    p_git.add_argument("--min-age-seconds", type=int, default=0)
    p_git.add_argument("--force", action="store_true")
    p_git.set_defaults(func=handle_git_reconcile)

    # Install Git Hook
    p_ghook = subparsers.add_parser("install-git-hook", help="Install native post-merge hooks")
    p_ghook.add_argument("--kernel-dir", required=True)
    p_ghook.add_argument("--profile", required=True)
    p_ghook.add_argument("--min-commits", type=int, default=1)
    p_ghook.add_argument("--min-age-seconds", type=int, default=0)
    p_ghook.set_defaults(func=handle_install_git_hook)

    # KPatch manual subcommands
    p_patch = subparsers.add_parser("patch-apply", help="Apply custom overlays and patches from /usr/src/kpatch")
    p_patch.add_argument("--kernel-dir", required=True)
    p_patch.set_defaults(func=lambda args: KPatchEngine().apply_transaction(args.kernel_dir))

    p_clean = subparsers.add_parser("patch-clean", help="Revert applied overlays and return tree to clean state")
    p_clean.add_argument("--kernel-dir", required=True)
    p_clean.set_defaults(func=lambda args: KPatchEngine().revert_active_transaction(args.kernel_dir))

    # Storage Commands
    p_persist = subparsers.add_parser("persist", help="Pin a custom kernel to prevent automatic deletion")
    p_persist.add_argument("kernel_name", nargs="?", default="", help="Generation tag or empty for running kernel")
    p_persist.set_defaults(func=handle_persist)

    p_unpersist = subparsers.add_parser("unpersist", help="Unpin a kernel and return it to temporal decay queue")
    p_unpersist.add_argument("kernel_name", nargs="?", default="", help="Generation tag or empty for running kernel")
    p_unpersist.set_defaults(func=handle_unpersist)

    p_ret = subparsers.add_parser("set-retention", help="Set maximum rotation copies (N)")
    p_ret.add_argument("count", type=int, help="Number of historical kernel generations to keep")
    p_ret.set_defaults(func=handle_set_retention)

    p_list = subparsers.add_parser("list", help="Display all active, queued, and persisted kernels")
    p_list.set_defaults(func=handle_list)

    p_prune = subparsers.add_parser("prune", help="Manually enforce retention limits and module GC")
    p_prune.set_defaults(func=handle_prune)

    # Lifecycle Subcommands
    p_boot = subparsers.add_parser("boot-check", help="Early boot trial assertion")
    p_boot.set_defaults(func=handle_boot_check)

    p_lease = subparsers.add_parser("lease-promote", help="Evaluate health assertions post-lease")
    p_lease.set_defaults(func=handle_lease_promote)

    p_shut = subparsers.add_parser("shutdown-hook", help="Guarded promotion hook for shutdown")
    p_shut.set_defaults(func=handle_shutdown_hook)

    p_com = subparsers.add_parser("commit", help="Explicitly promote candidate kernel")
    p_com.set_defaults(func=handle_commit)

    p_roll = subparsers.add_parser("rollback", help="Purge candidate and reboot into fallback")
    p_roll.set_defaults(func=handle_rollback)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
