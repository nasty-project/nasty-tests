#!/usr/bin/env python3
"""
NASty Integration Test Runner

Usage:
  sudo python3 run_tests.py --host 10.10.10.46
  sudo python3 run_tests.py --host 10.10.10.46 --pool tank --skip-nvmeof
  sudo python3 run_tests.py --host 10.10.10.46 --skip-delete
  sudo python3 run_tests.py --host 10.10.10.46 --delete-only
"""

import argparse
import asyncio
import os
import re
import sys

from nasty.client import NastyClient
from nasty.context import TestContext
from nasty.output import GREEN, RED, BOLD, RESET, info, ok, fail, warn, header
from nasty.shell import cmd_exists, run

from test_nfs import test_nfs
from test_smb import test_smb
from test_iscsi import test_iscsi
from test_nvmeof import test_nvmeof
from test_subvolume import test_subvolume
from test_snapshots import test_snapshots
from test_storage import test_storage
from test_multiprotocol import test_multiprotocol
from test_cleanup import delete_leftovers


EXPECTED_CHECKS = {
    "Subvolume": 14,
    "Snapshots": 5,
    "Storage": 7,
    "NFS": 65,
    "SMB": 65,
    "Multi-protocol": 9,
    "iSCSI": 95,
    "NVMe-oF": 110,
}

SKIP_DELETE_EXPECTED_CHECKS = {
    "Subvolume": 12,
    "Snapshots": 5,
    "Storage": 7,
    "NFS": 55,
    "SMB": 55,
    "Multi-protocol": 9,
    "iSCSI": 85,
    "NVMe-oF": 100,
}

REMOUNT_EXPECTED_CHECKS = {
    "NFS": 10,
    "SMB": 10,
    "iSCSI": 25,
    "NVMe-oF": 20,
}


def valid_tag(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", value):
        raise argparse.ArgumentTypeError(
            "tag must be 1-32 ASCII letters, digits, underscores, or hyphens")
    return value


async def run_suite(ctx: TestContext, name: str, test_fn, expected: int):
    """Run one suite and enforce its declared result contract."""
    before = len(ctx.results)
    try:
        await test_fn(ctx)
    except Exception as e:
        ctx.record(f"{name}: unhandled exception", False, str(e))
    actual = len(ctx.results) - before
    if actual != expected:
        ctx.record(
            f"{name}: result contract",
            False,
            f"expected {expected} checks, recorded {actual}",
        )


async def audit_cleanup(ctx: TestContext, audit_server: bool = True):
    """Fail if this run's tag remains on the appliance or Linux client."""
    leftovers = []

    def tagged(value) -> bool:
        if isinstance(value, str):
            return value == ctx.tag or value.endswith(f"-{ctx.tag}")
        if isinstance(value, list):
            return any(tagged(item) for item in value)
        if isinstance(value, dict):
            return any(tagged(item) for item in value.values())
        return False

    if audit_server:
        subvolumes = await ctx.client.call("subvolume.list", {"filesystem": ctx.pool})
        leftovers.extend(
            f"subvolume:{sv.get('name')}"
            for sv in subvolumes
            if tagged(sv.get("name", ""))
        )

        snapshots = await ctx.client.call("snapshot.list", {"filesystem": ctx.pool})
        leftovers.extend(
            f"snapshot:{snap.get('subvolume')}/{snap.get('name')}"
            for snap in snapshots
            if tagged(snap.get("name", "")) or tagged(snap.get("subvolume", ""))
        )

        for label, method in [
            ("nfs", "share.nfs.list"),
            ("smb", "share.smb.list"),
            ("iscsi", "share.iscsi.list"),
            ("nvmeof", "share.nvmeof.list"),
        ]:
            shares = await ctx.client.call(method)
            leftovers.extend(
                f"{label}-share:{share.get('id')}"
                for share in shares
                if tagged(share)
            )

    commands = [("mount", ["mount"])]
    if cmd_exists("iscsiadm"):
        commands.append(("iscsi-session", ["iscsiadm", "-m", "session"]))
    if cmd_exists("nvme"):
        commands.append(("nvme-connection", ["nvme", "list-subsys", "-o", "json"]))
    tag_pattern = re.compile(rf"-{re.escape(ctx.tag)}(?:[\"\s]|$)")
    for label, command in commands:
        result = run(command, check=False)
        if tag_pattern.search(result.stdout):
            leftovers.append(label)

    if leftovers:
        ctx.record("cleanup audit", False, ", ".join(leftovers))
    else:
        scope = "resources or client sessions" if audit_server else "client sessions"
        ok(f"Cleanup audit: no {scope} tagged {ctx.tag}")


async def test_setup(ctx: TestContext):
    header("Setup")

    info(f"Verifying pool '{ctx.pool}' exists...")
    pools = await ctx.client.call("fs.list")
    pool = next((p for p in pools if p["name"] == ctx.pool), None)
    if not pool:
        fail(f"Pool '{ctx.pool}' not found. Available: {[p['name'] for p in pools]}")
        sys.exit(1)
    if not pool["mounted"]:
        info(f"Mounting pool '{ctx.pool}'...")
        await ctx.client.call("fs.mount", {"name": ctx.pool})
    ok(f"Pool '{ctx.pool}' is mounted")

    info("Enabling protocols...")
    for proto in ["nfs", "smb", "iscsi", "nvmeof"]:
        try:
            await ctx.client.call("service.protocol.enable", {"name": proto})
            ok(f"Enabled {proto}")
        except Exception as e:
            warn(f"Enable {proto}: {e}")

    await asyncio.sleep(2)


async def main():
    parser = argparse.ArgumentParser(description="NASty integration test suite")
    parser.add_argument("--host",        required=True,       help="NASty appliance IP/hostname")
    parser.add_argument("--port",        type=int, default=443, help="WebUI HTTPS port (default 443)")
    parser.add_argument("--password",    default=None,        help="Admin password (prefer NASTY_PASSWORD)")
    parser.add_argument("--password-stdin", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--pool",        default=None,        help="Pool name (auto-detected if omitted; created if not found and --create-pool is set)")
    parser.add_argument("--create-pool", action="store_true", help="Auto-create the pool using available unmounted block devices if it does not exist")
    parser.add_argument("--skip-nfs",       action="store_true")
    parser.add_argument("--skip-smb",       action="store_true")
    parser.add_argument("--skip-iscsi",     action="store_true")
    parser.add_argument("--skip-nvmeof",    action="store_true")
    parser.add_argument("--skip-subvolume", action="store_true")
    parser.add_argument("--skip-snapshots", action="store_true")
    parser.add_argument("--skip-storage",   action="store_true")
    parser.add_argument("--skip-delete", action="store_true",
                        help="Skip server-side deletions (leave subvolumes/shares behind)")
    parser.add_argument("--delete-only", action="store_true",
                        help="Delete all test-* leftovers from a prior --skip-delete run, then exit")
    parser.add_argument("--tag", type=valid_tag, default=None,
                        help="Reuse a specific tag from a prior run (e.g. from --skip-delete)")
    parser.add_argument("--remount", action="store_true",
                        help="Skip creation/writes, mount existing shares and verify data only (use with --tag)")
    args = parser.parse_args()

    if args.password_stdin and args.password is not None:
        parser.error("--password and --password-stdin are mutually exclusive")
    if args.password_stdin:
        args.password = sys.stdin.readline().rstrip("\r\n")
    elif args.password is None:
        args.password = os.environ.get("NASTY_PASSWORD", "admin")

    if os.geteuid() != 0:
        print(f"{RED}ERROR:{RESET} This test must be run as root (needs mount/iscsi/nvme)")
        sys.exit(1)

    header("NASty Integration Test Suite")
    info(f"Target: {args.host}:{args.port}")

    # Warn and auto-skip if client tools are missing
    for proto, (cmd, pkg) in {
        "nfs":    ("mount.nfs",  "nfs-common"),
        "smb":    ("mount.cifs", "cifs-utils"),
        "iscsi":  ("iscsiadm",   "open-iscsi"),
        "nvmeof": ("nvme",       "nvme-cli"),
    }.items():
        if not getattr(args, f"skip_{proto}") and not cmd_exists(cmd):
            warn(f"{cmd} not found (install {pkg}), skipping {proto}")
            setattr(args, f"skip_{proto}", True)

    info("Connecting to NASty API...")
    client = NastyClient(args.host, args.port, args.password)
    try:
        await client.connect()
        ok("Connected and authenticated")
    except Exception as e:
        fail(f"Connection failed: {e}")
        sys.exit(1)

    pool_name = args.pool
    if not pool_name:
        pools = await client.call("fs.list")
        mounted = [p for p in pools if p["mounted"]]
        if not mounted:
            fail("No mounted pools found. Specify --pool or mount a pool first.")
            await client.close()
            sys.exit(1)
        pool_name = mounted[0]["name"]
        info(f"Auto-detected pool: {pool_name}")
    elif args.create_pool:
        # Create the pool if it doesn't already exist
        pools = await client.call("fs.list")
        existing = next((p for p in pools if p["name"] == pool_name), None)
        if not existing:
            info(f"Pool '{pool_name}' not found — discovering available devices...")
            devices = await client.call("device.list")
            available = [d for d in devices if not d.get("in_use")]
            if not available:
                fail("No available (unused) block devices found to create pool.")
                await client.close()
                sys.exit(1)
            device_specs = [{"path": d["path"]} for d in available]
            info(f"Creating pool '{pool_name}' on {[d['path'] for d in device_specs]}...")
            try:
                await client.call("fs.create", {"name": pool_name, "devices": device_specs})
                ok(f"Pool '{pool_name}' created")
            except Exception as e:
                fail(f"Failed to create pool '{pool_name}': {e}")
                await client.close()
                sys.exit(1)
        else:
            info(f"Pool '{pool_name}' already exists")

    if args.delete_only:
        try:
            for proto in ["nfs", "smb", "iscsi", "nvmeof"]:
                try:
                    await client.call("service.protocol.enable", {"name": proto})
                except Exception as e:
                    warn(f"Enable {proto} for cleanup: {e}")
            errors = await delete_leftovers(client, pool_name)
            if errors:
                for error in errors:
                    fail(error)
                sys.exit(1)
        finally:
            await client.close()
        return

    ctx = TestContext(client, args.host, pool_name, skip_delete=args.skip_delete,
                     tag=args.tag, remount=args.remount)

    info(f"Test tag: {ctx.tag}  (reuse with --tag {ctx.tag} --remount)")
    if args.remount:
        if not args.tag:
            warn("--remount without --tag: using freshly generated tag (nothing will be found)")
        warn("--remount: skipping creation, mounting existing shares only")
    elif args.skip_delete:
        warn("--skip-delete: subvolumes and shares will NOT be deleted after tests")

    try:
        await test_setup(ctx)

        if args.remount:
            expected = REMOUNT_EXPECTED_CHECKS
        elif args.skip_delete:
            expected = SKIP_DELETE_EXPECTED_CHECKS
        else:
            expected = EXPECTED_CHECKS

        if not args.skip_subvolume and not args.remount:
            await run_suite(ctx, "Subvolume", test_subvolume, expected["Subvolume"])
        else:                       warn("Subvolume: skipped")

        if not args.skip_snapshots and not args.remount:
            await run_suite(ctx, "Snapshots", test_snapshots, expected["Snapshots"])
        else:                       warn("Snapshots: skipped")

        if not args.skip_storage and not args.remount:
            await run_suite(ctx, "Storage", test_storage, expected["Storage"])
        else:                       warn("Storage: skipped")

        if not args.skip_nfs:
            await run_suite(ctx, "NFS", test_nfs, expected["NFS"])
        else:                    warn("NFS: skipped")

        if not args.skip_smb:
            await run_suite(ctx, "SMB", test_smb, expected["SMB"])
        else:                    warn("SMB: skipped")

        if not args.skip_nfs and not args.skip_smb and not args.remount:
            await run_suite(ctx, "Multi-protocol", test_multiprotocol,
                            expected["Multi-protocol"])
        else:                    warn("Multi-protocol: skipped")

        if not args.skip_iscsi:
            await run_suite(ctx, "iSCSI", test_iscsi, expected["iSCSI"])
        else:                    warn("iSCSI: skipped")

        if not args.skip_nvmeof:
            await run_suite(ctx, "NVMe-oF", test_nvmeof, expected["NVMe-oF"])
        else:                    warn("NVMe-oF: skipped")

        try:
            await audit_cleanup(ctx, audit_server=not ctx.skip_delete)
        except Exception as e:
            ctx.record("cleanup audit", False, str(e))

        if not ctx.results:
            ctx.record("test selection", False, "no checks were executed")

    finally:
        await client.close()

    header("Results")
    passed = sum(1 for _, p, _ in ctx.results if p)
    failed = sum(1 for _, p, _ in ctx.results if not p)

    for name, p, detail in ctx.results:
        status = f"{GREEN}PASS{RESET}" if p else f"{RED}FAIL{RESET}"
        suffix = f" — {detail}" if detail and not p else ""
        print(f"  [{status}] {name}{suffix}")

    print()
    color = GREEN if failed == 0 else RED
    print(f"{color}{BOLD}{passed}/{len(ctx.results)} passed, {failed} failed{RESET}")
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    asyncio.run(main())
