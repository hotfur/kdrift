#!/usr/bin/env python3
"""
kdag.py - Linux Kernel Kconfig DAG Structural Analysis & Profile Synthesis Engine
Version: 3.1.0 (Compiler-Aware AST Resolution, Transitive Selections & Defconfig Pruning)
Part of the kdrift toolchain.
"""

import sys
import os
import re
import argparse
import platform
from collections import deque, defaultdict
from typing import Dict, List, Set, Tuple, Any

from kdrift.toolchain import ToolchainEngine, ARCH_SPEC_MATRIX, normalize_host_arch

FAKE_ROOT = "__ROOT__"

def setup_kconfig_environment(kernel_dir: str, arch: str = ""):
    """Binds clean toolchain registers (NO ccache wrapper) into os.environ for Kconfig macros."""
    engine = ToolchainEngine()
    target_arch = arch or engine.config.get("target_arch", normalize_host_arch())
    kernel_arch = ARCH_SPEC_MATRIX.get(target_arch, {}).get("kernel_arch", "x86")

    # Ingress pristine compiler binaries without ccache wrapper tokens
    make_env, make_args, _ = engine.resolve_effective_toolchain(with_ccache=False)

    os.environ["srctree"] = kernel_dir
    os.environ["ARCH"] = kernel_arch
    os.environ["SRCARCH"] = kernel_arch
    os.environ.setdefault("KERNELVERSION", platform.release())

    for k, v in make_env.items():
        os.environ[k] = v

    for arg in make_args:
        if "=" in arg:
            k, v = arg.split("=", 1)
            os.environ[k] = v

    os.environ.setdefault("CC", "gcc")
    os.environ.setdefault("HOSTCC", os.environ["CC"])
    os.environ.setdefault("LD", "ld")


def patch_kconfiglib():
    """
    Patches kconfiglib AST evaluator:
    1. Intercepts empty macro returns (e.g. $(as-version)) and yields "0".
    2. Implements full Context Manager & Stream Protocols for file interception.
    3. Filters out transitional syntax tokens on modern Kconfig grammars.
    """
    import kconfiglib

    original_error_if = getattr(kconfiglib, "_error_if_fn", None)
    def _safe_error_if(kconf, *args):
        return ""
    if original_error_if:
        kconfiglib._error_if_fn = _safe_error_if

    # Intercept empty macro returns
    original_expand_name = kconfiglib.Kconfig._expand_name
    def _safe_expand_name(kself, s, i):
        try:
            return original_expand_name(kself, s, i)
        except kconfiglib.KconfigError as e:
            if "macro expanded to blank string" in str(e):
                return "0", s, len(s)
            raise e
    kconfiglib.Kconfig._expand_name = _safe_expand_name

    # Hardened Stream & Context Manager Proxy
    _orig_open = kconfiglib.Kconfig._open
    def _patched_open(kself: Any, filename: str, mode: str, *args: Any, **kwargs: Any) -> Any:
        f = _orig_open(kself, filename, mode, *args, **kwargs)

        class KconfigLineInterceptor:
            def __init__(self, original_file: Any):
                self._file = original_file

            # Context Manager Protocol (Bypasses __getattr__ in Python C-API)
            def __enter__(self):
                self._file.__enter__()
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return self._file.__exit__(exc_type, exc_val, exc_tb)

            # Iterator Protocol
            def __iter__(self):
                return self

            def __next__(self) -> str:
                line = self.readline()
                if not line:
                    raise StopIteration
                return line

            def readline(self, *readline_args: Any, **readline_kwargs: Any) -> str:
                line = self._file.readline(*readline_args, **readline_kwargs)
                if line:
                    if line.strip() == "modules":
                        return "# modules\n"
                    if re.match(r'^\s*transitional\b', line):
                        return re.sub(r'\btransitional\b', r'# transitional', line)
                    line = re.sub(r'\b(bool|tristate)\s+transitional\b', r'\1', line)
                return line

            def read(self, *read_args: Any, **read_kwargs: Any) -> str:
                content = self._file.read(*read_args, **read_kwargs)
                if content:
                    lines = content.splitlines(keepends=True)
                    sanitized = []
                    for l in lines:
                        if l.strip() == "modules":
                            sanitized.append("# modules\n")
                        elif re.match(r'^\s*transitional\b', l):
                            sanitized.append(re.sub(r'\btransitional\b', r'# transitional', l))
                        else:
                            sanitized.append(re.sub(r'\b(bool|tristate)\s+transitional\b', r'\1', l))
                    return "".join(sanitized)
                return content

            def __getattr__(self, name: str) -> Any:
                return getattr(self._file, name)

        return KconfigLineInterceptor(f)

    kconfiglib.Kconfig._open = _patched_open

def extract_symbols_with_polarity(expr, current_polarity: int = 1) -> Set[Tuple[str, int]]:
    import kconfiglib
    results = set()
    if not expr:
        return results

    if isinstance(expr, kconfiglib.Symbol) and expr.name:
        results.add((expr.name, current_polarity))
        return results

    if isinstance(expr, tuple):
        op = expr[0]
        if op == kconfiglib.NOT:
            results.update(extract_symbols_with_polarity(expr[1], -current_polarity))
        elif op in (kconfiglib.AND, kconfiglib.OR):
            results.update(extract_symbols_with_polarity(expr[1], current_polarity))
            results.update(extract_symbols_with_polarity(expr[2], current_polarity))
        elif op in (kconfiglib.EQUAL, kconfiglib.UNEQUAL):
            results.update(extract_symbols_with_polarity(expr[1], current_polarity))
            results.update(extract_symbols_with_polarity(expr[2], current_polarity))

    return results


def build_dual_class_visibility_dag(kernel_dir: str, arch: str = "") -> Tuple[
    Dict[str, Set[str]],
    Dict[str, Set[str]],
    Dict[Tuple[str, str], int],
    Set[str],
    Set[str],
    Any
]:
    import kconfiglib
    setup_kconfig_environment(kernel_dir, arch)
    patch_kconfiglib()

    kconfig_path = os.path.join(kernel_dir, "Kconfig")
    if not os.path.exists(kconfig_path):
        raise FileNotFoundError(f"Root Kconfig not found at: {kconfig_path}")

    print(f"[*] Parsing Linux kernel Kconfig tree (ARCH={os.environ.get('ARCH')}, CC={os.environ.get('CC')}) ...")
    kconf = kconfiglib.Kconfig(kconfig_path, warn=False)

    selectable_nodes = set()
    hidden_nodes = set()

    for name, sym in kconf.syms.items():
        if sym.type in (kconfiglib.BOOL, kconfiglib.TRISTATE):
            has_prompt = any(node.prompt is not None for node in sym.nodes)
            if has_prompt:
                selectable_nodes.add(name)
            else:
                hidden_nodes.add(name)

    all_tracked = selectable_nodes.union(hidden_nodes)
    print(f"[+] Total symbols parsed: {len(kconf.syms)}")
    print(f"[+] User-Selectable symbols (V_sel): {len(selectable_nodes)}")
    print(f"[+] Hidden State symbols (V_hid):    {len(hidden_nodes)}")

    adj = defaultdict(set)
    rev_adj = defaultdict(set)
    edge_polarity = {}

    for name in all_tracked:
        sym = kconf.syms[name]
        dep_tuples = set()

        if sym.direct_dep:
            dep_tuples.update(extract_symbols_with_polarity(sym.direct_dep, 1))

        for node in sym.nodes:
            if node.prompt and node.prompt[1]:
                dep_tuples.update(extract_symbols_with_polarity(node.prompt[1], 1))
            if node.dep:
                dep_tuples.update(extract_symbols_with_polarity(node.dep, 1))

        valid_deps = {(dep, pol) for (dep, pol) in dep_tuples if dep in all_tracked and dep != name}

        if valid_deps:
            for dep, pol in valid_deps:
                adj[dep].add(name)
                rev_adj[name].add(dep)
                edge_polarity[(dep, name)] = pol
        else:
            adj[FAKE_ROOT].add(name)
            rev_adj[name].add(FAKE_ROOT)
            edge_polarity[(FAKE_ROOT, name)] = 1

    return adj, rev_adj, edge_polarity, selectable_nodes, hidden_nodes, kconf


def task1_most_connected(adj: Dict[str, Set[str]], rev_adj: Dict[str, Set[str]], all_nodes: Set[str]) -> Tuple[List[str], int]:
    degrees = {}
    for node in all_nodes:
        if node == FAKE_ROOT:
            continue
        degrees[node] = len(adj[node]) + len(rev_adj[node])

    counts = {k: 0 for k in range(1, 51)}
    for node, deg in degrees.items():
        if deg <= 50:
            counts[deg] += 1

    max_rev_delta = -1
    inflection_k = 50

    for k in range(50, 3, -1):
        rev_delta = counts[k - 1] - counts[k]
        if rev_delta > max_rev_delta:
            max_rev_delta = rev_delta
            inflection_k = k

    print(f"[+] Structural Degree Inflection Boundary: Degree >= {inflection_k}")
    hub_vertices = [node for node, deg in degrees.items() if deg >= inflection_k]
    return hub_vertices, inflection_k


def task2_3_topological_levels(adj: Dict[str, Set[str]], rev_adj: Dict[str, Set[str]], all_nodes: Set[str]) -> Tuple[List[str], int]:
    in_degree = {n: len(rev_adj[n]) for n in all_nodes}
    queue = deque([n for n in all_nodes if in_degree[n] == 0])
    topo_order = []

    while queue:
        u = queue.popleft()
        topo_order.append(u)
        for v in adj[u]:
            in_degree[v] -= 1
            if in_degree[v] == 0:
                queue.append(v)

    depth = {n: 0 for n in all_nodes}
    for u in topo_order:
        for v in adj[u]:
            if depth[v] < depth[u] + 1:
                depth[v] = depth[u] + 1

    max_level = max(depth.values()) if depth else 0
    nodes_by_level = defaultdict(list)
    for n, lvl in depth.items():
        if n != FAKE_ROOT:
            nodes_by_level[lvl].append(n)

    levels = list(range(1, max_level + 1))
    level_counts = [len(nodes_by_level[lvl]) for lvl in levels]

    dn_dx = []
    for i in range(len(level_counts)):
        if i == 0:
            dn_dx.append(level_counts[0])
        else:
            dn_dx.append(level_counts[i] - level_counts[i - 1])

    max_idx = dn_dx.index(max(dn_dx)) if dn_dx else 0
    x_0 = levels[max_idx] if levels else 1
    true_level = max(1, x_0 - 1)

    vertices_behind = []
    for lvl in range(1, true_level + 1):
        vertices_behind.extend(nodes_by_level[lvl])

    print(f"[+] Calibrated Strata Boundary: Level <= {true_level} (Isolated {len(vertices_behind)} nodes)")
    return vertices_behind, true_level


def resolve_hidden_state_dependencies(
    raw_candidates: Set[str],
    selectable_nodes: Set[str],
    hidden_nodes: Set[str],
    rev_adj: Dict[str, Set[str]]
) -> Set[str]:
    promoted = set()
    for node in raw_candidates:
        if node in selectable_nodes:
            promoted.add(node)
        elif node in hidden_nodes:
            queue = deque([node])
            visited = {node}
            while queue:
                curr = queue.popleft()
                for parent in rev_adj.get(curr, set()):
                    if parent == FAKE_ROOT or parent in visited:
                        continue
                    visited.add(parent)
                    if parent in selectable_nodes:
                        promoted.add(parent)
                    else:
                        queue.append(parent)
    return promoted


def parse_dot_config(config_path: str) -> Dict[str, str]:
    state = {}
    if not os.path.exists(config_path):
        return state
    with open(config_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("# ") and " is not set" in line:
                sym = line.split(" ")[1]
                state[sym] = "n"
            elif "=" in line and not line.startswith("#"):
                parts = line.split("=", 1)
                state[parts[0]] = parts[1]
    return state


def build_active_selection_graph(kconf: Any, config_vals: Dict[str, str]) -> Dict[str, Set[str]]:
    import kconfiglib
    active_select_adj = defaultdict(set)
    for name, sym in kconf.syms.items():
        v = config_vals.get(name, config_vals.get(f"CONFIG_{name}", "n"))
        if v not in ("y", "m"):
            continue
        for target_sym, cond in sym.selects:
            if not target_sym.name:
                continue
            if kconfiglib.expr_value(cond) > 0:
                active_select_adj[name].add(target_sym.name)
    return active_select_adj


def prune_symmetric_invariants_ast(
    critical_syms: Set[str],
    config_vals: Dict[str, str],
    kconf: Any
) -> Tuple[Dict[str, str], List[str]]:
    import kconfiglib

    for name, sym in kconf.syms.items():
        v = config_vals.get(name, config_vals.get(f"CONFIG_{name}", "n"))
        sym.set_value(v)

    assigned_states = {}
    for sym in critical_syms:
        bare_name = sym[7:] if sym.startswith("CONFIG_") else sym
        val = config_vals.get(bare_name, config_vals.get(f"CONFIG_{bare_name}", "n"))
        assigned_states[bare_name] = val if val in ("y", "m") else "n"

    always_candidates = {s: v for s, v in assigned_states.items() if v in ("y", "m")}
    never_candidates = [s for s, v in assigned_states.items() if v == "n"]

    retained_never = []
    for sym in never_candidates:
        kconf_sym = kconf.syms.get(sym)
        if not kconf_sym:
            retained_never.append(sym)
            continue
        is_visible = any(kconfiglib.expr_value(n.prompt[1]) > 0 for n in kconf_sym.nodes if n.prompt)
        direct_dep_val = kconfiglib.expr_value(kconf_sym.direct_dep)
        if direct_dep_val == 0 or not is_visible:
            continue
        retained_never.append(sym)

    active_select_adj = build_active_selection_graph(kconf, config_vals)
    dominated_always = set()
    for root_cand in always_candidates:
        queue = deque([root_cand])
        visited = {root_cand}
        while queue:
            curr = queue.popleft()
            for child in active_select_adj.get(curr, set()):
                if child not in visited:
                    visited.add(child)
                    queue.append(child)
                    if child in always_candidates:
                        dominated_always.add(child)

    retained_always = {s: v for s, v in always_candidates.items() if s not in dominated_always}
    return retained_always, retained_never


def locate_defconfig(kernel_dir: str, arch: str, user_defconfig: str = "") -> str:
    if user_defconfig:
        if os.path.exists(user_defconfig):
            return user_defconfig
        arch_path = os.path.join(kernel_dir, "arch", arch, "configs", user_defconfig)
        if os.path.exists(arch_path):
            return arch_path

    search_paths = []
    if arch in ("x86", "x86_64"):
        search_paths.append(os.path.join(kernel_dir, "arch/x86/configs/x86_64_defconfig"))
        search_paths.append(os.path.join(kernel_dir, "arch/x86/configs/i386_defconfig"))
    search_paths.append(os.path.join(kernel_dir, f"arch/{arch}/configs/defconfig"))
    search_paths.append(os.path.join(kernel_dir, f"arch/{arch}/configs/{arch}_defconfig"))

    for p in search_paths:
        if os.path.exists(p):
            return p
    return ""


def resolve_defconfig_values(kernel_dir: str, defconfig_path: str, arch: str) -> Dict[str, str]:
    import kconfiglib
    try:
        setup_kconfig_environment(kernel_dir, arch)
        patch_kconfiglib()
        kconf_def = kconfiglib.Kconfig(os.path.join(kernel_dir, "Kconfig"), warn=False)
        kconf_def.load_config(defconfig_path)
        return {name: sym.str_value for name, sym in kconf_def.syms.items() if sym.str_value}
    except Exception as e:
        print(f"[!] Warning: AST defconfig parsing failed ({e}). Falling back to raw parser.")
        raw_map = parse_dot_config(defconfig_path)
        return {(k[7:] if k.startswith("CONFIG_") else k): v for k, v in raw_map.items()}


def prune_defconfig_delta(
    always: Dict[str, str],
    never: List[str],
    def_vals: Dict[str, str],
    keep_always: bool = False
) -> Tuple[Dict[str, str], List[str]]:
    final_never = []
    for sym in never:
        def_val = def_vals.get(sym, "n")
        if def_val != "n":
            final_never.append(sym)

    final_always = {}
    if keep_always:
        final_always = dict(always)
    else:
        for sym, target_val in always.items():
            def_val = def_vals.get(sym, "n")
            if def_val == "y" and target_val == "y":
                continue
            final_always[sym] = target_val

    return final_always, final_never


def write_edge_list(adj: Dict[str, Set[str]], edge_polarity: Dict[Tuple[str, str], int], output_path: str):
    with open(output_path, "w") as f:
        for parent, children in adj.items():
            for child in children:
                pol = edge_polarity.get((parent, child), 1)
                f.write(f"{parent} -> {child} {'(+)' if pol == 1 else '(-)'}\n")


def main():
    parser = argparse.ArgumentParser(description="kdag: Linux Kernel DAG Structural Analyzer & Invariant Compiler")
    parser.add_argument("--kernel-dir", required=True, help="Path to Linux kernel source tree")
    parser.add_argument("--config", required=True, help="Path to baseline target .config")
    parser.add_argument("--defconfig", default="", help="Path or name of architecture defconfig baseline")
    parser.add_argument("--keep-always", action="store_true", help="Bypass defconfig delta pruning for ALWAYS entries")
    parser.add_argument("--edge-list", default="/tmp/kernel_dag.edges", help="Output path for DAG edge list")
    parser.add_argument("--out-symbols", default="/tmp/critical_symbols.txt", help="Output path for critical symbols")
    parser.add_argument("--out-profile", default="invariants.profile", help="Output path for resolved profile")
    parser.add_argument("--arch", default="", help="Target architecture (default: toolchain/host arch)")
    parser.add_argument("--all-symbols", "--skip-all-filters", dest="all_symbols", action="store_true", help="Bypass all graph heuristic filters; evaluate 100%% of symbols in Kconfig tree as invariant candidates")
    parser.add_argument("--no-degree-filter", "--skip-degree-filter", dest="no_degree_filter", action="store_true", help="Skip the Task 1 degree hub filter (bypasses deg >= k inflection threshold)")
    parser.add_argument("--no-level-filter", "--skip-level-filter", dest="no_level_filter", action="store_true", help="Skip the Task 2/3 topological strata filter (bypasses lvl <= x_0 threshold)")
    parser.add_argument("--min-degree", type=int, default=None, help="Manual override for degree threshold k (default: auto-calculated inflection boundary)")
    parser.add_argument("--max-level", type=int, default=None, help="Manual override for strata level threshold x_0 (default: auto-calculated finite difference boundary)")

    args = parser.parse_args()

    engine = ToolchainEngine()
    arch = args.arch or engine.config.get("target_arch", normalize_host_arch())
    kernel_arch = ARCH_SPEC_MATRIX.get(arch, {}).get("kernel_arch", "x86")

    adj, rev_adj, edge_polarity, selectable_nodes, hidden_nodes, kconf = build_dual_class_visibility_dag(args.kernel_dir, kernel_arch)

    all_nodes = selectable_nodes.union(hidden_nodes).union({FAKE_ROOT})
    write_edge_list(adj, edge_polarity, args.edge_list)

    # ----------------------------------------------------------------------
    # GRAPH FILTER EVALUATION & HEURISTIC BYPASS ROUTING
    # ----------------------------------------------------------------------
    if args.all_symbols or (args.no_degree_filter and args.no_level_filter):
        print("\n[*] [FILTER BYPASS] Bypassing all structural filters (deg >= k and lvl <= x_0).")
        print(f"[*] Admitting all {len(all_nodes) - 1} Kconfig symbols directly to AST evaluation pool.")
        raw_critical = {n for n in all_nodes if n != FAKE_ROOT}

    else:
        hub_nodes = set()
        level_nodes = set()

        # 1. Degree Hub Filter (Task 1)
        if not args.no_degree_filter:
            if args.min_degree is not None:
                degrees = {n: len(adj[n]) + len(rev_adj[n]) for n in all_nodes if n != FAKE_ROOT}
                hubs = [n for n, deg in degrees.items() if deg >= args.min_degree]
                print(f"[+] Degree Hub Filter: Manual override (Degree >= {args.min_degree}, {len(hubs)} nodes)")
            else:
                hubs, _ = task1_most_connected(adj, rev_adj, all_nodes)
            hub_nodes = set(hubs)
        else:
            print("[*] [TOGGLE] Skipping degree hub filter (Task 1: deg >= k bypassed).")

        # 2. Topological Strata Filter (Task 2/3)
        if not args.no_level_filter:
            if args.max_level is not None:
                # Custom level override
                in_degree = {n: len(rev_adj[n]) for n in all_nodes}
                queue = deque([n for n in all_nodes if in_degree[n] == 0])
                topo_order = []
                while queue:
                    u = queue.popleft()
                    topo_order.append(u)
                    for v in adj[u]:
                        in_degree[v] -= 1
                        if in_degree[v] == 0:
                            queue.append(v)
                depth = {n: 0 for n in all_nodes}
                for u in topo_order:
                    for v in adj[u]:
                        if depth[v] < depth[u] + 1:
                            depth[v] = depth[u] + 1
                levels = [n for n, lvl in depth.items() if n != FAKE_ROOT and lvl <= args.max_level]
                print(f"[+] Strata Level Filter: Manual override (Level <= {args.max_level}, {len(levels)} nodes)")
            else:
                levels, _ = task2_3_topological_levels(adj, rev_adj, all_nodes)
            level_nodes = set(levels)
        else:
            print("[*] [TOGGLE] Skipping topological strata filter (Task 2/3: lvl <= x_0 bypassed).")

        raw_critical = hub_nodes.union(level_nodes)
        print(f"[+] Combined Heuristic Candidate Pool: {len(raw_critical)} symbols.")

    # ----------------------------------------------------------------------
    # DEPENDENCY PROMOTION & AST PROFILE COMPILATION
    # ----------------------------------------------------------------------
    critical_selectable = resolve_hidden_state_dependencies(raw_critical, selectable_nodes, hidden_nodes, rev_adj)

    with open(args.out_symbols, "w") as f:
        for sym in sorted(critical_selectable):
            f.write(f"{sym}\n")

    config_vals = parse_dot_config(args.config)
    always, never = prune_symmetric_invariants_ast(critical_selectable, config_vals, kconf)

    defconfig_path = locate_defconfig(args.kernel_dir, kernel_arch, args.defconfig)
    if defconfig_path and os.path.exists(defconfig_path):
        def_vals = resolve_defconfig_values(args.kernel_dir, defconfig_path, kernel_arch)
        always, never = prune_defconfig_delta(always, never, def_vals, keep_always=args.keep_always)

    with open(args.out_profile, "w") as f:
        f.write("# Generated by kdag: Compiler-Aware Invariant Compiler (v3.1.0)\n\n[ALWAYS]\n")
        for sym in sorted(always.keys()):
            f.write(f"CONFIG_{sym}={always[sym]}\n")
        f.write("\n[NEVER]\n")
        for sym in sorted(never):
            f.write(f"CONFIG_{sym}=n\n")

    print(f"\n[+] Invariant profile generated: {args.out_profile} (ALWAYS: {len(always)}, NEVER: {len(never)})")

if __name__ == "__main__":
    main()
