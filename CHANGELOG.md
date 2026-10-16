# Changelog

All notable changes to this project are documented here.

## [1.0.0] — 2026-10-16

### Added

- Initial public release. Builds a vSphere 8 cluster outside VCF to the point VCF 9.1 imports it as a workload domain, in fourteen steps from one YAML file: DNS, host build, vcsa deploy or upgrade, cluster, hosts, NTP, VDS, vmkernels, management migration, vSAN, vLCM image, SSH, import prerequisites. Plan by default; `--commit` writes; `--only` runs one step.
- `selftest`: 246 checks against stub hosts and a stub vCenter, no environment needed.

### Notes

- Every name and address in the example config is a placeholder: `example.com` hosts, `site-a` and `site-b`, private ranges.
- Passwords come from the config or a prompt, and never reach the run log.
- The tool's own version string is 1.0.
