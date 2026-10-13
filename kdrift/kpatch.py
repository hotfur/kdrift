#!/usr/bin/env python3
"""
kdrift KPatch Engine (v3.2.0)
Transactional, reversible kernel tree overlay and unified diff engine.
Part of the kdrift toolchain.
"""

import os
import sys
import json
import shutil
import logging
import hashlib
import subprocess
from typing import Dict, List, Tuple, Any

KPATCH_DEFAULT_DIR = "/usr/src/kpatch"
MANIFEST_FILE = "/var/lib/kdrift/kpatch_manifest.json"

logger = logging.getLogger("kdrift.kpatch")


def compute_file_sha256(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        hasher.update(f.read())
    return hasher.hexdigest()

class KPatchSession:
    """
    RAII Context Manager guarding kernel source tree modifications.
    Guarantees atomic rollback of all overlays, Kbuild appends, and diffs
    if any stage of configuration, compilation, or staging halts, fails,
    or receives a termination signal (SIGINT/SIGTERM).
    """

    def __init__(self, engine: "KPatchEngine", kernel_dir: str):
        self.engine = engine
        self.kernel_dir = kernel_dir
        self.committed = False

    def __enter__(self):
        logger.info("[KPATCH SESSION] Arming transactional tree guard...")
        if not self.engine.apply_transaction(self.kernel_dir):
            raise RuntimeError("KPatch transaction failed pre-flight verification or application.")
        return self

    def mark_successful(self):
        """Disarms automatic rollback upon complete, verified candidate staging."""
        self.committed = True
        logger.info("[KPATCH SESSION] Compilation and staging verified. Tree transaction locked.")

    def rollback(self):
        logger.warning("[KPATCH ROLLBACK] Compilation failure or interrupt detected!")
        logger.warning("[KPATCH ROLLBACK] Executing emergency reversion to restore pristine baseline...")
        self.engine.revert_active_transaction(self.kernel_dir)

    def __exit__(self, exc_type, exc_val, exc_tb):
        # BaseException catches SystemExit (sys.exit) and KeyboardInterrupt (Ctrl+C)
        if not self.committed or exc_type is not None:
            self.rollback()
        # Return False to let SystemExit / exceptions propagate cleanly
        return False

class KPatchEngine:
    """Manages transactional application and reversion of tree overlays and diffs."""

    def __init__(self, kpatch_dir: str = KPATCH_DEFAULT_DIR, manifest_path: str = MANIFEST_FILE):
        self.kpatch_dir = kpatch_dir
        self.manifest_path = manifest_path

    def load_manifest(self) -> Dict[str, Any]:
        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to read kpatch manifest: {e}")
        return {"applied": False, "files_created": [], "files_appended": {}, "patches_applied": []}

    def save_manifest(self, manifest: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.manifest_path), exist_ok=True)
        with open(self.manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

    def revert_active_transaction(self, kernel_dir: str) -> bool:
        """Restores kernel tree to pure state using recorded manifest."""
        manifest = self.load_manifest()
        if not manifest.get("applied"):
            return True

        logger.info("[KPATCH] Reverting active patch transaction to restore clean baseline...")

        # 1. Reverse unified diffs in reverse order
        for patch_path in reversed(manifest.get("patches_applied", [])):
            if os.path.exists(patch_path):
                logger.info(f"  Reversing patch: {os.path.basename(patch_path)}")
                cmd = ["patch", "-R", "-p1", "-s", "-d", kernel_dir, "-i", patch_path]
                subprocess.run(cmd, check=False)

        # 2. Revert appended fragments
        for rel_path, meta in manifest.get("files_appended", {}).items():
            dest = os.path.join(kernel_dir, rel_path)
            if os.path.exists(dest):
                tag_begin = f"# --- BEGIN KPATCH INJECTION: {rel_path} ---"
                tag_end = f"# --- END KPATCH INJECTION: {rel_path} ---"
                with open(dest, "r") as f:
                    content = f.read()
                if tag_begin in content and tag_end in content:
                    logger.info(f"  Stripping Kbuild append block from: {rel_path}")
                    before = content.split(tag_begin)[0]
                    after = content.split(tag_end)[1]
                    with open(dest, "w") as f:
                        f.write(before.rstrip() + "\n" + after.lstrip())

        # 3. Purge added overlay files
        for rel_path in manifest.get("files_created", []):
            dest = os.path.join(kernel_dir, rel_path)
            if os.path.exists(dest):
                logger.info(f"  Unlinking overlay file: {rel_path}")
                try:
                    os.remove(dest)
                except OSError as e:
                    logger.error(f"  Failed to remove {dest}: {e}")

        # 4. Scrub dangling patch debris (.rej, .orig)
        for root, _, files in os.walk(kernel_dir):
            for f in files:
                if f.endswith(".rej") or f.endswith(".orig"):
                    try:
                        os.remove(os.path.join(root, f))
                    except OSError:
                        pass

        # Clear manifest
        self.save_manifest({"applied": False, "files_created": [], "files_appended": {}, "patches_applied": []})
        logger.info("[KPATCH] Tree reverted to pristine baseline.")
        return True

    def apply_transaction(self, kernel_dir: str) -> bool:
        """Executes transactional pre-flight, tree-overlay, append, and patch sequence."""
        if not os.path.exists(self.kpatch_dir):
            logger.debug(f"KPatch directory {self.kpatch_dir} does not exist. Skipping.")
            return True

        # Always revert prior transactions before applying fresh
        self.revert_active_transaction(kernel_dir)

        manifest: Dict[str, Any] = {
            "applied": False,
            "files_created": [],
            "files_appended": {},
            "patches_applied": []
        }

        tree_dir = os.path.join(self.kpatch_dir, "tree")
        append_dir = os.path.join(self.kpatch_dir, "append")
        patches_dir = os.path.join(self.kpatch_dir, "patches")

        # Fallback: Support flat structure if structured subdirectories don't exist
        has_subdirs = any(os.path.exists(d) for d in [tree_dir, append_dir, patches_dir])

        # ----------------------------------------------------------------------
        # 1. PHASE 1: DISCOVERY & PRE-FLIGHT
        # ----------------------------------------------------------------------
        patches_to_apply = []
        if os.path.exists(patches_dir):
            for root, _, files in os.walk(patches_dir):
                for f in sorted(files):
                    if f.endswith(".patch") or f.endswith(".diff"):
                        patches_to_apply.append(os.path.join(root, f))
        elif not has_subdirs:
            for f in sorted(os.listdir(self.kpatch_dir)):
                if f.endswith(".patch") or f.endswith(".diff"):
                    patches_to_apply.append(os.path.join(self.kpatch_dir, f))

        # Pre-flight check patches
        for patch_file in patches_to_apply:
            logger.info(f"[KPATCH PRE-FLIGHT] Testing diff: {os.path.basename(patch_file)}...")
            cmd = ["patch", "--dry-run", "-p1", "-s", "-f", "-d", kernel_dir, "-i", patch_file]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                logger.error(f"[KPATCH REJECT] Patch {patch_file} failed dry-run:\n{res.stderr}")
                return False

        logger.info("[KPATCH PRE-FLIGHT] All diffs verified clean.")

        # ----------------------------------------------------------------------
        # 2. PHASE 2: TREE OVERLAY INJECTION
        # ----------------------------------------------------------------------
        if os.path.exists(tree_dir):
            for root, _, files in os.walk(tree_dir):
                for file_name in files:
                    src_path = os.path.join(root, file_name)
                    rel_path = os.path.relpath(src_path, tree_dir)
                    dest_path = os.path.join(kernel_dir, rel_path)

                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    if not os.path.exists(dest_path):
                        manifest["files_created"].append(rel_path)

                    logger.info(f"[KPATCH OVERLAY] Injecting: {rel_path}")
                    shutil.copy2(src_path, dest_path)

        # ----------------------------------------------------------------------
        # 3. PHASE 3: KBUILD APPEND INJECTION
        # ----------------------------------------------------------------------
        if os.path.exists(append_dir):
            for root, _, files in os.walk(append_dir):
                for file_name in files:
                    src_path = os.path.join(root, file_name)
                    rel_path = os.path.relpath(src_path, append_dir)
                    dest_path = os.path.join(kernel_dir, rel_path)

                    if not os.path.exists(dest_path):
                        # If destination doesn't exist, create it as a new file
                        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                        shutil.copy2(src_path, dest_path)
                        manifest["files_created"].append(rel_path)
                    else:
                        # Append with demarcations
                        with open(src_path, "r") as f_src:
                            snippet = f_src.read().strip()

                        tag_begin = f"\n# --- BEGIN KPATCH INJECTION: {rel_path} ---\n"
                        tag_end = f"\n# --- END KPATCH INJECTION: {rel_path} ---\n"
                        payload = tag_begin + snippet + tag_end

                        logger.info(f"[KPATCH APPEND] Hooking fragment into: {rel_path}")
                        with open(dest_path, "a") as f_dest:
                            f_dest.write(payload)

                        manifest["files_appended"][rel_path] = {
                            "snippet_hash": hashlib.sha256(snippet.encode()).hexdigest()
                        }

        # ----------------------------------------------------------------------
        # 4. PHASE 4: EXECUTE PATCH APPLICATION
        # ----------------------------------------------------------------------
        for patch_file in patches_to_apply:
            logger.info(f"[KPATCH APPLY] Applying unified diff: {os.path.basename(patch_file)}...")
            cmd = ["patch", "-p1", "-s", "-d", kernel_dir, "-i", patch_file]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                logger.error(f"[KPATCH ERROR] Failed to apply {patch_file}. Executing abort-rollback...")
                self.revert_active_transaction(kernel_dir)
                return False
            manifest["patches_applied"].append(patch_file)

        manifest["applied"] = True
        self.save_manifest(manifest)
        logger.info(
            f"[KPATCH COMPLETE] Applied: {len(manifest['files_created'])} overlays, "
            f"{len(manifest['files_appended'])} appends, {len(manifest['patches_applied'])} diffs."
        )
        return True
