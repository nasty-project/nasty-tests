# NASty Integration Tests

End-to-end integration tests for the [NASty](https://github.com/nasty-project/nasty) storage appliance. Tests exercise the full stack — engine API, storage protocols, and data integrity — over the network from a Linux client.

## Running

Tests run inside a [Colima](https://github.com/abiosoft/colima) Linux VM (required for NFS/SMB/iSCSI/NVMe-oF client operations from macOS):

```bash
NASTY_PASSWORD=admin ./run-tests.sh --host 10.10.10.50
```

The runner automatically provisions the VM with required packages (`nfs-common`, `cifs-utils`, `open-iscsi`, `nvme-cli`, `python3`, `websockets`) on first run.

### Options

| Flag | Description |
|------|-------------|
| `--host HOST` | NASty appliance IP (required) |
| `--password PW` | Admin password (default: `admin`; `NASTY_PASSWORD` avoids process arguments) |
| `--pool POOL` | Filesystem name (auto-detected if omitted) |
| `--skip-subvolume` | Skip subvolume lifecycle tests |
| `--skip-snapshots` | Skip snapshot tests |
| `--skip-storage` | Skip compression and integrity tests |
| `--skip-nfs` | Skip NFS tests |
| `--skip-smb` | Skip SMB tests |
| `--skip-iscsi` | Skip iSCSI tests |
| `--skip-nvmeof` | Skip NVMe-oF tests |
| `--skip-delete` | Leave test resources behind for inspection |
| `--delete-only` | Clean up leftovers from a prior `--skip-delete` run |
| `--skip-rbac` | Skip management-role and scoped-token tests |
| `--skip-protocol-auth` | Skip authenticated/denied protocol probes |
| `--only-security` | Run only RBAC and protocol authorization tests |

Security suites are skipped with `--skip-delete` because their temporary users,
tokens, and authorization fixtures must always be removed.

## Test Suites

### test_subvolume (~26 checks)
Subvolume lifecycle: create, list, get, delete, properties (set/find/remove), block subvolume attach/detach, block resize.

### test_snapshots (~9 checks)
Snapshot creation (read-only flag), clone from snapshot, clone appears as independent subvolume.

### test_storage (~16 checks)
Compression verification (zstd), snapshot data integrity — writes data before snapshot, verifies original has post-snapshot data, verifies clone has pre-snapshot data.

### test_nfs (~19 checks)
Per-subvolume NFS share lifecycle: create share, mount on client, write/read data, verify across multiple subvolumes. Snapshot clone shares. Full cleanup.

### test_smb (~18 checks)
Per-subvolume SMB share lifecycle: create share with user auth, CIFS mount, write/read data. Snapshot clone shares. Full cleanup.

### test_iscsi (~35 checks)
Per-subvolume iSCSI target lifecycle: create block subvolume + target, iSCSI discovery/login, format + mount, write/read data. Snapshot clone targets. Full cleanup.

### test_nvmeof (~43 checks)
Per-subvolume NVMe-oF subsystem lifecycle: create block subvolume + subsystem, nvme connect, format + mount, write/read data. Snapshot clone subsystems. Full cleanup.

### test_multiprotocol (~14 checks)
Cross-protocol test: creates a filesystem subvolume and shares it via both NFS and SMB simultaneously. Verifies data written via one protocol is readable via the other.

### test_rbac (26 checks)
Admin, Operator, and ReadOnly sessions; filesystem-scoped API tokens; owner
isolation between independent operator tokens; denied cross-owner access.

### test_protocol_auth (18 checks)
NFS client restrictions, authenticated SMB shares, iSCSI CHAP and initiator
ACLs, and NVMe-oF host-NQN allow/revoke behavior.

### test_cleanup
Utility: deletes all `test-*` resources from a prior `--skip-delete` run.

## Output

Results are printed in real-time and saved to `last-run.log`:

```
414/414 passed, 0 failed
```

## Related

- [NASty](https://github.com/nasty-project/nasty) — the NAS appliance under test
- [nasty-csi](https://github.com/nasty-project/nasty-csi) — CSI driver (has its own Ginkgo E2E tests)
