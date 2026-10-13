# kdrift: Declarative Linux Kernel Invariant Assertion, DAG Analysis & Zero-Trust Trial Boot Reconciler

> **DEVELOPMENT STATUS & PROTOTYPE NOTICE:**  
> This repository contains an experimental Proof-of-Concept (PoC) developed to validate the architectural primitives of declarative Kconfig invariant assertions and zero-trust one-shot trial boots. 
> 
> *Architecture, state machine topologies, and systems design by the author.* Generative AI (Google Gemini) was utilized strictly as an accelerator for code scaffolding and documentation drafting during the exploratory phase. 
> 
> Because heuristic prototyping and dynamic scripting runtimes cannot guarantee ring-0 safety, this PoC is currently undergoing a complete ground-up rewrite into an audited, statically linked Rust daemon. See [`TODO.md`](TODO.md) for the production architecture specification.

---

## 1. Ontological Architecture & Problem Definition

Upstream Linux kernel configuration pipelines rely heavily on conditional dependency satisfaction compiled via Kconfig’s Directed Acyclic Graph (DAG). Traditional automated fragment injectors (`merge_config.sh`, distribution package scripts, ebuild pipelines) operate as **lossy feedforward pipelines**. 

```
[ Baseline .config ] ──┐
                       ├──► [ merge_config.sh ] ──► [ make olddefconfig ] ──► [ Resolved .config ]
[ Fragment / Invariant ]─┘                              │                              │
                                                        │ (Silent Drop if              │
                                                        │  Deps Unsatisfied)           ▼
                                                        └───────────────────► [ Option Forced Off ]
                                                                              (Exit Code: 0)
```

When a kernel configuration fragment specifies an option whose structural DAG prerequisites (hardware bus dependencies, architecture support, prompt visibility conditions, compiler capabilities) are unmet, `make olddefconfig` executes a silent drop. The build compiles cleanly with exit code `0`, yet the resulting kernel binary boots with severed security mitigations, stripped namespaces, disabled virtualization, or unmounted storage drivers.

The `kdrift` toolchain resolves this vulnerability through a closed-loop control system:
1. **Structural AST Extraction & Invariant Synthesis (`kdag`)**: Mathematical reduction of the Kconfig DAG into critical structural hubs and topological strata, generating an AST-minimized `invariants.profile`.
2. **Deterministic Compiler & Toolchain Isolation (`kdrift-toolchain`)**: Host compiler discovery and cross-compilation target synthesis across x86_64, i386, aarch64, armv7l, riscv64, and ppc64le with GCC or integrated LLVM suites.
3. **Transactional Kernel Patching & Kbuild Injection (`kpatch`)**: Non-destructive, RAII-guarded source tree overlaying, atomic unified diffing, and idempotent Kbuild appends.
4. **Invariant Constraint Enforcement (`kdrift-assert`)**: AST-level verification of post-merge symbols against the invariant profile, tracing blocked direct/prompt dependencies and reverse selections if violations occur.
5. **Generational Storage & Anti-Regression Trial Boot Reconciler (`kdrift`)**: Content-addressed kernel artifact isolation (`candidate_gen = ${krel}-${cfg_hash}`), out-of-tree DKMS gating, progressive FIFO rotation with critical kernel pinning, and one-shot GRUB trial booting guarded by runtime health assertions.

---

## 2. Structural Topology

```
├── pyproject.toml              # Build backend (setuptools) registering kdrift-assert, kdrift, kdag, kdrift-toolchain
├── install.sh                  # Polymorphic installer (Alpine, Arch, Fedora/RHEL, Gentoo, Debian/Ubuntu)
├── invariants.profile          # Canonical declarative policy defining [ALWAYS] and [NEVER] constraints
├── kdrift/
│   ├── __init__.py
│   ├── cli.py                  # kdrift-assert: AST dependency tracing, fragment injection, invariant auditing
│   ├── drift.py                # kdrift: Trial boot state machine, progressive storage GC, DKMS gate, GRUB orchestration
│   ├── kdag.py                 # kdag: Graph degree inflection, topological strata boundary, defconfig pruning
│   ├── kpatch.py               # kpatch: RAII-managed tree overlays, Kbuild appends, unified diff rollback engine
│   └── toolchain.py            # Toolchain probe, LLVM/GCC cross-compilation synthesis, make argument factory
└── files/                      # Interceptors, hooks, and init services
    ├── 90-kdrift.hook          # Arch Linux pacman transaction hook
    ├── 99-kdrift.install       # Fedora/RHEL kernel-install hook
    ├── zz-kdrift               # Debian/Ubuntu postinst.d hook
    ├── kdrift-boot-check.service # Systemd early-boot integrity audit service
    ├── kdrift-shutdown.service # Systemd graceful poweroff promotion hook
    ├── kdrift-git-sync.timer   # Systemd source polling timer
    ├── kdrift-git-sync.service # Systemd source tree build trigger
    ├── kdrift-boot.start       # OpenRC local.d early-boot check
    ├── kdrift-shutdown         # OpenRC runlevel shutdown hook
    └── kdrift-git-sync.cron    # Periodic cron fallback script
```

---

## 3. End-to-End Operational Lifecycle

The system operates across a 5-stage lifecycle transitioning from raw manual tuning to autonomous, zero-trust invariant maintenance.

```
+---------------------------------------------------------------------------------------------------------+
|                                        TYPICAL WORKFLOW TOPOLOGY                                        |
+---------------------------------------------------------------------------------------------------------+

  [ STAGE 1: INGRESS ]          Download vanilla / generic tree (/usr/src/linux)
                                Run make defconfig / make localmodconfig / make localyesconfig
                                            │
                                            ▼
  [ STAGE 2: TUNING ]           Run make menuconfig (tailor drivers, security posture, scheduler)
                                            │
                                            ▼
  [ STAGE 3: EXPERIMENTATION ]  Inject machine-specific overlays or unified diffs into /usr/src/kpatch
                                Test build manually or stage candidate via kdrift reconcile
                                            │
                                            ▼
  [ STAGE 4: SYNTHESIS ]        Extract DAG structural invariants via kdag:
                                  kdag --kernel-dir /usr/src/linux --config /usr/src/linux/.config \
                                       --defconfig x86_64_defconfig --out-profile invariants.profile
                                Audit & fine-tune the resulting [ALWAYS] and [NEVER] registers
                                            │
                                            ▼
  [ STAGE 5: AUTOMATION ]       Lock policy to /etc/kernel/invariants.profile
                                Enable package manager interceptors or git repo sync daemons
                                Autonomous lifecycle: Compile -> kpatch -> Invariant ICE Gate ->
                                DKMS Assert -> One-Shot GRUB Boot -> Health Watchdog -> Promotion / Rollback
```

### Step 1: Base Configuration Ingress
Acquire the desired upstream or distribution kernel source inside `/usr/src/linux`. Seed a baseline configuration matching hardware constraints:
```bash
cd /usr/src/linux

# Strategy A: Baseline architectural defaults
make defconfig

# Strategy B: Hardware-tailored configuration based on currently loaded modules
make localmodconfig
```

### Step 2: Custom Parameterization
Fine-tune internal kernel registers to target system requirements:
```bash
make menuconfig
```

### Step 3: Experimental Patches & Invariant Tree Injections (`kpatch`)
If your hardware target requires custom vendor C source drivers, Kbuild Makefile adjustments, or upstream hotfixes, structure them inside `/usr/src/kpatch`:
- **Source Tree Additions (`/usr/src/kpatch/tree/`)**: Mirror the relative kernel path (e.g., `tree/drivers/platform/x86/custom_sensor.c`).
- **Makefile/Kbuild Appends (`/usr/src/kpatch/append/`)**: Target Makefiles with inject chunks (e.g., `append/drivers/platform/x86/Makefile` containing `obj-y += custom_sensor.o`). `kdrift` automatically injects delimited demarcations.
- **Unified Diffs (`/usr/src/kpatch/patches/`)**: Plain `.patch` files applied sequentially with dry-run assertions.

Test manual application and sanitization:
```bash
# Test patch application transaction
kdrift patch-apply --kernel-dir /usr/src/linux

# Revert back to pristine baseline
kdrift patch-clean --kernel-dir /usr/src/linux
```

### Step 4: Automatic Invariant Synthesis via DAG Analysis (`kdag`)
Rather than hand-crafting assertion profiles, run the `kdag` engine. It converts the Kconfig AST into a dual-class directed graph ($V_{sel}$ user-selectable prompts vs. $V_{hid}$ hidden internal selects), determines graph degree inflection boundaries, isolates topological strata, maps hidden states back to selectable parents, and prunes defconfig redundancies.
By default, `kdag` extracts critical candidates using a union of degree hubs ($\text{deg} \ge k$) and base topological strata ($\text{lvl} \le x_0$). On heavily customized, monolithic, or hardware-specific trees, you can bypass or parameterize these filters:

- **Mode A: Total Filter Bypass (`--skip-all-filters` / `--all-symbols`)**  
  Passes 100% of symbols from the Kconfig tree into the AST invariant resolver without dropping sparse or deep options:
  ```bash
  kdag --kernel-dir /usr/src/linux \
       --config /usr/src/linux/.config \
       --defconfig x86_64_defconfig \
       --skip-all-filters \
       --out-profile /etc/kernel/invariants.profile
  ```
- **Mode B: Skip Degree Filter (`--skip-degree-filter`)**  
  Bypasses the Task 1 hub threshold ($\text{deg} \ge k$), capturing foundational strata strictly by topological depth.
- **Mode C: Skip Level Filter (`--skip-level-filter`)**  
  Bypasses the Task 2/3 strata threshold ($\text{lvl} \le x_0$), capturing symbols strictly by connection density.
- **Mode D: Parametric Boundary Overrides (`--min-degree <k>`, `--max-level <x_0>`)**  
  Manually pin threshold boundaries rather than relying on automatic inflection and finite-difference derivatives.

Inspect the generated `/etc/kernel/invariants.profile`:
```ini
# Generated by kdag: Compiler-Aware Invariant Compiler (v3.1.0)

[ALWAYS]
CONFIG_64BIT=y
CONFIG_BINFMT_ELF=y
CONFIG_BLK_DEV_INITRD=y
CONFIG_CGROUPS=y
CONFIG_HARDENED_USERCOPY=y
CONFIG_IKCONFIG=y
CONFIG_IKCONFIG_PROC=y
CONFIG_NAMESPACES=y
CONFIG_SECCOMP=y
CONFIG_STRICT_KERNEL_RWX=y

[NEVER]
CONFIG_FLOPPY=n
CONFIG_IA32_EMULATION=n
CONFIG_X86_X32=n
```
Manually adjust specific symbols if organizational security policies require mandatory inclusions or exclusions.

> **CRITICAL OPERATIONAL DIRECTIVE:**  
> While `kdag` accurately automates the bulk extraction of Kconfig invariants, its current heuristic resolver can introduce minor regressions (~2–10 symbols) on heavily configured, monolithic, or edge kernel trees. **Manual inspection of the generated `invariants.profile` is strictly mandatory before arming `kdrift reconcile`, as least on the first compilation cycle.** Verify that hardware buses, root filesystem drivers, and critical security options (`CONFIG_NAMESPACES`, `CONFIG_SECCOMP`, storage controllers) match your explicit baseline.

### Step 5: Autonomous Reconciler Engagement (`kdrift`)
Once the invariant profile is locked, handover operational maintenance to `kdrift`:
```bash
# Execute transactional build, DKMS compilation gate, and one-shot trial arming
kdrift reconcile \
  --kernel-dir /usr/src/linux \
  --profile /etc/kernel/invariants.profile \
  --base-config /usr/src/linux/.config
```

---

## 4. Subsystem Deep-Dive

### 4.1. Toolchain & Cross-Compilation Matrix (`kdrift-toolchain`)
Managed via `/etc/kernel/toolchain.json`. Automatically maps architecture targets to Kbuild flags and tool suites:
- **GCC Toolchain**: Leverages architecture-prefixed tuples (e.g., `aarch64-linux-gnu-gcc`).
- **Clang/LLVM Integrated (`LLVM=1`, `LLVM_IAS=1`)**: Routes linking through `ld.lld`, assembly via Clang's integrated assembler, and symbol utilities through `llvm-nm`, `llvm-objcopy`, and `llvm-readelf`.
- **Compiler Acceleration**: Dispatches `ccache` dynamically during compilation pipelines while isolating pristine compilers during Kconfig dependency checks.

Run interactive re-configuration:
```bash
kdrift-toolchain
```

### 4.2. Invariant Assertion Engine (`kdrift-assert`)
Validates configurations without side-effects or executes merge pipelines:

```bash
# Audit an existing kernel tree against constraints
kdrift-assert check-source \
  --kernel-dir /usr/src/linux \
  --profile /etc/kernel/invariants.profile

# Merge baseline config with invariants and diagnose truncated options
kdrift-assert merge \
  --kernel-dir /usr/src/linux \
  --base-config /boot/config-$(uname -r) \
  --profile /etc/kernel/invariants.profile
```

If an invariant is truncated by Kconfig logic, `kdrift-assert` executes an AST diagnostic trace:
```text
[ERROR] CONFIG_HARDENED_USERCOPY assertion failed! Expected 'y', resolved as 'n'
[ERROR]   [REASON] HIDDEN in menu (parent or compiler condition evaluates to false). | Prompt dependencies: HAVE_HARDENED_USERCOPY_ALLOCATOR
```

### 4.3. Transactional Reconciler & Boot State Machine (`kdrift`)

`kdrift` treats the running OS kernel as mutable state guarded by a state machine:

```
               [ kdrift reconcile triggered ]
                             │
                             ▼
                 [ KPatch Transaction Arm ] ─── Dry-run Fail ──► [ Abort Transaction ]
                             │ Clean
                             ▼
                 [ Invariant Assertion Gate ] ── Truncated ────► [ KPatch Rollback & Halt ]
                             │ Pass
                             ▼
                 [ Kernel & Module Compile ] ── Error ────────► [ KPatch Rollback & Halt ]
                             │ Success
                             ▼
                 [ DKMS Verification Gate ] ─── Error ────────► [ KPatch Rollback & Halt ]
                             │ Success
                             ▼
                 [ Stage Candidate Artifacts ]
                   /boot/vmlinuz-candidate-${ID}
                   /boot/initramfs-candidate-${ID}.img
                             │
                             ▼
                 [ Armed One-Shot GRUB Boot ]
                   (grub-reboot / grub2-reboot)
                   State: ARMED_TRIAL
                             │
             ┌───────────────┴───────────────┐
             │ Machine Reboots               │
             └───────┬───────────────┬───────┘
                     │               │
       [ Boot Hang / Kernel Panic ]  │ [ Successful Init Executed ]
                     │               │
                     ▼               ▼
         [ Reset / Hardware Watchdog ] [ Early Boot Audit: kdrift-boot-check ]
                     │               │ - Verifies running kernel == candidate
                     │               │ - Transitions to TRIAL_PROBING
                     ▼               │ - Spawns 300s Async Lease Timer
           [ GRUB Default Boot ]     │
           - Boots active stable     ├─────────────────────────────────┐
           - Early boot marks state  ▼                                 ▼
             as ROLLED_BACK     [ Lease Timeout: 300s ]     [ Graceful Shutdown ]
                                (kdrift lease-promote)       (kdrift-shutdown.service)
                                     │                                 │
                                     └──────────────┬──────────────────┘
                                                    │
                                                    ▼
                                       [ audit_runtime_health() ]
                                       - RW filesystem verification
                                       - Init system viability check
                                       - dmesg alert/emerg buffer check
                                                    │
                                      ┌─────────────┴─────────────┐
                                      │ Pass                      │ Fail
                                      ▼                           ▼
                             [ Candidate Promoted ]       [ State: DEGRADED_TRIAL ]
                             - Copied to stable-${ID}     - Promotion Refused
                             - Symlinked to custom
                             - State: STABLE
```

### 4.4. Progressive Storage Engine & Generational Retention
Custom kernels accumulate in `/boot` and `/lib/modules`. `kdrift` prevents filesystem exhaustion via an automated generational retention engine:
- **Temporal FIFO Rotation Queue**: Historical kernels are rotated up to a user-defined threshold $N$ (default: 5).
- **Persistent Invariant Pinning**: Critical, battle-tested kernels can be isolated from the rotation queue to prevent deletion during automated garbage collection.
- **Module Garbage Collector**: Purges unreferenced modules in `/lib/modules/` when kernel generations are evicted.

```bash
# View active kernel, rotation queue, and pinned inventory
kdrift list

# Pin running kernel (or explicit ID) to prevent automated purging
kdrift persist

# Unpin kernel, returning it to temporal decay queue
kdrift unpersist 6.12.1-a1b2c3d4

# Adjust historical queue capacity
kdrift set-retention 10

# Manually trigger retention policy and module garbage collector
kdrift prune
```

### 4.5. Multi-Tier GRUB Environment Architecture (`grubenv`)

Standard distributions frequently lack `grub-set-default`, package inconsistent CLI wrappers, or run inside minimal rescue environments (such as initramfs) where no GRUB binaries exist in `$PATH`. To eliminate bootloader state-lock paradoxes, `kdrift` implements a resilient 3-tier dispatch engine:

```
                  [ kdrift Bootloader Dispatcher ]
                                  │
         ┌────────────────────────┼────────────────────────┐
         ▼                        ▼                        ▼
  [ TIER 1: WRAPPERS ]    [ TIER 2: UTILITY ]      [ TIER 3: BARE-METAL ]
  grub-set-default        grub-editenv             Native 1024-Byte Block Engine
  grub-reboot             - set saved_entry=...    - In-place binary I/O
                          - set next_entry=...     - Exact byte padding (#)
                                                   - Zero CLI dependencies
                                                   - Initramfs / Rescue native
```

1. **Tier 1 (Distro Wrapper):** Calls `grub-set-default` / `grub-reboot` if present in `$PATH`.
2. **Tier 2 (Core Utility):** If wrapper scripts are missing, invokes `grub-editenv` directly against `/boot/grub/grubenv` (`set saved_entry=...` and `set next_entry=...`).
3. **Tier 3 (Zero-Dependency Native Block Engine):** If no GRUB binaries are installed (e.g., inside an emergency initramfs shell or stripped Alpine container), `kdrift` directly reads, updates, and atomically writes the physical 1024-byte `grubenv` block in-place with exact `#` padding.

#### Debian / Ubuntu Menu Flattening & Drop-in Overrides
Debian-based systems ship with `GRUB_DEFAULT=0` and nest secondary kernels inside an "Advanced options" submenu by default. This causes candidate kernels to hijack Index 0 and traps fallback kernels inside nested submenus. `kdrift` automatically provisions `/etc/default/grub.d/99-kdrift.cfg`:
```sh
GRUB_DISABLE_SUBMENU=y      # Flattens all kernels to root level (O(1) access)
GRUB_DEFAULT=saved          # Decouples default boot from Index 0
GRUB_SAVEDEFAULT=false      # Prevents unverified kernels from auto-saving
```
Because `/etc/default/grub.d/*.cfg` files are sourced alphabetically by `/usr/sbin/grub-mkconfig`, `99-kdrift.cfg` cleanly overrides `/etc/default/grub` without modifying user files.
```


---

## 5. Installation & Distribution Integration

```bash
git clone https://github.com/your-org/kdrift.git
cd kdrift
sudo ./install.sh
```

During installation, the installer configures system hooks based on the deployment strategy:
1. **Package Manager Interceptors**:
   - **Arch Linux**: `/etc/pacman.d/hooks/90-kdrift.hook`
   - **Debian / Ubuntu**: `/etc/kernel/postinst.d/zz-kdrift`
   - **Fedora / RHEL**: `/etc/kernel/install.d/99-kdrift.install`
   - **Gentoo**: Portage environment hook `/etc/portage/env/sys-kernel/gentoo-sources/kdrift`
2. **Git Source Tree Automation**: Background sync via `kdrift-git-sync.timer` (systemd) or cron polling local repositories.
3. **Hybrid**: Deploys both package manager hooks and background repository monitoring.

---

## 6. CLI Reference Matrix

| Command | Arguments | Operational Function |
|---|---|---|
| `kdrift-assert compile` | `--profile <path> --output <path>` | Compiles an INI invariant profile into a flat Kconfig fragment. |
| `kdrift-assert merge` | `--kernel-dir <path> --base-config <path> --profile <path> [--arch <arch>]` | Executes non-destructive merge, checks invariants, traces blocked AST paths. |
| `kdrift-assert check-source` | `--kernel-dir <path> --profile <path> [--arch <arch>]` | Validates active source tree `.config` against invariants profile. |
| `kdag` | `--kernel-dir <dir> --config <cfg> [--defconfig <def>] [--skip-all-filters] [--skip-degree-filter] [--skip-level-filter] [--min-degree <k>] [--max-level <lvl>]` | Analyzes Kconfig DAG, executes heuristic strata/degree filtering or full-tree bypass, prunes defconfig redundancies. |
| `kdrift-toolchain` | *(None)* | Launches interactive CLI wizard to configure target architecture, cross-prefixes, and compilers. |
| `kdrift reconcile` | `--kernel-dir <dir> --profile <prof> [--base-config <cfg>]` | Full pipeline: KPatch -> Invariant Gate -> Build -> DKMS -> Fallback Anchor -> One-Shot Trial -> Multi-Tier GRUB. |
| `kdrift git-reconcile` | `--kernel-dir <path> --profile <path> [--min-commits <n>] [--min-age-seconds <s>]` | Throttled reconciliation for Git repositories based on commit distance and age. |
| `kdrift install-git-hook` | `--kernel-dir <path> --profile <path>` | Deploys native `post-merge` and `post-commit` hooks into target repository. |
| `kdrift patch-apply` | `--kernel-dir <path>` | Verifies and applies `/usr/src/kpatch` overlays, appends, and diffs. |
| `kdrift patch-clean` | `--kernel-dir <path>` | Reverts all injected patches, appends, and overlays back to clean baseline. |
| `kdrift list` | *(None)* | Displays current boot state, rotation queue, and pinned invariant allocations. |
| `kdrift persist` | `[kernel_name]` | Pins a generation to prevent garbage collection eviction. |
| `kdrift unpersist` | `[kernel_name]` | Returns pinned generation back to FIFO temporal rotation queue. |
| `kdrift set-retention` | `<count>` | Sets maximum historical kernel count $N$ retained in rotation queue. |
| `kdrift prune` | *(None)* | Forces retention limit enforcement and purges orphaned `/lib/modules`. |
| `kdrift commit` | *(None)* | Forces manual promotion of candidate generation to active stable. |
| `kdrift rollback` | *(None)* | Aborts candidate, restores fallback bootloader pointer, and reboots. |

---

## 7. Notes from the Author & Known Edge Cases

### Firmware & Secure Boot Pre-flight Assertion
> **IMPORTANT:** `kdrift` v3.1 is an experimental bare-metal PoC designed primarily for Coreboot/SeaBIOS, developer workstations, and unconstrained testbeds. **UEFI Secure Boot must be disabled in firmware setup.** Candidate kernel images staged by the PoC are not signed with a local Machine Owner Key (MOK). Attempting to trial-boot on a Secure Boot-enforced system will result in immediate firmware rejection (`Image signature verification failed`). MOK signing will be addressed later via hook to external scripts (see `TODO.md`).

### 7.1. Operational Warnings & Quirks
- **Monolithic Kernels (`CONFIG_BLK_DEV_INITRD=n`)**: If compiling a kernel without an initramfs, GRUB cannot parse filesystem UUID abstractions (`root=UUID=...`). Ensure root devices are addressed via direct device nodes (e.g., `root=/dev/nvme0n1p2`) or set `GRUB_DISABLE_LINUX_UUID=true` in `/etc/default/grub`.
- **Missing `CONFIG_EXTRA_FIRMWARE` Invariant Support (Monolithic Blob Ingestion)**: When running monolithic kernels (`CONFIG_MODULES=n`), drivers requiring external microcode (e.g., GPU, Wi-Fi, Ethernet, CPU microcode) require `CONFIG_EXTRA_FIRMWARE` and `CONFIG_EXTRA_FIRMWARE_DIR` to bake firmware blobs directly into the binary. Because the current `invariants.profile` engine does not yet parse string literals, string assignments in the profile are ignored. 
  - *Mitigation / Workaround:* Pre-populate these parameters directly in your baseline `.config` and pass `--base-config /path/to/.config` into `kdrift reconcile`. The Kconfig merge pipeline will preserve these strings during tree synthesis.
- **Systemd vs. POSIX Subshell Watchdog**: On systems utilizing OpenRC or bare runit, `kdrift` falls back to an asynchronous detached subshell (`sleep 300 && kdrift lease-promote`). If the host is powered off within 300 seconds without triggering a clean init shutdown, the candidate will remain unpromoted until the next clean boot or manual commit.
- **DKMS Host Module Cross-Compilation**: Cross-compiling for foreign architectures (`target_arch != host_arch`) automatically bypasses DKMS checks. Host DKMS engines cannot compile out-of-tree binary modules for foreign targets unless appropriate cross-wrapper toolchains are registered within the host DKMS framework.

### 7.2. Buggy Edge Cases Under Tracking
1. **Missing Tri-State (`=m`) and String Invariant Assertions:**
   - *Symptom:* The current profile engine enforces binary constraints (`=y` vs `n`). Specifying modular components (`CONFIG_*=m`) or configuration strings (such as embedded firmware blobs: `CONFIG_EXTRA_FIRMWARE="..."`) is not yet supported natively inside `invariants.profile`.
   - *Status:* Slated for next version. 
2. **Visually Dead or Unresponsive Input Trial Boots:**
   - *Symptom:* If a candidate kernel boots cleanly to userspace but lacks graphical, display, or HID input drivers (touchpad/keyboard/USB), the 300-second probation timer begins counting down. If the user executes a hard hardware power reset, the system may reboot back into stable while leaving the internal state machine stuck in `ARMED_TRIAL` rather than transitioning to `ROLLED_BACK`.
   - *Remedy:* Manually run `sudo kdrift rollback` upon booting into the stable fallback kernel. The Milestone 1 Rust heartbeat daemon addresses this by requiring active local display/input telemetry before enabling promotion.
3. **Kdag Regression Leakage on Heavily Parameterized Trees:**
   - *Symptom:* While `kdag` reliably isolates structural hubs and reduces Kconfig options in the general case, its degree-inflection boundary and strata-depth algorithms remain heuristic approximations. On heavily tuned, highly non-standard, or specialized kernels, the auto-generated profile is known to introduce a small handful of discrete regressions (typically 2 to 10 symbols resolved opposite to intent or dropped).
   - *Operational Rule:* **Always manually audit the generated `invariants.profile` prior to locking it into production.** Compare the generated `[ALWAYS]` and `[NEVER]` tables against your baseline `.config` diff before dispatching automated reconciliation loops.
4. **Early Boot Kernel Panics (VFS Mount Failure):**
   - *Symptom:* Missing critical storage controllers or filesystem drivers causes the candidate kernel to panic immediately without mounting the root partition.
   - *Mitigation:* `kdrift` relies on GRUB's volatile memory registers for single-shot booting, meaning a hard reboot will return to the stable kernel. However, autonomous recovery requires hardware reset watchdog lines or `panic=10` passed to the candidate kernel's boot parameters.
5. **Coreboot / Libreboot GRUB Payload Desynchronization:**
   - *Symptom:* On systems running Libreboot or coreboot with a built-in GRUB payload, `kdrift reconcile` arms a candidate, but the machine reboots directly into the first option on the list without attempting the candidate.
   - *Root Cause:* The payload executes from SPI flash with `$prefix` bound to `(cbfsdisk)`. The distro's `/boot/grub/grub.cfg` attempts `load_env` against `$prefix/grubenv` instead of `($root)/boot/grub/grubenv`, failing to parse the `next_entry` register. Thus the SPI flash "grubenv" get applied, and the default selection is the first option on the list.
   - *Workaround / Mitigation:* Update `/etc/default/grub` to ensure `$prefix` is synchronized with `$root` before `load_env` is called, or utilize the `kdrift` explicit path injector (`load_env -f ($root)/boot/grub/grubenv`).
6. **Obscured Compiler Errors on Malformed KPatch / Injected Source Failures:**
   - *Symptom:* If an injected source file (`/usr/src/kpatch/tree/`), Kbuild fragment (`/usr/src/kpatch/append/`), or custom patch introduces a syntax error, broken macro, or missing header, the root-cause compiler diagnostics on the offending file may occasionally be obscured or swallowed by the build stream output.
   - *Behavior:* The RAII guard executes properly upon detecting the non-zero compilation return code, automatically reverting all injected overlays, appends, and unified diffs back to the pristine baseline without leaving dirty build artifacts in the tree.
   - *Workaround:* If a build fails immediately after adding new patches or tree overlays, test-compile the isolated driver object or inspect `make V=1` to expose the specific compiler error before staging it into `/usr/src/kpatch`.

## 8. Hardware & Platform Validation Matrix

The `kdrift` state engine and invariant compiler are continuously verified across physical bare-metal architectures, open-source firmware payloads, and virtualized CI targets.

### 8.1. Verified Hardware Testbeds

| Platform / Machine | Microarchitecture & Specs | Firmware Stack | OS & Target Kernel | Status & Field Telemetry |
| :--- | :--- | :--- | :--- | :--- |
| **Gigabyte MZ31-AR0** | AMD EPYC 7551 (32C/64T), 2× NVLink RTX 3090 | Proprietary Server AMI BIOS | Debian 13 (Trixie), Linux 6.12.112 | **VERIFIED (Pass)**<br>Full DKMS out-of-tree verification (NVIDIA drivers), module isolation, and one-shot GRUB fallback working cleanly. |
| **Lenovo ThinkPad X280** | Intel Core i5-8250U (Kaby Lake-R), Intel UHD 620 | Coreboot (SeaBIOS payload) | Alpine Linux 3.24, Linux 6.18.55 | **VERIFIED (Pass)**<br>Pure SeaBIOS boot to GRUB. Monolithic invariant profile and fast trial-boot reconciliation confirmed. |
| **Lenovo ThinkPad T480** | Intel Core i5-8250U (Kaby Lake-R), Intel UHD 620 | Libreboot (Neutered CSME) | Alpine Linux Edge, Linux 6.18.55 | **QUALIFIED (Workaround)**<br>Chained via SeaBIOS payload works cleanly. Direct boot via Libreboot SPI GRUB payload requires manual `$prefix` path normalization due to CBFS decoupling. |
| **HP EliteBook 2170p** | Intel Core i7-3667U (Ivy Bridge), Intel HD 4000 | Libreboot (16MB SPI) | Alpine Linux Edge, Linux 6.18.55 | **QUALIFIED (Workaround)**<br>Chained via SeaBIOS payload passes. Native SPI GRUB payload exhibits `$prefix` path mismatch; resolved via SeaBIOS secondary payload. |
| **QEMU Reference Node** | x86_64, Q35 Chipset, VirtIO SCSI/Net | SeaBIOS 1.16+ (Standard) | Debian 13 (Trixie), Linux 6.12.112 | **VERIFIED (Pass)**<br>Primary automated CI testbed. Panic injection (`panic=10`), disk pull, and state machine transitions execute cleanly. |

### 8.2. Experimental & In-Progress Hardware

| Platform / Machine | Target Substrate | Target OS & Kernel | Architectural Friction / Current Status |
| :--- | :--- | :--- | :--- |
| **Dell G3 3579** | Intel Core i5-8300H, GTX 1050Ti Mobile, `me_cleaner` | Debian 13 (Trixie), Linux 6.12.107 | **EXPERIMENTAL**<br>Firmware instability: neutered Intel ME triggers intermittent `TPM device not found` alerts on cold boots, occasionally invalidating EFI boot priority registers. |
| **Raspberry Pi 4B (4GB)** | Broadcom BCM2711, Quad-core Cortex-A72 (ARM64) | Alpine Linux aarch64, U-Boot / GRUB EFI | **IN PROGRESS**<br>Validating aarch64 device-tree compilation, Image.gz staging, and U-Boot one-shot handoff abstractions. |
| **Huananzhi X99-F8D Plus** | Dual Intel Xeon E5-2680 v4 (Broadwell-EP), RTX 3090 | Fedora 41 Server, Linux 6.12+ | **STAGED FOR MILESTONE 4**<br>Allocated for multi-socket enterprise RPM / kernel-install hook validation. |

### 8.3. Status Classification Standard
- **VERIFIED (Pass):** Full automated reconciliation lifecycle confirmed: compilation, DKMS verification, candidate staging, one-shot reservation, clean boot probation, and fallback rollback.
- **QUALIFIED (Workaround):** Functional with specific hardware configurations documented in Section 7 (e.g., payload chaining, firmware path overrides).
- **EXPERIMENTAL:** Functional in userland; underlying platform firmware exhibits non-deterministic reset or hardware quirks under tracking.
- **IN PROGRESS / STAGED:** Actively being integrated into the automated QEMU matrix and multi-distro test harness.
```
