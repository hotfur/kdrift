#!/usr/bin/env python3
"""
kdrift Toolchain Resolver & Cross-Compilation Matrix Engine (v3.1.0)
Part of the kdrift toolchain.
Provides compiler discovery, capability profiling, make parameter synthesis,
and interactive ISA cross-triple configuration.
"""

import os
import sys
import json
import shutil
import platform
import logging
import subprocess
from typing import Dict, List, Tuple, Optional, Any

TOOLCHAIN_CONFIG_FILE = "/etc/kernel/toolchain.json"
logger = logging.getLogger("kdrift.toolchain")

ARCH_SPEC_MATRIX: Dict[str, Dict[str, Any]] = {
    "x86_64": {
        "kernel_arch": "x86",
        "make_target": "bzImage",
        "binary_relpath": "arch/x86/boot/bzImage",
        "triples": ["x86_64-linux-gnu-", "x86_64-unknown-linux-gnu-"]
    },
    "i386": {
        "kernel_arch": "x86",
        "make_target": "bzImage",
        "binary_relpath": "arch/x86/boot/bzImage",
        "triples": ["i686-linux-gnu-", "i386-linux-gnu-"]
    },
    "aarch64": {
        "kernel_arch": "arm64",
        "make_target": "Image.gz",
        "binary_relpath": "arch/arm64/boot/Image.gz",
        "fallback_relpath": "arch/arm64/boot/Image",
        "triples": ["aarch64-linux-gnu-", "aarch64-unknown-linux-gnu-", "aarch64-none-linux-gnu-"]
    },
    "armv7l": {
        "kernel_arch": "arm",
        "make_target": "zImage",
        "binary_relpath": "arch/arm/boot/zImage",
        "triples": ["arm-linux-gnueabihf-", "arm-linux-gnu-"]
    },
    "riscv64": {
        "kernel_arch": "riscv",
        "make_target": "Image.gz",
        "binary_relpath": "arch/riscv/boot/Image.gz",
        "fallback_relpath": "arch/riscv/boot/Image",
        "triples": ["riscv64-linux-gnu-", "riscv64-unknown-linux-gnu-"]
    },
    "ppc64le": {
        "kernel_arch": "powerpc",
        "make_target": "zImage",
        "binary_relpath": "arch/powerpc/boot/zImage",
        "triples": ["powerpc64le-linux-gnu-"]
    }
}


def normalize_host_arch() -> str:
    """Normalizes platform.machine() to standardized matrix keys."""
    mach = platform.machine().lower()
    if mach in ("x86_64", "amd64"):
        return "x86_64"
    if mach in ("i386", "i686"):
        return "i386"
    if mach in ("aarch64", "arm64"):
        return "aarch64"
    if mach.startswith("arm"):
        return "armv7l"
    if mach.startswith("riscv64"):
        return "riscv64"
    if mach in ("ppc64le", "powerpc64le"):
        return "ppc64le"
    return mach


class ToolchainProbe:
    """Audits local binaries, cross-triples, and compiler flags."""

    @staticmethod
    def query_binary_version(binary_path: str) -> Optional[str]:
        if not binary_path or not shutil.which(binary_path):
            return None
        try:
            res = subprocess.run([binary_path, "--version"], capture_output=True, text=True, timeout=3)
            if res.returncode == 0 and res.stdout:
                return res.stdout.splitlines()[0].strip()
        except Exception:
            pass
        return None

    @staticmethod
    def audit_host_compilers() -> Dict[str, Dict[str, Any]]:
        report: Dict[str, Dict[str, Any]] = {}

        gcc_bin = shutil.which("gcc")
        if gcc_bin:
            report["gcc"] = {
                "available": True,
                "path": gcc_bin,
                "version": ToolchainProbe.query_binary_version(gcc_bin),
                "is_clang": "clang" in (ToolchainProbe.query_binary_version(gcc_bin) or "").lower()
            }
        else:
            report["gcc"] = {"available": False}

        clang_bin = shutil.which("clang")
        if clang_bin:
            lld_bin = shutil.which("ld.lld")
            llvm_ar = shutil.which("llvm-ar")
            llvm_nm = shutil.which("llvm-nm")
            llvm_objcopy = shutil.which("llvm-objcopy")

            has_full_llvm = all([lld_bin, llvm_ar, llvm_nm, llvm_objcopy])
            report["clang"] = {
                "available": True,
                "path": clang_bin,
                "version": ToolchainProbe.query_binary_version(clang_bin),
                "has_lld": bool(lld_bin),
                "has_full_llvm": has_full_llvm
            }
        else:
            report["clang"] = {"available": False}

        report["ccache"] = {
            "available": bool(shutil.which("ccache")),
            "path": shutil.which("ccache")
        }

        return report

    @staticmethod
    def discover_cross_triples(target_arch: str) -> List[str]:
        target_info = ARCH_SPEC_MATRIX.get(target_arch)
        if not target_info:
            return []

        candidates = target_info.get("triples", [])
        return [trip for trip in candidates if shutil.which(f"{trip}gcc")]


class ToolchainEngine:
    """Manages active toolchain selection, fallbacks, and make parameters."""

    def __init__(self, config_path: str = TOOLCHAIN_CONFIG_FILE):
        self.config_path = config_path
        self.config = self._load_or_detect_defaults()

    def _load_or_detect_defaults(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to read {self.config_path}: {e}. Falling back to detection.")

        host_arch = normalize_host_arch()
        probes = ToolchainProbe.audit_host_compilers()

        pref = "gcc"
        if probes.get("clang", {}).get("available") and probes["clang"].get("has_full_llvm"):
            pref = "clang"
        elif probes.get("gcc", {}).get("available"):
            pref = "gcc"

        return {
            "target_arch": host_arch,
            "compiler_flavor": pref,
            "use_llvm_integrated": bool(pref == "clang" and probes.get("clang", {}).get("has_full_llvm")),
            "use_ccache": probes.get("ccache", {}).get("available", False),
            "cross_compile_prefix": "",
            "custom_cflags": "",
            "custom_ldflags": ""
        }

    def save_configuration(self) -> None:
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w") as f:
            json.dump(self.config, f, indent=2)
        logger.info(f"Toolchain configuration serialized to {self.config_path}")


    def resolve_effective_toolchain(self, with_ccache: bool = False) -> Tuple[Dict[str, str], List[str], str]:
        """
        Resolves make environment, make arguments, and target kernel image path.
        
        Args:
            with_ccache: If True, wraps CC with ccache for compilation throughput.
                         If False, provides pristine compiler binaries for Kconfig AST checks.
        """
        host_arch = normalize_host_arch()
        target_arch = self.config.get("target_arch", host_arch)
        is_cross = (target_arch != host_arch) or bool(self.config.get("cross_compile_prefix"))

        arch_spec = ARCH_SPEC_MATRIX.get(target_arch, ARCH_SPEC_MATRIX["x86_64"])
        kernel_arch = arch_spec["kernel_arch"]
        binary_relpath = arch_spec["binary_relpath"]

        preferred_compiler = self.config.get("compiler_flavor", "gcc").lower()
        use_ccache = with_ccache and self.config.get("use_ccache", True) and bool(shutil.which("ccache"))
        cross_prefix = self.config.get("cross_compile_prefix", "")

        if is_cross and not cross_prefix and target_arch != host_arch:
            triples = ToolchainProbe.discover_cross_triples(target_arch)
            if triples:
                cross_prefix = triples[0]
                logger.info(f"[CROSS] Auto-detected toolchain triple: {cross_prefix}")

        actual_compiler = preferred_compiler
        if preferred_compiler == "clang":
            if not shutil.which("clang"):
                logger.warning("Clang/LLVM requested but not found. Falling back to GCC.")
                actual_compiler = "gcc"
        if actual_compiler == "gcc":
            probe_gcc = f"{cross_prefix}gcc" if cross_prefix else "gcc"
            if not shutil.which(probe_gcc):
                if shutil.which("clang"):
                    logger.warning(f"GCC ({probe_gcc}) unavailable. Falling back to Clang.")
                    actual_compiler = "clang"
                else:
                    raise RuntimeError(f"Fatal Toolchain Exhaustion: Neither {probe_gcc} nor clang was located.")

        make_env = os.environ.copy()
        make_args = [f"ARCH={kernel_arch}"]

        if is_cross and cross_prefix:
            make_args.append(f"CROSS_COMPILE={cross_prefix}")

        if actual_compiler == "clang":
            use_llvm_all = self.config.get("use_llvm_integrated", True) and shutil.which("ld.lld")
            raw_cc = "clang"
            cc_exec = f"ccache {raw_cc}" if use_ccache else raw_cc

            make_env["CC"] = cc_exec
            make_env["HOSTCC"] = cc_exec
            make_env["AS"] = raw_cc

            if use_llvm_all:
                make_args.extend(["LLVM=1", "LLVM_IAS=1"])
                make_env["LLVM"] = "1"
                make_env["LLVM_IAS"] = "1"
                make_env["LD"] = "ld.lld"
                make_env["NM"] = "llvm-nm"
                make_env["OBJCOPY"] = "llvm-objcopy"
                make_env["OBJDUMP"] = "llvm-objdump"
                make_env["READELF"] = "llvm-readelf"
                make_env["STRIP"] = "llvm-strip"
            else:
                make_env["LD"] = f"{cross_prefix}ld" if cross_prefix else "ld"
                make_env["NM"] = f"{cross_prefix}nm" if cross_prefix else "nm"
                make_env["OBJCOPY"] = f"{cross_prefix}objcopy" if cross_prefix else "objcopy"
                make_env["OBJDUMP"] = f"{cross_prefix}objdump" if cross_prefix else "objdump"
                make_env["READELF"] = f"{cross_prefix}readelf" if cross_prefix else "readelf"
                make_env["STRIP"] = f"{cross_prefix}strip" if cross_prefix else "strip"

            make_args.append(f"CC={cc_exec}")
            make_args.append(f"HOSTCC={cc_exec}")
            make_args.append(f"AS={raw_cc}")
            make_args.append(f"LD={make_env['LD']}")
        else:
            raw_gcc = f"{cross_prefix}gcc" if cross_prefix else "gcc"
            gcc_exec = f"ccache {raw_gcc}" if use_ccache else raw_gcc

            make_env["CC"] = gcc_exec
            make_env["HOSTCC"] = "gcc"
            make_env["AS"] = f"{cross_prefix}as" if cross_prefix else "as"
            make_env["LD"] = f"{cross_prefix}ld" if cross_prefix else "ld"
            make_env["NM"] = f"{cross_prefix}nm" if cross_prefix else "nm"
            make_env["OBJCOPY"] = f"{cross_prefix}objcopy" if cross_prefix else "objcopy"
            make_env["OBJDUMP"] = f"{cross_prefix}objdump" if cross_prefix else "objdump"
            make_env["READELF"] = f"{cross_prefix}readelf" if cross_prefix else "readelf"
            make_env["STRIP"] = f"{cross_prefix}strip" if cross_prefix else "strip"

            make_args.append(f"CC={gcc_exec}")
            make_args.append(f"AS={make_env['AS']}")
            make_args.append(f"LD={make_env['LD']}")

        if self.config.get("custom_cflags"):
            make_env["KCFLAGS"] = self.config["custom_cflags"]
        if self.config.get("custom_ldflags"):
            make_env["KBUILD_LDFLAGS"] = self.config["custom_ldflags"]

        return make_env, make_args, binary_relpath


def interactive_configure(target_profile_path: str = TOOLCHAIN_CONFIG_FILE):
    """Interactive CLI wizard called by install.sh."""
    print("\n=======================================================")
    print("kdrift Toolchain Configuration Wizard")
    print("=======================================================")

    host_arch = normalize_host_arch()
    probes = ToolchainProbe.audit_host_compilers()

    print(f"Detected Host Architecture: {host_arch}")
    print("Auditing compiler ecosystems...")
    for comp, data in probes.items():
        if data.get("available"):
            v = data.get("version", "Active")
            print(f"  [+] {comp.upper():<6}: Available ({v})")
        else:
            print(f"  [-] {comp.upper():<6}: NOT FOUND")

    print(f"\nSupported Target Architectures: {list(ARCH_SPEC_MATRIX.keys())}")
    target_arch = input(f"Target Kernel Architecture [{host_arch}]: ").strip()
    if not target_arch or target_arch not in ARCH_SPEC_MATRIX:
        target_arch = host_arch

    cross_prefix = ""
    if target_arch != host_arch:
        triples = ToolchainProbe.discover_cross_triples(target_arch)
        default_triple = triples[0] if triples else f"{target_arch}-linux-gnu-"
        cross_prefix = input(f"Cross-compilation Tuple Prefix [{default_triple}]: ").strip()
        if not cross_prefix:
            cross_prefix = default_triple

    default_flavor = "gcc"
    if probes["clang"].get("available") and probes["clang"].get("has_full_llvm"):
        default_flavor = "clang"
    elif probes["gcc"].get("available"):
        default_flavor = "gcc"

    print("\nCompiler Engines:")
    print("  1) GCC (Standard GNU Toolchain)")
    print("  2) Clang/LLVM (Full LLVM=1 Suite: Clang, LLD, llvm-objcopy)")
    print("  3) Clang/GNU (Clang frontend + GNU binutils/assembler)")
    choice = input(f"Select compiler flavor [1-3] (Default: {default_flavor}): ").strip()

    llvm_integrated = False
    if choice == "2":
        flavor = "clang"
        llvm_integrated = True
    elif choice == "3":
        flavor = "clang"
        llvm_integrated = False
    elif choice == "1":
        flavor = "gcc"
    else:
        flavor = default_flavor
        llvm_integrated = bool(flavor == "clang" and probes["clang"].get("has_full_llvm"))

    use_ccache = probes["ccache"].get("available", False)
    cc_input = input(f"Enable ccache compiler acceleration? [Y/n] ({'y' if use_ccache else 'n'}): ").strip().lower()
    if cc_input in ("n", "no"):
        use_ccache = False
    elif cc_input in ("y", "yes"):
        use_ccache = True

    config_data = {
        "target_arch": target_arch,
        "compiler_flavor": flavor,
        "use_llvm_integrated": llvm_integrated,
        "use_ccache": use_ccache,
        "cross_compile_prefix": cross_prefix,
        "custom_cflags": "",
        "custom_ldflags": ""
    }

    os.makedirs(os.path.dirname(target_profile_path), exist_ok=True)
    with open(target_profile_path, "w") as f:
        json.dump(config_data, f, indent=2)

    print(f"\n[+] Toolchain policy locked to {target_profile_path}")


if __name__ == "__main__":
    interactive_configure()
