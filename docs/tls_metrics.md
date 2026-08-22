
## 2026-08-14T14:44:24Z — tag `baseline`

- rev `25dae1d` (dirty), build `release`, OpenSSL 3.0.13 30 Jan 2024, 12 cpus, kernel 6.6.87.2-microsoft-standard-WSL2
- params: {"repeat": 3, "burst": 300, "workers": 8, "handshakes": 100, "bench_requests": 100000, "bench_clients": 50, "pipelines": [16, 1], "tests": ["set", "get"]}
- artifact: `docs/tls_metrics_baseline.json`

| metric | median | noise (±) |
|---|---:|---:|
| `bench.get_P1.plain.ops_per_s` | 67,750.680 | 44.4% |
| `bench.get_P1.plain.p50_ms` | 0.375 | 2.1% |
| `bench.get_P1.tls.ops_per_s` | 51,334.700 | 0.7% |
| `bench.get_P1.tls.p50_ms` | 0.487 | 1.6% |
| `bench.get_P1.tls_over_plain` | 0.763 | 74.4% |
| `bench.get_P16.plain.ops_per_s` | 943,396.250 | 2.9% |
| `bench.get_P16.plain.p50_ms` | 0.447 | 7.2% |
| `bench.get_P16.tls.ops_per_s` | 564,971.750 | 6.5% |
| `bench.get_P16.tls.p50_ms` | 0.607 | 4.0% |
| `bench.get_P16.tls_over_plain` | 0.594 | 6.3% |
| `bench.set_P1.plain.ops_per_s` | 66,050.200 | 5.4% |
| `bench.set_P1.plain.p50_ms` | 0.383 | 6.3% |
| `bench.set_P1.tls.ops_per_s` | 50,916.500 | 3.2% |
| `bench.set_P1.tls.p50_ms` | 0.487 | 3.3% |
| `bench.set_P1.tls_over_plain` | 0.760 | 3.5% |
| `bench.set_P16.plain.ops_per_s` | 952,381.000 | 5.0% |
| `bench.set_P16.plain.p50_ms` | 0.455 | 1.8% |
| `bench.set_P16.tls.ops_per_s` | 578,034.690 | 11.2% |
| `bench.set_P16.tls.p50_ms` | 0.631 | 81.1% |
| `bench.set_P16.tls_over_plain` | 0.588 | 9.7% |
| `cert.restart_downtime_ms` | 68.515 | 0.0% |
| `handshake_plain.fresh_ms.max` | 0.256 | 9.1% |
| `handshake_plain.fresh_ms.n` | 100.000 | 0.0% |
| `handshake_plain.fresh_ms.p50` | 0.163 | 2.7% |
| `handshake_plain.fresh_ms.p90` | 0.173 | 4.5% |
| `handshake_plain.fresh_ms.p99` | 0.230 | 9.1% |
| `handshake_tls.fresh_ms.max` | 2.376 | 212.3% |
| `handshake_tls.fresh_ms.n` | 100.000 | 0.0% |
| `handshake_tls.fresh_ms.p50` | 1.518 | 0.5% |
| `handshake_tls.fresh_ms.p90` | 1.668 | 3.4% |
| `handshake_tls.fresh_ms.p99` | 1.950 | 6.7% |
| `handshake_tls.resume_saving_pct` | 40.756 | 2.8% |
| `handshake_tls.resumed_ms.max` | 1.131 | 3.8% |
| `handshake_tls.resumed_ms.n` | 100.000 | 0.0% |
| `handshake_tls.resumed_ms.p50` | 0.896 | 1.8% |
| `handshake_tls.resumed_ms.p90` | 0.973 | 0.9% |
| `handshake_tls.resumed_ms.p99` | 1.103 | 8.6% |
| `handshake_tls.resumption_rate` | 1.000 | 0.0% |
| `storm_plain.accept_to_reply_ms.max` | 108.399 | 2.3% |
| `storm_plain.accept_to_reply_ms.n` | 300.000 | 0.0% |
| `storm_plain.accept_to_reply_ms.p50` | 59.586 | 1.5% |
| `storm_plain.accept_to_reply_ms.p90` | 99.672 | 2.1% |
| `storm_plain.accept_to_reply_ms.p99` | 108.078 | 2.0% |
| `storm_plain.burst` | 300.000 | 0.0% |
| `storm_plain.connected` | 300.000 | 0.0% |
| `storm_plain.conns_per_s` | 2,765.524 | 2.3% |
| `storm_plain.failed` | 0.000 | 0.0% |
| `storm_plain.server_cpu_ms_per_conn` | 0.367 | 9.1% |
| `storm_plain.server_cpu_s` | 0.110 | 9.1% |
| `storm_plain.victim_during_burst_ms.max` | 0.452 | 26.2% |
| `storm_plain.victim_during_burst_ms.n` | 612.000 | 2.9% |
| `storm_plain.victim_during_burst_ms.p50` | 0.167 | 2.6% |
| `storm_plain.victim_during_burst_ms.p90` | 0.206 | 4.9% |
| `storm_plain.victim_during_burst_ms.p99` | 0.309 | 11.4% |
| `storm_plain.victim_idle_ms.max` | 0.377 | 97.2% |
| `storm_plain.victim_idle_ms.n` | 9,887.000 | 2.0% |
| `storm_plain.victim_idle_ms.p50` | 0.093 | 3.2% |
| `storm_plain.victim_idle_ms.p90` | 0.105 | 1.8% |
| `storm_plain.victim_idle_ms.p99` | 0.135 | 1.0% |
| `storm_plain.victim_stall_factor_p99` | 2.295 | 10.7% |
| `storm_plain.window_s` | 0.108 | 2.3% |
| `storm_plain.workers` | 8.000 | 0.0% |
| `storm_tls.accept_to_reply_ms.max` | 328.728 | 9.1% |
| `storm_tls.accept_to_reply_ms.n` | 300.000 | 0.0% |
| `storm_tls.accept_to_reply_ms.p50` | 173.487 | 22.9% |
| `storm_tls.accept_to_reply_ms.p90` | 302.757 | 10.2% |
| `storm_tls.accept_to_reply_ms.p99` | 328.088 | 9.2% |
| `storm_tls.burst` | 300.000 | 0.0% |
| `storm_tls.connected` | 300.000 | 0.0% |
| `storm_tls.conns_per_s` | 912.426 | 8.5% |
| `storm_tls.failed` | 0.000 | 0.0% |
| `storm_tls.server_cpu_ms_per_conn` | 1.100 | 3.0% |
| `storm_tls.server_cpu_s` | 0.330 | 3.0% |
| `storm_tls.victim_during_burst_ms.max` | 7.311 | 648.7% |
| `storm_tls.victim_during_burst_ms.n` | 176.000 | 19.9% |
| `storm_tls.victim_during_burst_ms.p50` | 1.889 | 11.7% |
| `storm_tls.victim_during_burst_ms.p90` | 3.114 | 4.6% |
| `storm_tls.victim_during_burst_ms.p99` | 5.003 | 24.4% |
| `storm_tls.victim_idle_ms.max` | 0.443 | 22.9% |
| `storm_tls.victim_idle_ms.n` | 7,933.000 | 4.0% |
| `storm_tls.victim_idle_ms.p50` | 0.117 | 4.0% |
| `storm_tls.victim_idle_ms.p90` | 0.132 | 7.0% |
| `storm_tls.victim_idle_ms.p99` | 0.170 | 7.1% |
| `storm_tls.victim_stall_factor_p99` | 29.702 | 26.3% |
| `storm_tls.window_s` | 0.329 | 9.1% |
| `storm_tls.workers` | 8.000 | 0.0% |

## 2026-08-14T17:14:24Z — tag `baseline-w300`

- rev `25dae1d` (dirty), build `release`, OpenSSL 3.0.13 30 Jan 2024, 12 cpus, kernel 6.6.87.2-microsoft-standard-WSL2
- params: {"repeat": 3, "burst": 300, "workers": 300, "handshakes": 100, "bench_requests": 100000, "bench_clients": 50, "pipelines": [16, 1], "tests": ["set", "get"]}
- artifact: `docs/tls_metrics_baseline-w300.json`

| metric | median | noise (±) |
|---|---:|---:|
| `storm_plain.accept_to_reply_ms.max` | 122.670 | 3.7% |
| `storm_plain.accept_to_reply_ms.n` | 300.000 | 0.0% |
| `storm_plain.accept_to_reply_ms.p50` | 74.098 | 5.5% |
| `storm_plain.accept_to_reply_ms.p90` | 118.082 | 4.1% |
| `storm_plain.accept_to_reply_ms.p99` | 122.409 | 3.7% |
| `storm_plain.burst` | 300.000 | 0.0% |
| `storm_plain.connected` | 300.000 | 0.0% |
| `storm_plain.conns_per_s` | 1,770.065 | 3.5% |
| `storm_plain.failed` | 0.000 | 0.0% |
| `storm_plain.server_cpu_ms_per_conn` | 0.567 | 11.8% |
| `storm_plain.server_cpu_s` | 0.170 | 11.8% |
| `storm_plain.victim_during_burst_ms.max` | 1.331 | 182.6% |
| `storm_plain.victim_during_burst_ms.n` | 883.000 | 5.3% |
| `storm_plain.victim_during_burst_ms.p50` | 0.173 | 6.5% |
| `storm_plain.victim_during_burst_ms.p90` | 0.220 | 15.9% |
| `storm_plain.victim_during_burst_ms.p99` | 0.436 | 25.8% |
| `storm_plain.victim_idle_ms.max` | 0.618 | 469.1% |
| `storm_plain.victim_idle_ms.n` | 8,910.000 | 4.0% |
| `storm_plain.victim_idle_ms.p50` | 0.100 | 3.8% |
| `storm_plain.victim_idle_ms.p90` | 0.128 | 5.9% |
| `storm_plain.victim_idle_ms.p99` | 0.237 | 10.1% |
| `storm_plain.victim_stall_factor_p99` | 1.867 | 25.8% |
| `storm_plain.window_s` | 0.169 | 3.4% |
| `storm_plain.workers` | 300.000 | 0.0% |
| `storm_tls.accept_to_reply_ms.max` | 367.131 | 28.6% |
| `storm_tls.accept_to_reply_ms.n` | 300.000 | 0.0% |
| `storm_tls.accept_to_reply_ms.p50` | 292.092 | 62.7% |
| `storm_tls.accept_to_reply_ms.p90` | 363.718 | 28.8% |
| `storm_tls.accept_to_reply_ms.p99` | 366.505 | 28.7% |
| `storm_tls.burst` | 300.000 | 0.0% |
| `storm_tls.connected` | 300.000 | 0.0% |
| `storm_tls.conns_per_s` | 806.666 | 22.8% |
| `storm_tls.failed` | 0.000 | 0.0% |
| `storm_tls.server_cpu_ms_per_conn` | 1.233 | 8.1% |
| `storm_tls.server_cpu_s` | 0.370 | 8.1% |
| `storm_tls.victim_during_burst_ms.max` | 53.712 | 181.7% |
| `storm_tls.victim_during_burst_ms.n` | 113.000 | 9.7% |
| `storm_tls.victim_during_burst_ms.p50` | 0.208 | 11.9% |
| `storm_tls.victim_during_burst_ms.p90` | 8.854 | 144.6% |
| `storm_tls.victim_during_burst_ms.p99` | 43.844 | 224.8% |
| `storm_tls.victim_idle_ms.max` | 0.480 | 577.3% |
| `storm_tls.victim_idle_ms.n` | 7,383.000 | 5.6% |
| `storm_tls.victim_idle_ms.p50` | 0.118 | 4.2% |
| `storm_tls.victim_idle_ms.p90` | 0.155 | 7.5% |
| `storm_tls.victim_idle_ms.p99` | 0.269 | 18.0% |
| `storm_tls.victim_stall_factor_p99` | 168.261 | 208.8% |
| `storm_tls.window_s` | 0.372 | 27.4% |
| `storm_tls.workers` | 300.000 | 0.0% |

## 2026-08-14T21:48:41Z — tag `accept-cap-4`

- rev `25dae1d` (dirty), build `release`, OpenSSL 3.0.13 30 Jan 2024, 12 cpus, kernel 6.6.87.2-microsoft-standard-WSL2
- params: {"repeat": 3, "burst": 300, "workers": 300, "handshakes": 100, "bench_requests": 100000, "bench_clients": 50, "pipelines": [16, 1], "tests": ["set", "get"]}
- artifact: `docs/tls_metrics_accept-cap-4.json`

| metric | median | noise (±) |
|---|---:|---:|
| `storm_plain.accept_to_reply_ms.max` | 111.694 | 3.6% |
| `storm_plain.accept_to_reply_ms.n` | 300.000 | 0.0% |
| `storm_plain.accept_to_reply_ms.p50` | 67.782 | 3.7% |
| `storm_plain.accept_to_reply_ms.p90` | 107.209 | 3.1% |
| `storm_plain.accept_to_reply_ms.p99` | 111.483 | 3.7% |
| `storm_plain.burst` | 300.000 | 0.0% |
| `storm_plain.connected` | 300.000 | 0.0% |
| `storm_plain.conns_per_s` | 1,955.171 | 2.1% |
| `storm_plain.failed` | 0.000 | 0.0% |
| `storm_plain.server_cpu_ms_per_conn` | 0.533 | 6.3% |
| `storm_plain.server_cpu_s` | 0.160 | 6.3% |
| `storm_plain.victim_during_burst_ms.max` | 0.368 | 1662.6% |
| `storm_plain.victim_during_burst_ms.n` | 881.000 | 8.1% |
| `storm_plain.victim_during_burst_ms.p50` | 0.157 | 6.7% |
| `storm_plain.victim_during_burst_ms.p90` | 0.191 | 6.0% |
| `storm_plain.victim_during_burst_ms.p99` | 0.237 | 3.7% |
| `storm_plain.victim_idle_ms.max` | 0.478 | 46.5% |
| `storm_plain.victim_idle_ms.n` | 10,392.000 | 7.5% |
| `storm_plain.victim_idle_ms.p50` | 0.090 | 9.1% |
| `storm_plain.victim_idle_ms.p90` | 0.101 | 7.0% |
| `storm_plain.victim_idle_ms.p99` | 0.129 | 7.2% |
| `storm_plain.victim_stall_factor_p99` | 1.846 | 10.1% |
| `storm_plain.window_s` | 0.153 | 2.1% |
| `storm_plain.workers` | 300.000 | 0.0% |
| `storm_tls.accept_to_reply_ms.max` | 328.822 | 29.6% |
| `storm_tls.accept_to_reply_ms.n` | 300.000 | 0.0% |
| `storm_tls.accept_to_reply_ms.p50` | 254.785 | 60.5% |
| `storm_tls.accept_to_reply_ms.p90` | 325.501 | 22.8% |
| `storm_tls.accept_to_reply_ms.p99` | 328.580 | 29.6% |
| `storm_tls.burst` | 300.000 | 0.0% |
| `storm_tls.connected` | 300.000 | 0.0% |
| `storm_tls.conns_per_s` | 883.643 | 18.6% |
| `storm_tls.failed` | 0.000 | 0.0% |
| `storm_tls.server_cpu_ms_per_conn` | 1.167 | 8.6% |
| `storm_tls.server_cpu_s` | 0.350 | 8.6% |
| `storm_tls.victim_during_burst_ms.max` | 41.359 | 109.6% |
| `storm_tls.victim_during_burst_ms.n` | 172.000 | 107.0% |
| `storm_tls.victim_during_burst_ms.p50` | 0.209 | 90.2% |
| `storm_tls.victim_during_burst_ms.p90` | 3.812 | 401.4% |
| `storm_tls.victim_during_burst_ms.p99` | 36.193 | 134.5% |
| `storm_tls.victim_idle_ms.max` | 0.418 | 48.1% |
| `storm_tls.victim_idle_ms.n` | 8,500.000 | 6.3% |
| `storm_tls.victim_idle_ms.p50` | 0.109 | 8.8% |
| `storm_tls.victim_idle_ms.p90` | 0.125 | 4.6% |
| `storm_tls.victim_idle_ms.p99` | 0.163 | 3.2% |
| `storm_tls.victim_stall_factor_p99` | 221.931 | 131.3% |
| `storm_tls.window_s` | 0.340 | 21.7% |
| `storm_tls.workers` | 300.000 | 0.0% |

## 2026-08-14T21:56:27Z — tag `power-check`

- rev `25dae1d` (dirty), build `release`, OpenSSL 3.0.13 30 Jan 2024, 12 cpus, kernel 6.6.87.2-microsoft-standard-WSL2
- params: {"repeat": 3, "burst": 1500, "workers": 300, "handshakes": 100, "bench_requests": 100000, "bench_clients": 50, "pipelines": [16, 1], "tests": ["set", "get"]}
- artifact: `docs/tls_metrics_power-check.json`

| metric | median | noise (±) |
|---|---:|---:|
| `storm_plain.accept_to_reply_ms.max` | 503.479 | 8.9% |
| `storm_plain.accept_to_reply_ms.n` | 1,500.000 | 0.0% |
| `storm_plain.accept_to_reply_ms.p50` | 291.893 | 11.6% |
| `storm_plain.accept_to_reply_ms.p90` | 475.932 | 9.1% |
| `storm_plain.accept_to_reply_ms.p99` | 501.624 | 8.9% |
| `storm_plain.burst` | 1,500.000 | 0.0% |
| `storm_plain.connected` | 1,500.000 | 0.0% |
| `storm_plain.conns_per_s` | 2,832.320 | 8.5% |
| `storm_plain.failed` | 0.000 | 0.0% |
| `storm_plain.server_cpu_ms_per_conn` | 0.380 | 3.5% |
| `storm_plain.server_cpu_s` | 0.570 | 3.5% |
| `storm_plain.victim_during_burst_ms.max` | 1.321 | 20.2% |
| `storm_plain.victim_during_burst_ms.n` | 1,132.000 | 1.3% |
| `storm_plain.victim_during_burst_ms.p50` | 0.475 | 10.0% |
| `storm_plain.victim_during_burst_ms.p90` | 0.713 | 8.9% |
| `storm_plain.victim_during_burst_ms.p99` | 0.866 | 8.9% |
| `storm_plain.victim_idle_ms.max` | 0.405 | 22.6% |
| `storm_plain.victim_idle_ms.n` | 10,060.000 | 4.3% |
| `storm_plain.victim_idle_ms.p50` | 0.089 | 8.7% |
| `storm_plain.victim_idle_ms.p90` | 0.109 | 4.9% |
| `storm_plain.victim_idle_ms.p99` | 0.160 | 29.9% |
| `storm_plain.victim_stall_factor_p99` | 5.418 | 35.1% |
| `storm_plain.window_s` | 0.530 | 8.8% |
| `storm_plain.workers` | 300.000 | 0.0% |
| `storm_tls.accept_to_reply_ms.max` | 1,718.864 | 17.1% |
| `storm_tls.accept_to_reply_ms.n` | 1,500.000 | 0.0% |
| `storm_tls.accept_to_reply_ms.p50` | 1,093.354 | 13.1% |
| `storm_tls.accept_to_reply_ms.p90` | 1,696.974 | 12.1% |
| `storm_tls.accept_to_reply_ms.p99` | 1,717.510 | 16.6% |
| `storm_tls.burst` | 1,500.000 | 0.0% |
| `storm_tls.connected` | 1,500.000 | 0.0% |
| `storm_tls.conns_per_s` | 872.388 | 17.4% |
| `storm_tls.failed` | 0.000 | 0.0% |
| `storm_tls.server_cpu_ms_per_conn` | 1.127 | 3.0% |
| `storm_tls.server_cpu_s` | 1.690 | 3.0% |
| `storm_tls.victim_during_burst_ms.max` | 85.497 | 39.3% |
| `storm_tls.victim_during_burst_ms.n` | 222.000 | 4.5% |
| `storm_tls.victim_during_burst_ms.p50` | 0.429 | 41.7% |
| `storm_tls.victim_during_burst_ms.p90` | 22.543 | 54.6% |
| `storm_tls.victim_during_burst_ms.p99` | 82.073 | 33.0% |
| `storm_tls.victim_idle_ms.max` | 0.389 | 41.0% |
| `storm_tls.victim_idle_ms.n` | 7,803.000 | 9.1% |
| `storm_tls.victim_idle_ms.p50` | 0.116 | 11.7% |
| `storm_tls.victim_idle_ms.p90` | 0.136 | 9.6% |
| `storm_tls.victim_idle_ms.p99` | 0.236 | 11.9% |
| `storm_tls.victim_stall_factor_p99` | 354.772 | 35.3% |
| `storm_tls.window_s` | 1.719 | 17.1% |
| `storm_tls.workers` | 300.000 | 0.0% |

## 2026-08-16T00:14:04Z — tag `cert-reload`

- rev `25dae1d` (dirty), build `release`, OpenSSL 3.0.13 30 Jan 2024, 12 cpus, kernel 6.6.87.2-microsoft-standard-WSL2
- params: {"repeat": 3, "burst": 300, "workers": 8, "handshakes": 100, "bench_requests": 100000, "bench_clients": 50, "pipelines": [16, 1], "tests": ["set", "get"]}
- artifact: `docs/tls_metrics_cert-reload.json`

| metric | median | noise (±) |
|---|---:|---:|
| `cert.hot_reload_ms` | 1.158 | 0.0% |
| `cert.restart_downtime_ms` | 61.445 | 0.0% |

## 2026-08-16T00:16:19Z — tag `cert-reload`

- rev `25dae1d` (dirty), build `release`, OpenSSL 3.0.13 30 Jan 2024, 12 cpus, kernel 6.6.87.2-microsoft-standard-WSL2
- params: {"repeat": 3, "burst": 300, "workers": 8, "handshakes": 100, "bench_requests": 100000, "bench_clients": 50, "pipelines": [16, 1], "tests": ["set", "get"]}
- artifact: `docs/tls_metrics_cert-reload.json`

| metric | median | noise (±) |
|---|---:|---:|
| `cert.bad_material_refused` | True | — |
| `cert.hot_reload_cert_changed` | True | — |
| `cert.hot_reload_conns_survived` | True | — |
| `cert.hot_reload_ms` | 1.092 | 0.0% |
| `cert.hot_reload_supported` | True | — |
| `cert.key_directive_sets_key_field` | True | — |
| `cert.restart_cert_changed` | True | — |
| `cert.restart_conns_survived` | False | — |
| `cert.restart_downtime_ms` | 62.070 | 0.0% |
| `cert.rollback_restored_path` | True | — |
| `cert.still_serving_after_bad_set` | True | — |
