# Changelog

All notable changes to this project are documented here.

## [1.2.0] — 2026-10-24

### Added

- `image` sets the desired image before it enables image management. A cluster created before any host joined has no image - nothing was there to seed it from - and the enablement check fails on exactly that. The step now takes the image from a host already in the cluster, over the documented calls behind Setup Image > Import image from a host: `esx/settings/depots/offline?action=createFromHost` extracts what the host runs into the depot, the specification it returns is imported as a draft (`import-software-spec`), the draft is committed, then the enablement check and the enable run as before. The desired image is what the hosts run, so nothing is non-compliant and nothing is remediated. The imported specification is kept in the evidence directory. An image that cannot be read is not mistaken for one that is missing, and a draft someone left open on the cluster blocks the step, naming its owner, rather than being overwritten.
- `objects` seeds a brand-new vCenter's first cluster from the site's first host: `CreateClusterEx` takes a connect spec for a host not yet in the inventory, joins it to the cluster and takes its image in one call. Until now a seed needed a host already in the vCenter, which a fresh one does not have, and the cluster was created baseline-managed.
- `cluster.image_seed_from`, now documented: `auto` (the default), a host name, or `none` for a baseline-managed cluster the `image` step converts later.

### Changed

- `deploy_vc.py selftest` is 289 checks (was 264): the image taken from a host before the enable, the exact call sequence and bodies, the open-draft and unreadable-image blocks, the extract cut down to what the import takes, and the cluster's birth seeded from a new host, an existing host, a named host, or not at all.
- The tool's own version string is `deploy_vc.py` 1.1, so the run log says which copy ran.

### Notes

- Found on the first run against a 9.1.1 vCenter: the cluster was created before its hosts joined, so it had no vLCM image, and the import would have refused it. Settling that before the import rather than after is what this release is for.
- The conversion sequence is exercised against the self-test's stubbed REST side, call by call and body by body, against the API reference. The first live run is the plan, which sends nothing, then `--only image --commit`.

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
