# vSphere 8 clusters, ready to import

Build a vSphere 8 cluster outside VCF to the point VCF 9.1 will import it as a workload domain: DNS, the host build check, the appliance deploy or upgrade, the cluster, NTP, the distributed switch, the vmkernels, the management migration, vSAN, the vLCM image, SSH on the appliance, and the import prerequisites. Plan by default. Nothing is written without `--commit`.

When the hosts are coming out of another vCenter rather than being built new, `host_release.py` runs first: it takes each host out of its live cluster, out of NSX and out of inventory, and leaves it standalone, which is the state `esx_ready.sh` requires.

License: GPL-3.0. Built and proved out for [essential.coach](https://essential.coach).
Full write-up: [Importing ESXi 8 Into VCF 9.1 When ESX 9 Won't Run on the Hardware](https://essential.coach/importing-esxi-8-into-vcf-9-1/)

The example config calls the sites `site-a` and `site-b`, and the hosts are `example.com`. They are not a design.

## Run

```bash
python3 deploy_vc.py selftest                                   # 264 checks, no environment needed
cp deploy_vc.example.yaml env_vc8.yaml                          # then edit the sites, hosts and ranges
python3 deploy_vc.py build --config env_vc8.yaml                # plan: reads everything, writes nothing
python3 deploy_vc.py build --config env_vc8.yaml --commit
python3 deploy_vc.py build --config env_vc8.yaml --only image   # one step; repeatable
```

The steps, in order: `dns`, `esx`, `vcsa`, `upgrade`, `objects`, `hosts`, `ntp`, `vds`, `vmk`, `mgmt`, `vsan`, `image`, `ssh`, `ready`. `ntp` sits right after `hosts` because vSAN wants the clocks together before it forms a cluster and the import refuses a host whose time is unmanaged. `mgmt` moves vmk0 and the uplink carrying it in one host transaction; that uplink stays out of `network.pnics`. `image` seeds the cluster's vLCM image from a host already in it: the import refuses a baseline-managed cluster.

`vds` creates the switch at `network.vds_version`, 8.0.3 unless set. Left to choose, a 9.1.1 vCenter builds a version 9 switch, and an ESXi 8 host cannot join it: the host attach fails with "the operation is not supported on the object", which explains nothing. A switch that already exists at another version blocks the step with the version it found and the one the hosts need, because a distributed switch is upgraded one way only: delete it in the vSphere Client and run the step again. A site whose hosts run a different build than the shared `esx:` block carries its own `esx:` block, which replaces the shared one outright.

`vmk` adds the vMotion and vSAN adapters from the configured ranges. A host that already has an adapter on that portgroup at another address is refused, naming both addresses, because adding a second one would put two vmkernels on one subnet. `network.vmotion.gateway` and `network.vsan.gateway` are for routed vMotion or vSAN. A configured gateway that is missing on the host is a planned change, set on commit and read back; a gateway nobody asked for is reported, never removed, because taking one away is the riskier direction and no config says to.

`--set NAME=VALUE` changes one config value by its full name and stops, writing a backup of the file first, so a range left blank is filled from the command line rather than by editing a live file: `--set sites.site-a.network.vmotion.range=10.0.141.61-99`. `--precheck` checks the appliance deploy or upgrade without touching anything. `--vendor-precheck` also runs vcsa-deploy's own precheck, which on a 9.x upgrade updates vLCM on the source appliance, so it needs `--commit`. `--detailed-exitcode`: 0 nothing pending, 2 pending, 1 blocked.

Every step reads the live environment and plans against it, so a run that stopped part way is finished by running the same command again. After every write the object is read back and reported as it is now, not as the API said.

Not done here: licensing, and the import itself. The hosts themselves are [esxi-vcf-host-prep](https://github.com/noahfarshad/esxi-vcf-host-prep)'s job, and it runs first.

Passwords come from the config or are asked for by name, and never reach the run log (`/tmp/deploy_vc/<timestamp>/console.txt`).

Needs `pyvmomi` from the `vcsa` step on. Run it with any Python: if the one you typed lacks pyvmomi, the tool looks for one that has it and re-runs itself under it. If no Python on the machine has it, `python3 deploy_vc.py bootstrap` makes a virtualenv the tool then finds on its own. PyYAML is optional.

## Releasing hosts from the vCenter they are in today

```bash
python3 host_release.py selftest                                      # 158 checks, no vCenter needed
python3 host_release.py example                                       # the source: block, documented
python3 host_release.py survey --config env_vc8.yaml --site site-a    # where each host is, and whether vSAN can lose it
python3 host_release.py plan   --config env_vc8.yaml --site site-a
python3 host_release.py apply  --config env_vc8.yaml --site site-a --commit
python3 host_release.py verify --config env_vc8.yaml --site site-a    # exit 0 once every host is out
```

The hosts and the source vCenter come from a `source:` block under the site in the same env file, so one file describes both ends of a move; `deploy_vc.py` tolerates the block and `host_release.py` validates it. The phases, each read from live inventory and resumable:

| Phase | What it does | Refuses when |
|---|---|---|
| `check` | the host is where the config says | it is in a different cluster, or disconnected while still clustered or holding vSAN disks |
| `evacuate` | reports what maintenance mode will relocate | powered-on VMs and DRS is not fully automated; or every host in the cluster is leaving and the VMs are not vCLS agents |
| `drain` | maintenance mode with full data migration, the multi-hour one | the source vSAN would be left under `min_free_pct` free (default 25), or its capacity cannot be read. Checked on a plan as well as a commit |
| `disks` | the vSAN disk groups out, with a `noAction` decommission spec so vSAN does not try to relocate what `drain` already moved | the host is not in maintenance mode |
| `uncluster` | out of the cluster to the datacenter level, which is what makes NSX uninstall from a host it manages through the cluster | vSAN disks still claimed |
| `nsx` | waits for NSX to finish, then force-deletes a transport node that lingers (`--nsx-wait`, default 600 s) | the host is a transport node and no `source.nsx` is configured, or the source NSX cannot be read |
| `release` | disconnected and out of inventory: standalone, ready for `esx_ready.sh` | NSX still lists it, or the host still carries an N-VDS or TEP vmkernels |

That last refusal is deliberate. `esx_ready.sh` has no NSX handling: it would remove the TEP vmkernels as extras and leave the VIBs and the N-VDS, which then fail the destination's vLCM solution apply. The block prints the `nsx_cleanup.py` command instead, in both its in-vCenter and standalone forms ([nsx-host-cleanup](https://github.com/noahfarshad/nsx-host-cleanup)).

Capacity is judged the way vSAN reports it: only the capacity tier counts, and a host's raw bytes are scaled by the ratio the cluster itself reports between raw and usable. One host drains at a time by default, because an evacuation draws on the free space the other hosts share; `--parallel N` lifts that. `--hosts esx13,esx14` acts on a subset, and the capacity check then counts only those as leaving, so a cluster that cannot lose all of its hosts may still be able to lose some. `--only drain` runs one phase. When every host in the cluster is on the list there is nowhere to evacuate to, so the cluster is dissolved rather than shrunk: the plan says so and how much data is abandoned with it, and goes no further until `--dissolve` is passed. With it, maintenance mode moves no data and every host drains at once.

A standalone host at the datacenter level is a `ComputeResource` wrapping the `HostSystem`, and the `HostSystem` refuses `Destroy_Task` with "The operation is not supported on the object"; the wrapper is what goes. Every vCenter task has a deadline, so a wedged task reports instead of spinning. Ctrl-C is safe: the tool keeps no state of its own.

`host_release.py` imports `deploy_vc.py` from its own directory. When `source.nsx` is set it also needs `nsx_prep.py` from [nsx-overlay-prep](https://github.com/noahfarshad/nsx-overlay-prep) beside it, for the NSX client; the self-test says so and skips its end-to-end checks when the file is absent.

## Where it sits

Every new cluster, and every day-2 host add, in this order. Each step blocks on the one before it, on purpose.

0. `host_release.py`, this repository, when the hosts are coming out of another vCenter: evacuate, drain, disk groups out, out of the cluster, out of NSX, out of inventory.
1. [esxi-vcf-host-prep](https://github.com/noahfarshad/esxi-vcf-host-prep): `esx_ready.sh` takes the hosts themselves to the build, the hostname, one vmkernel, one uplink, one portgroup, clean disks, VMFS-6, a certificate naming the host, and out of maintenance mode. Stale NSX from a previous life comes off here too, with [nsx-host-cleanup](https://github.com/noahfarshad/nsx-host-cleanup) in `--direct` mode.
2. `deploy_vc.py`, this repository. The `esx` step refuses a cluster whose hosts are on mixed builds.
3. The VCF import, then the inventory sync in VCF Operations, which stalls on a host without NTP.
4. [nsx-overlay-prep](https://github.com/noahfarshad/nsx-overlay-prep): the overlay a brownfield import leaves out.
