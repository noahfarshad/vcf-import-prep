# Changelog

All notable changes to this project are documented here.

## [1.1.1] — 2026-10-23

### Added

- `network.vds_version` (default `8.0.3`): the distributed switch is created at a version the hosts can join. Left to choose, a 9.1.1 vCenter builds a version 9 switch and an ESXi 8 host cannot join it; the host attach fails with "the operation is not supported on the object". A switch that already exists at another version blocks the `vds` step, naming the version it found and the one the hosts need, because a switch is upgraded one way only: delete it in the vSphere Client and run the step again.
- A per-site `esx:` block (`build`, `user`) that replaces the shared one outright, for a site whose hosts run a different build.

### Changed

- `vmk`: a configured `gateway` that is missing on a host is now a planned change, set on commit with `UpdateVirtualNic` and read back. A gateway nobody asked for is still reported, never removed.
- `deploy_vc.py selftest` is 264 checks (was 256): the switch version reaches the create call, an explicit `vds_version` wins, the schema knows it, a site inherits or overrides the shared `esx:` block, and the gateway is set and read back on commit.

### Notes

- Found on the first run against a 9.1.1 vCenter in front of ESXi 8 hosts: the appliance deployed, the cluster formed, the hosts joined, and the `vds` step was the first thing that could not be read back as asked.

## [1.1.0] — 2026-10-23

### Added

- `host_release.py` 1.0: release ESXi hosts from the vCenter they are in today so `esx_ready.sh` can prep them for the one they are going to. Seven phases, each read from live inventory and resumable: `check`, `evacuate`, `drain` (maintenance mode with full data migration, the multi-hour one), `disks` (vSAN disk groups out, with a `noAction` decommission spec so vSAN does not try to relocate), `uncluster` (to the datacenter level, which is what makes NSX uninstall from a host it manages through the cluster), `nsx` (wait for the uninstall, force-delete a transport node that lingers), `release` (disconnected and out of inventory). `survey`, `plan`, `apply --commit`, `verify`; `--hosts` for a subset, `--parallel`, `--nsx-wait`, `--dissolve` for a cluster every host is leaving.
- The gates in `host_release.py` that stop it destroying things: a host found outside the cluster the config names is refused; an evacuation that would leave the source vSAN under `min_free_pct` (default 25) free is refused, on a plan as well as a commit, and so is one whose capacity cannot be read; powered-on VMs need DRS fully automated or they are not moved; a cluster every host is leaving is dissolved, not shrunk, and needs `--dissolve` said out loud; a host still carrying an N-VDS or TEP vmkernels is not released, because `esx_ready.sh` has no NSX handling, and the `nsx_cleanup.py` command is printed instead. 158 self-test checks.
- The `source:` block in the config, read by `host_release.py` and tolerated by `deploy_vc.py`, so one env file describes both ends of a move. `python3 host_release.py example` prints it.
- `network.vmotion.gateway` and `network.vsan.gateway` for routed vMotion or vSAN. Left out, the adapter gets no gateway. A host whose adapter already has a different gateway is reported, never rewritten.

### Changed

- `deploy_vc.py`'s `vmk` step refuses to add a second vmkernel on a portgroup where the host already has one at another address: two vmkernels on one subnet is not a state anyone wants, and it used to be what a wrong range produced. The block names both addresses.
- `deploy_vc.py selftest` is 256 checks (was 246), and a failed check prints what it saw.

### Notes

- `host_release.py` imports `deploy_vc.py` from its own directory. When `source.nsx` is set it also needs `nsx_prep.py` from [nsx-overlay-prep](https://github.com/noahfarshad/nsx-overlay-prep) beside it, for the NSX client; the self-test says so and skips its end-to-end checks when the file is absent.
- The tool's own version strings are `deploy_vc.py` 1.0 and `host_release.py` 1.0.
- Every name and address in the example config and the self-tests is a placeholder.

## [1.0.0] — 2026-10-16

### Added

- Initial public release. Builds a vSphere 8 cluster outside VCF to the point VCF 9.1 imports it as a workload domain, in fourteen steps from one YAML file: DNS, host build, vcsa deploy or upgrade, cluster, hosts, NTP, VDS, vmkernels, management migration, vSAN, vLCM image, SSH, import prerequisites. Plan by default; `--commit` writes; `--only` runs one step.
- `selftest`: 246 checks against stub hosts and a stub vCenter, no environment needed.

### Notes

- Every name and address in the example config is a placeholder: `example.com` hosts, `site-a` and `site-b`, private ranges.
- Passwords come from the config or a prompt, and never reach the run log.
- The tool's own version string is 1.0.
