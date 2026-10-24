# Report validation measurements

This file replaces the report of 2026-09-12, which was written through a
denied write path and contained five documented inaccuracies; the
maintainer decided on 2026-10-24 to discard that text and regenerate it
from a fresh run of the same, unmodified measurement script. All numbers
below come from the run of 2026-10-24 recorded in full at
`/var/tmp/mdi-measure-fresh.log` (script output, rc=0).

## What was measured

`measure_report_validation.py` (sha256
`51c576d1b7e4b6b550f79788ae26221976c0bf8f9abb40816e66b222639907c9`)
builds synthetic reports of three sizes plus a qualification-light variant,
then times `report.validate_report` and `report.validate_transition` and
their internal helpers, with warmup 1 and 3 repetitions, medians reported
here. Measured source versions: `report.py`
`e0138382c19d28184dac095f6d9fa52ef17f4bc5c807d6397d5b7710b1447c2`,
`runstore.py` `5e47a3e1f56de3ac3c4ce39a2949fa2fde6ede178223b38999db664e21a32e6c`.
Host: Fedora 44, i7-1185G7, CPython 3.15.0. The September numbers were
taken on CPython 3.14.7 and are not directly comparable.

## Sizes

| Target | Serialized | Bundles | Subjects | Qualifications |
|---|---|---|---|---|
| small | 131,612 B | 60 | 60 | 15 |
| medium | 2,099,227 B | 957 | 957 | 240 |
| large | 12,583,895 B (12.0 MiB) | 5,700 | 5,700 | 1,425 |
| large-qlight | 12,583,224 B (12.0 MiB) | 5,929 | 5,929 | 119 |

## Medians

| Measurement | small | medium | large | large-qlight |
|---|---|---|---|---|
| validate_report | 0.00272 s | 0.06397 s | 1.0674 s | 0.5611 s |
| validate_transition | 0.00786 s | 0.19390 s | 3.2494 s | 1.7270 s |
| _required_limitations | 0.00009 s | 0.02047 s | 0.76800 s | 0.11339 s |
| _json_equal_full_twin | 0.00251 s | 0.04183 s | 0.27215 s | 0.43153 s |
| alloc peak (validate) | 21.7 KiB | 318.1 KiB | 1,981.6 KiB | 1,666.1 KiB |

## What the numbers say

Qualification scanning dominates validation cost growth: at the large
size `_required_limitations` takes 0.768 s of the 1.067 s
`validate_report` median and accounts for 956.0 KiB of the 1,981.6 KiB
traced allocation peak. The comparison pair proves the causality: the
qualification-light variant has the same serialized size (12.0 MiB) but
validates in 0.561 s instead of 1.067 s.

Allocation deltas per call stay between 0.3 and 1.6 KiB across every
measurement and size, so repeated validation in this synthetic loop does
not accumulate traced memory. This is a statement about these calls only;
it is not a claim about the whole collector, run-store chains or any real
workload.

The peak traced allocation for validating a 12.0 MiB serialized report is
1,981.6 KiB, about 1.93 MiB, excluding the input report objects, the
chain storage and the interpreter runtime.

## Limits of this measurement

Synthetic reports on one machine, three repetitions, one CPython build.
The smallest measured report is 131,612 B; nothing here describes reports
smaller than that, and no real-user report distribution was collected. No
production optimization was selected on the basis of these numbers. If
validation cost ever matters in practice, the qualification-scan finding
is the first place to look, with a real workload in hand.
