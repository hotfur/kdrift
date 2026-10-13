# TODO.md: kdrift Production Architecture Blueprint & Milestone Roadmap

This document outlines the technical backlog, engineering specifications, and delivery milestones for transitioning the `kdrift` Proof of Concept into an audited, statically linked, bare-metal-capable Rust systems daemon.

> **MAINTAINER REALITY NOTICE:**  
> `kdrift` is maintained by a solo systems developer, not an enterprise infrastructure team. Complex enterprise extensions (such as UKIs, TPM measured boot, and cryptographic signing pipelines) require dedicated testing infrastructure, hardware testbenches, and developer-hours. Features marked with **[FUNDING REQUIRED]** are formally scoped and architected below, but their execution is deferred until external sponsorship or commercial backing is secured.

---

## Strategic Milestone Trajectory

```
  [ V1 Prod ] ──► [ V2 Prod ] ──► [ V2.5 Prod ] ──► [ V3 Prod ] ──► [ V4 Prod ] ──► [ V5 Prod ]
   Musl Core       Kconfig FFI     Patch Blame       Hardware        Secure Boot &    Ramoops /
   Tristate/Str    Binding         Engine            Watchdog        TPM / UKI        pstore
   Multicall       (scripts/conf)  (Stretch Goal)    Hand-off        (Needs Funding)  Autopsy
```

* **V1 Production**: Tristate (`=m`) & String/Numeric Invariants + Musl Statically Linked Multicall Daemon.
* **V2 Production**: Upstream-Compliant Rust FFI Binding into `scripts/kconfig/conf` / `libkconfig`.
* **V2.5 Production (Stretch)**: Semantic Patch Error Blame Engine (*if time permits; otherwise deferred*).
* **V3 Production**: Physical Hardware Watchdog (`/dev/watchdog`) & NMI Hard-Lockup Preemption.
* **V4 Production**: Modern Firmware Matrix: Secure Boot, TPM Measured Boot & Unified Kernel Images (UKI) (*solo maintainer gated; requires funding*).
* **V5 Production**: Kernel Crash Dump Autopsy via `ramoops` / `pstore`.

---

## Milestone V1 (Production Core): Static Daemon & Complete Typing

### 1.1. Static Rust Core & Zero-Dependency Multicall Binary
- [ ] **CPython & Shell Script Elimination**
  - Rewrite all logic from `kdrift/` and `install.sh` into pure, memory-safe Rust.
  - Compile with `musl-libc` (`x86_64-unknown-linux-musl`, `aarch64-unknown-linux-musl`, `riscv64gc-unknown-linux-musl`) to eliminate dynamic linking dependencies.
  - Enforce binary size constraints: stripped ELF $< 8\,\text{MB}$ and baseline resident memory $< 4\,\text{MB}$ RSS to guarantee native execution inside initramfs RAM disks and rescue environments.
  - Deliver a unified multicall binary dispatching subcommands (`kdrift`, `kdrift-assert`, `kdag`, `kpatch`) via binary symlink inspection (`std::env::args_os().next()`).
- [ ] **Crash-Resilient State Machine (`kdrift-core`)**
  - Implement formal transitions:
    $$\text{STABLE} \longrightarrow \text{STAGED} \longrightarrow \text{ARMED\_TRIAL} \longrightarrow \text{TRIAL\_PROBING} \longrightarrow \begin{cases} \text{PROMOTED} \\ \text{DEGRADED\_TRIAL} \\ \text{ROLLED\_BACK} \end{cases}$$
  - Atomic persistence via volatile write buffers, explicit `fsync`, and atomic POSIX renames (`std::fs::rename`).
  - Resolve state-lock desynchronization on power drops:
    $$\text{State} == \text{ARMED\_TRIAL} \land \text{RunningKernel} \neq \text{CandidateID} \implies \text{Transition}(\text{ROLLED\_BACK})$$

### 1.2. Complete Invariant Typing (Tristate, String, Numeric)
- [ ] **Tri-State Assertions (`=y` vs `=m` vs `n`)**
  - Enforce explicit modularity constraints in `invariants.profile` to prevent monolithic builds from silently converting required loadable modules into built-in drivers or dropping them entirely.
- [ ] **String & Path Assertions**
  - Parse and validate literal configuration strings:
    - Embedded microcode blobs: `CONFIG_EXTRA_FIRMWARE="intel-ucode/... amd-ucode/..."`
    - Firmware search directories: `CONFIG_EXTRA_FIRMWARE_DIR="/lib/firmware"`
    - Hardcoded command lines: `CONFIG_CMDLINE="console=ttyS0,115200 root=..."`
- [ ] **Numeric & Hex Boundary Assertions**
  - Validate address bounds, alignments, and sizing thresholds (e.g., `CONFIG_PHYSICAL_START=0x1000000`, `CONFIG_LOG_BUF_SHIFT=18`).

### 1.3. Generational Storage & Rotation
- [ ] **Content-Addressed Generational Allocation**
  - Deterministic artifacts: $\text{GenID} = \text{krel} - \text{sha256}(\text{.config})[0..8]$.
  - Temporal FIFO rotation queue capped at $N$ kernels (`kdrift set-retention`).
  - Persistent pinning (`kdrift persist <ID>`) and module tree garbage collection in `/lib/modules/`.

---

## Milestone V2 (Upstream Fidelity Engine): Rust FFI Binding

### 2.1. Upstream Kconfig FFI Binding (`kdrift-engine`)
*Critical Architectural Shift:* Independent pure-Rust Kconfig reimplementations are rejected. The upstream Kconfig parser (`scripts/kconfig/{zconf.y,preprocess.c}`) contains thousands of lines of subtle macro evaluations (`$(cc-option)`, `$(as-version)`), file inclusions, and compiler-probing edge cases that drift between kernel versions.
- [ ] **Target-Tree FFI Integration**
  - Do not maintain an independent parser from scratch.
  - Compile the target kernel's own `scripts/kconfig/conf` or interface with `libkconfig` directly using Rust FFI bindings (`bindgen` against upstream `lkc.h`).
  - Pass the exact target compiler and cross-compilation environment variables into the FFI boundary, guaranteeing 100% semantic AST fidelity with the upstream kernel release being built.
- [ ] **AST Invariant Resolution & Dependency Tracing**
  - Traverse upstream symbol trees directly in memory via FFI.
  - Extract exact blocking dependency expressions (`depends on`, `select`, `visible if`) when an assertion in `invariants.profile` is truncated.
- [ ] **Mathematical Invariant Synthesis (`kdag`)**
  - Formalize graph reduction on the active implication graph extracted via FFI.
  - Eliminate dominated selections using transitive reduction; prune `defconfig` redundancies to emit minimal, auditable invariant profiles.

---

## Milestone V2.5 (Stretch Goal): Patch Error Blame Engine

> **DISCLAIMER:** This milestone is strictly pursued **only if development bandwidth and schedule permit**. If edge cases consume allocated time, this capability will be deferred.

### 5.3. Semantic Compiler-Error-to-Patch Blame Attribution Engine
- [ ] **Source Map Tracking (`SourceSpanManifest`)**
  - During `kpatch` injection, construct an in-memory spatial coordinate map tracking modified files, line ranges, and originating patch files.
- [ ] **Compiler Diagnostics Interception**
  - Intercept Clang and GCC structured diagnostic streams (`-fdiagnostics-format=json`).
  - If a compilation failure occurs within an injected span, match the error coordinates against the manifest:
    ```text
    [KPATCH FAILURE DETECTED]
    Target File: drivers/platform/x86/asus-wmi.c:1240
    Responsible Patch: patches/02-rog-fan-curve.patch
    Causal Error: error: too few arguments to function 'thermal_zone_device_register'
    ```
- [ ] **Graceful Fallback**
  - If coordinate resolution fails due to fuzzy patch offsets, cleanly fall back to pristine RAII tree rollback without halting the toolchain.

---

## Milestone V3 (Hardware Reliability): Hardware Watchdog

### 3.4. Hardware Watchdog (`/dev/watchdog`) & NMI Hard-Lockup Preemption
- [ ] **The Threat Model**
  - When candidate kernels deadlock inside an interrupt-disabled spinlock or trigger a hard bus lockup, software timers freeze and `panic=10` will never fire.
- [ ] **Bare-Metal Hardware Hand-Off Driver**
  - Prior to arming a trial reboot, open and program the physical motherboard watchdog (`/dev/watchdog` via Intel TCO, AMD SP5100, or IPMI BMC) with a non-maskable countdown (e.g., 180 seconds).
  - If the machine hard-locks in ring-0, the hardware timer pulls the physical motherboard reset line.
  - Upon successful userspace initialization, the `kdrift-watchdog` service assumes custody of the device node and feeds the heartbeat timer.
- [ ] **Somatic Userspace Heartbeat Daemon**
  - Guard the probation window (default: 300s) against visually dead or input-dead states:
    - DRM/KMS check: Query `/sys/class/drm/*/status` for connected displays.
    - HID input check: Verify active input nodes in `/dev/input/event*`.
    - Network carrier assertion: Verify operational carrier states on designated interfaces.

---

## Milestone V4 [FUNDING REQUIRED]: Secure Boot, TPM & UKI Matrix

> **SOLO MAINTAINER STATUS:**  
> Modern enterprise cryptographic boot infrastructure requires matrix testing across dedicated physical hardware (multiple discrete TPM 2.0 implementations, diverse OEM UEFI firmware implementations, and signed shim ecosystems). As a solo developer, this milestone is blocked until dedicated grant funding or corporate sponsorship is secured.

### 4.4. Modern Unified Cryptographic Boot
- [ ] **Unified Kernel Images (UKI) Support**
  - Construct Type-2 UKIs combining kernel, initramfs, hardcoded command line, and OS release into a single PE binary via `ukify` / `systemd-stub`.
  - Dispatch trial boots natively using the EFI variable `LoaderEntryOneShot` via `bootctl` or direct `efivarfs` I/O, bypassing legacy GRUB block mutations entirely.
- [ ] **TPM 2.0 Measured Boot & PCR Pre-calculation**
  - **The Problem:** Systems utilizing TPM2-sealed root LUKS volumes fail to decrypt during trial boots because unstaged candidate kernels measure unfamiliar hashes into PCR 4/7/11, deadlocking before userland is reached.
  - **The Fix:** Pre-calculate expected candidate UKI measurement hashes and stage transient TPM policy authorizations prior to reboot.
- [ ] **UEFI Secure Boot & MOK Signing Infrastructure**
  - Detect Secure Boot enforcement via `/sys/firmware/efi/efivars/SecureBoot-*`.
  - Provide an isolated hook bridge allowing external key management daemons or hardware security modules (HSMs) to sign staged candidate binaries with a local Machine Owner Key (MOK).

---

## Scope Boundary: Multi-Volume / Snapshot Collisions

* **Status:** **Explicitly Out of Scope / Not Our Problem Currently**
* **Rationale:** Addressing Btrfs/ZFS snapshot rollbacks, subvolume synchronization, and rootfs state desynchronization during an early boot kernel revert adds significant non-kernel stateful complexity. 
* Until Milestone V4 (Unified Kernel Images and modern bootloader abstractions) is completed and dedicated testing funding is secured, filesystem snapshot state consistency remains the administrative responsibility of the host system's snapshot manager (e.g., `snapper`, `zfs-auto-snapshot`).

---

## Milestone V5 (Post-Mortem Telemetry): Ramoops / Pstore Autopsy

### 3.2. Crash Dump Autopsy via `pstore` / `ramoops`
- [ ] **Crash Logging Baseline Invariants**
  - Enforce `CONFIG_PSTORE=y`, `CONFIG_PSTORE_RAM=y`, and `CONFIG_PSTORE_CONSOLE=y` in the canonical `[ALWAYS]` invariant baseline.
- [ ] **Automated Post-Mortem Extraction**
  - Upon fallback boot into the stable kernel after a trial crash, harvest crash artifacts from `/sys/fs/pstore/dmesg-ramoops-*`.
  - Parse panic signatures (e.g., `VFS: Unable to mount root fs on unknown-block(0,0)`).
  - Map specific panic traces back to missing Kconfig configuration prerequisites (e.g., missing storage host controller drivers, missing root filesystem modules).
  - Output an actionable diagnostic summary and generate a candidate configuration patch to remediate the missing invariants.
