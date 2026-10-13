#!/bin/sh
# ==============================================================================
# kdrift Universal Interactive Root Installer (v3.1.0)
# Supports: Alpine, Arch, Fedora/RHEL, Gentoo, Debian/Ubuntu
# ==============================================================================
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

info()  { printf "${GREEN}[INFO]${NC} %s\n" "$1"; }
warn()  { printf "${YELLOW}[WARN]${NC} %s\n" "$1"; }
error() { printf "${RED}[ERROR]${NC} %s\n" "$1"; exit 1; }
header(){ printf "\n${BLUE}${BOLD}=== %s ===${NC}\n" "$1"; }

# ------------------------------------------------------------------------------
# 1. ROOT & ENVIRONMENT PRE-FLIGHT
# ------------------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    error "install.sh must be executed with root privileges (sudo ./install.sh)."
fi

header "kdrift Systemic Installer v3.1.0"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FILES_DIR="${SCRIPT_DIR}/files"

if [ ! -f "${SCRIPT_DIR}/pyproject.toml" ]; then
    error "Missing pyproject.toml in ${SCRIPT_DIR}. Execute script from repository root."
fi

# ------------------------------------------------------------------------------
# 2. DETERMINISTIC DISTRO & INIT SYSTEM DETECTION
# ------------------------------------------------------------------------------
DISTRO="unknown"
INIT_SYS="unknown"

if command -v apk >/dev/null 2>&1; then
    DISTRO="alpine"
elif command -v pacman >/dev/null 2>&1; then
    DISTRO="arch"
elif command -v dnf >/dev/null 2>&1 || command -v rpm >/dev/null 2>&1; then
    DISTRO="fedora"
elif command -v emerge >/dev/null 2>&1; then
    DISTRO="gentoo"
elif command -v apt-get >/dev/null 2>&1 || command -v dpkg >/dev/null 2>&1; then
    DISTRO="debian"
fi

if [ "$DISTRO" = "unknown" ] && [ -f /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case "$ID" in
        alpine)                             DISTRO="alpine" ;;
        arch|artix|endeavouros|manjaro)     DISTRO="arch" ;;
        fedora|rhel|centos|rocky|almalinux) DISTRO="fedora" ;;
        gentoo)                             DISTRO="gentoo" ;;
        debian|ubuntu|linuxmint|pop)        DISTRO="debian" ;;
        *)                                  DISTRO="unknown" ;;
    esac
fi

if [ -d /run/systemd/system ]; then
    INIT_SYS="systemd"
elif [ -x /sbin/openrc ] || [ -d /etc/init.d ]; then
    INIT_SYS="openrc"
fi

info "Detected Distribution: ${BOLD}${DISTRO}${NC}"
info "Detected Init System:  ${BOLD}${INIT_SYS}${NC}"

printf "Confirm or override distribution [alpine/arch/fedora/gentoo/debian] (${DISTRO}): "
read -r INPUT_DISTRO
if [ -n "$INPUT_DISTRO" ]; then
    DISTRO="$INPUT_DISTRO"
fi

# ------------------------------------------------------------------------------
# 3. PYTHON PACKAGE INSTALLATION & DETERMINISTIC BINARY PROJECTION
# ------------------------------------------------------------------------------
header "Installing kdrift Python Core"

if command -v python3 >/dev/null 2>&1; then
    info "Installing kdrift via pip..."
    python3 -m pip install --upgrade --break-system-packages . 2>/dev/null || \
    python3 -m pip install --upgrade .
else
    error "python3 is required but not installed."
fi

resolve_binary() {
    _bin_name="$1"
    _sys_scripts="$(python3 -c "import sysconfig; print(sysconfig.get_path('scripts'))" 2>/dev/null || true)"
    _user_base="$(python3 -m site --user-base 2>/dev/null || true)"

    for _candidate in \
        "$(command -v "$_bin_name" 2>/dev/null)" \
        "${_sys_scripts}/${_bin_name}" \
        "${_user_base}/bin/${_bin_name}" \
        "/usr/local/bin/${_bin_name}" \
        "/usr/bin/${_bin_name}" \
        "/root/.local/bin/${_bin_name}"; do
        if [ -n "$_candidate" ] && [ -f "$_candidate" ] && [ -x "$_candidate" ]; then
            _real_path="$(readlink -f "$_candidate" 2>/dev/null || echo "$_candidate")"
            if [ -f "$_real_path" ] && [ -x "$_real_path" ]; then
                echo "$_real_path"
                return 0
            fi
        fi
    done
    return 1
}

link_binary_to_targets() {
    _bin_name="$1"
    _source_path="$(resolve_binary "$_bin_name" || true)"

    if [ -z "$_source_path" ]; then
        warn "Could not resolve binary location for ${_bin_name}!"
        return 1
    fi

    info "Discovered ${_bin_name} at: ${_source_path}"

    for _target_dir in "/usr/local/bin" "/usr/bin"; do
        mkdir -p "$_target_dir"
        _dest_file="${_target_dir}/${_bin_name}"

        if [ "$_source_path" != "$_dest_file" ]; then
            ln -sf "$_source_path" "$_dest_file"
            chmod +x "$_dest_file" 2>/dev/null || true
            info "Projected symlink: ${_dest_file} -> ${_source_path}"
        fi
    done
}

link_binary_to_targets "kdrift-assert"
link_binary_to_targets "kdrift"
link_binary_to_targets "kdag"
link_binary_to_targets "kdrift-toolchain"

if ! command -v kdrift >/dev/null 2>&1; then
    error "kdrift executable is not accessible in current PATH."
fi

# ------------------------------------------------------------------------------
# 4. INVARIANTS PROFILE SETUP
# ------------------------------------------------------------------------------
header "Configuring Invariants Profile"

PROFILE_PATH="/etc/kernel/invariants.profile"
printf "Path for invariants.profile [${PROFILE_PATH}]: "
read -r INPUT_PROFILE
if [ -n "$INPUT_PROFILE" ]; then
    PROFILE_PATH="$INPUT_PROFILE"
fi

mkdir -p "$(dirname "$PROFILE_PATH")"

if [ ! -f "$PROFILE_PATH" ]; then
    info "Creating baseline template at ${PROFILE_PATH}..."
    cat <<'EOF' > "$PROFILE_PATH"
[ALWAYS]
CONFIG_IKCONFIG=y
CONFIG_IKCONFIG_PROC=y
CONFIG_CGROUPS=y
CONFIG_NAMESPACES=y
CONFIG_HARDENED_USERCOPY=y
CONFIG_STRICT_KERNEL_RWX=y

[NEVER]
CONFIG_IA32_EMULATION=n
CONFIG_FLOPPY=n
EOF
    info "Baseline profile created."
else
    info "Existing invariants profile found at ${PROFILE_PATH}."
fi

# ------------------------------------------------------------------------------
# 5. COMPILER & TOOLCHAIN MATRIX SETUP
# ------------------------------------------------------------------------------
header "Configuring Compiler & Toolchain Engine"

TOOLCHAIN_CONFIG="/etc/kernel/toolchain.json"

if [ -f "$TOOLCHAIN_CONFIG" ]; then
    info "Existing toolchain configuration identified at ${TOOLCHAIN_CONFIG}."
    printf "Reconfigure compiler toolchain matrix? [y/N]: "
    read -r RECONFIG_TOOLCHAIN
    if [ "$RECONFIG_TOOLCHAIN" = "y" ] || [ "$RECONFIG_TOOLCHAIN" = "Y" ]; then
        python3 -c "import kdrift.toolchain as kt; kt.interactive_configure('${TOOLCHAIN_CONFIG}')"
    fi
else
    info "Launching interactive toolchain selection wizard..."
    python3 -c "import kdrift.toolchain as kt; kt.interactive_configure('${TOOLCHAIN_CONFIG}')"
fi

# ------------------------------------------------------------------------------
# 6. STORAGE RETENTION POLICY CONFIGURATION
# ------------------------------------------------------------------------------
header "Configuring Storage Retention Policy"

DEFAULT_RETENTION="5"
printf "Enter maximum historical kernel rotation copies to retain [1-20] (${DEFAULT_RETENTION}): "
read -r INPUT_RETENTION
INPUT_RETENTION="${INPUT_RETENTION:-$DEFAULT_RETENTION}"

python3 -c "
from kdrift.drift import load_state, save_state
state = load_state()
state['retention_limit'] = int('${INPUT_RETENTION}')
save_state(state)
"
info "Kernel rotation capacity set to ${INPUT_RETENTION} copies (excluding persistent kernels)."

# ------------------------------------------------------------------------------
# 6.5. KPATCH STORAGE SCAFFOLDING & SPECIFICATION INJECTION
# ------------------------------------------------------------------------------
header "Configuring KPatch Invariant Engine"

KPATCH_DIR="/usr/src/kpatch"
mkdir -p "${KPATCH_DIR}/tree" "${KPATCH_DIR}/append" "${KPATCH_DIR}/patches"

if [ ! -f "${KPATCH_DIR}/README.md" ]; then
    info "Provisioning canonical KPatch standard specification at ${KPATCH_DIR}/README.md..."
    cat <<'EOF' > "${KPATCH_DIR}/README.md"
# kpatch: Deterministic Kernel Patch & Overlay Architecture

This directory houses machine-specific patches, C source objects, and Kbuild injections.
All contents are transactionally tested, injected, and tracked by `kdrift reconcile`.

## Directory Topology

### 1. `tree/` (Full Source Injections)
Drop any `.c`, `.h`, or `.S` files matching the relative kernel tree path:
- Example: `tree/drivers/platform/x86/undervolt.c` -> `/usr/src/linux/drivers/platform/x86/undervolt.c`

### 2. `append/` (Non-Destructive Kbuild/Makefile Appends)
Place fragments that must be appended to existing kernel files without clobbering:
- Example: `append/drivers/platform/x86/Makefile`
  Contents:
  ```makefile
  obj-y += undervolt.o
  ```
  `kdrift` automatically encapsulates these with demarcation tags and idempotency checks.

### 3. `patches/` (Unified Diffs)
Standard `git diff` or unified patches evaluated in alphanumeric order:
- Example: `patches/01-gate_af_alg_cap_net_admin.patch`

## CLI Verification
- Force apply: `kdrift patch-apply --kernel-dir /usr/src/linux`
- Revert clean: `kdrift patch-clean --kernel-dir /usr/src/linux`
EOF
fi

# ------------------------------------------------------------------------------
# 7. WORKFLOW SELECTION
# ------------------------------------------------------------------------------
header "Select Deployment Strategy"

echo "1) Package Manager / Kernel Interceptor Hooks (Standard distro updates)"
echo "2) Git Source Repository Monitor (Local /usr/src/linux Git repo sync)"
echo "3) Hybrid (Both Package Manager Hooks & Git Repo Sync)"
printf "Selection [1-3] (3): "
read -r WORKFLOW_CHOICE
WORKFLOW_CHOICE="${WORKFLOW_CHOICE:-3}"

# ------------------------------------------------------------------------------
# 8. FILE DEPLOYMENT ENGINE
# ------------------------------------------------------------------------------
header "Deploying Target Interceptors & Services"

mkdir -p /var/lib/kdrift /var/log

if [ "$INIT_SYS" = "systemd" ]; then
    info "Deploying Systemd Trial Boot Services..."
    cp "${FILES_DIR}/kdrift-shutdown.service" /etc/systemd/system/
    cp "${FILES_DIR}/kdrift-boot-check.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable kdrift-shutdown.service kdrift-boot-check.service
elif [ "$INIT_SYS" = "openrc" ]; then
    info "Deploying OpenRC Trial Boot Services..."
    cp "${FILES_DIR}/kdrift-shutdown" /etc/init.d/kdrift-shutdown
    chmod +x /etc/init.d/kdrift-shutdown
    rc-update add kdrift-shutdown shutdown 2>/dev/null || true

    mkdir -p /etc/local.d
    cp "${FILES_DIR}/kdrift-boot.start" /etc/local.d/kdrift-boot.start
    chmod +x /etc/local.d/kdrift-boot.start
    rc-update add local default 2>/dev/null || true
fi

if [ "$WORKFLOW_CHOICE" = "1" ] || [ "$WORKFLOW_CHOICE" = "3" ]; then
    case "$DISTRO" in
        arch)
            info "Installing Arch Linux pacman hook..."
            mkdir -p /etc/pacman.d/hooks
            cp "${FILES_DIR}/90-kdrift.hook" /etc/pacman.d/hooks/
            ;;
        fedora)
            info "Installing Fedora/RHEL kernel-install hook..."
            mkdir -p /etc/kernel/install.d
            cp "${FILES_DIR}/99-kdrift.install" /etc/kernel/install.d/
            chmod +x /etc/kernel/install.d/99-kdrift.install
            ;;
        debian)
            info "Installing Debian/Ubuntu postinst.d hook..."
            mkdir -p /etc/kernel/postinst.d
            cp "${FILES_DIR}/zz-kdrift" /etc/kernel/postinst.d/
            chmod +x /etc/kernel/postinst.d/zz-kdrift
            ;;
        gentoo)
            info "Configuring Gentoo Portage bashrc hook..."
            mkdir -p /etc/portage/env/sys-kernel/gentoo-sources
            cat <<EOF > /etc/portage/env/sys-kernel/gentoo-sources/kdrift
pre_src_configure() {
    /usr/bin/kdrift reconcile --kernel-dir "\${S}" --profile "${PROFILE_PATH}"
}
EOF
            ;;
        alpine)
            info "Alpine Linux utilizes direct source builds or git hooks."
            ;;
    esac
fi

if [ "$WORKFLOW_CHOICE" = "2" ] || [ "$WORKFLOW_CHOICE" = "3" ]; then
    header "Configuring Git Source Monitor"

    KERNEL_GIT_DIR="/usr/src/linux"
    printf "Path to local Git kernel tree [${KERNEL_GIT_DIR}]: "
    read -r INPUT_GIT_DIR
    if [ -n "$INPUT_GIT_DIR" ]; then KERNEL_GIT_DIR="$INPUT_GIT_DIR"; fi

    MIN_COMMITS="1"
    printf "Minimum commit delta threshold [1]: "
    read -r INPUT_COMMITS
    if [ -n "$INPUT_COMMITS" ]; then MIN_COMMITS="$INPUT_COMMITS"; fi

    MIN_AGE="3600"
    printf "Minimum age throttle in seconds [3600]: "
    read -r INPUT_AGE
    if [ -n "$INPUT_AGE" ]; then MIN_AGE="$INPUT_AGE"; fi

    cat <<EOF > /var/lib/kdrift/git-sync.env
KERNEL_DIR="${KERNEL_GIT_DIR}"
PROFILE="${PROFILE_PATH}"
MIN_COMMITS="${MIN_COMMITS}"
MIN_AGE_SECONDS="${MIN_AGE}"
EOF

    if [ -d "${KERNEL_GIT_DIR}/.git" ]; then
        info "Installing native Git post-merge hook in ${KERNEL_GIT_DIR}..."
        kdrift install-git-hook \
            --kernel-dir "${KERNEL_GIT_DIR}" \
            --profile "${PROFILE_PATH}" \
            --min-commits "${MIN_COMMITS}" \
            --min-age-seconds "${MIN_AGE}" || true
    fi

    if [ "$INIT_SYS" = "systemd" ]; then
        info "Deploying Systemd Git Sync Timer..."
        cp "${FILES_DIR}/kdrift-git-sync.service" /etc/systemd/system/
        cp "${FILES_DIR}/kdrift-git-sync.timer" /etc/systemd/system/
        systemctl daemon-reload
        systemctl enable --now kdrift-git-sync.timer
    elif [ "$INIT_SYS" = "openrc" ]; then
        info "Deploying Cron/Periodic Git Sync Script..."
        if [ -d /etc/periodic/hourly ]; then
            cp "${FILES_DIR}/kdrift-git-sync.cron" /etc/periodic/hourly/kdrift-git-sync
            chmod +x /etc/periodic/hourly/kdrift-git-sync
        elif [ -d /etc/cron.hourly ]; then
            cp "${FILES_DIR}/kdrift-git-sync.cron" /etc/cron.hourly/kdrift-git-sync
            chmod +x /etc/cron.hourly/kdrift-git-sync
        fi
    fi
fi

# Debian/Ubuntu patch
if [ "$DISTRO" = "debian" ]; then
    info "Configuring Debian/Ubuntu GRUB parameters (flattening submenus and enabling saved_entry)..."
    mkdir -p /etc/default/grub.d
    cat <<'EOF' > /etc/default/grub.d/99-kdrift.cfg
# Generated by kdrift installer
GRUB_DISABLE_SUBMENU=y
GRUB_DEFAULT=saved
GRUB_SAVEDEFAULT=false
EOF
    # Regenerate grub to apply flattening immediately
    if command -v update-grub >/dev/null 2>&1; then
        update-grub
    elif command -v grub-mkconfig >/dev/null 2>&1; then
        grub-mkconfig -o /boot/grub/grub.cfg
    fi
fi

# ------------------------------------------------------------------------------
# 9. SELINUX CONTEXT ADJUSTMENT (FEDORA / RHEL)
# ------------------------------------------------------------------------------
if [ "$DISTRO" = "fedora" ] && command -v restorecon >/dev/null 2>&1; then
    header "Applying SELinux Security Contexts"
    semanage fcontext -a -t bin_t "/usr/local/bin/kdrift" 2>/dev/null || true
    semanage fcontext -a -t bin_t "/usr/bin/kdrift" 2>/dev/null || true
    semanage fcontext -a -t bin_t "/usr/local/bin/kdag" 2>/dev/null || true
    semanage fcontext -a -t bin_t "/usr/local/bin/kdrift-*" 2>/dev/null || true
    semanage fcontext -a -t var_lib_t "/var/lib/kdrift(/.*)?" 2>/dev/null || true
    restorecon -Rv /usr/local/bin/kdrift /usr/bin/kdrift /usr/local/bin/kdag /var/lib/kdrift 2>/dev/null || true
fi

# ------------------------------------------------------------------------------
# 10. FINALIZE
# ------------------------------------------------------------------------------
header "Installation Complete"
info "kdrift successfully configured for ${DISTRO} (${INIT_SYS})."
info "Profile location: ${PROFILE_PATH}"
info "Toolchain spec:   ${TOOLCHAIN_CONFIG}"
info "Verified binaries: $(command -v kdrift-assert) | $(command -v kdrift) | $(command -v kdag)"
info "Run 'kdrift list' to inspect initial kernel storage state."
