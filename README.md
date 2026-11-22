# Umbermill

Umbermill selects Python tests from an independent patch and a baseline dependency map. It reads the test inventory and patches from `/data` and writes a JSON record for each patch. The container can run offline after the image is built.

Baseline evidence covers test execution, imports, fixture reads, and source observers. The selector also follows inherited dialect settings, mapping consumers, and method dispatch. For ordinary body edits, it checks qualified statement and operand evidence before using the broader execution dependencies. The scheduler balances the selected tests against the two-second cost of opening each worker.

For module initialization failures, Umbermill combines cold imports with observations from test collection and the running suite. For certain literal changes in the optimizer or executor, it checks the original TPC-DS workload on the recorded platform. The check uses primitive values observed in the preceding tests, the executor test and its worker processes. A changed outcome or an observation the check cannot verify keeps the test on the schedule.

Eligible selections can run against a reconstructed patch in isolated processes. The verifier checks collection order, tracks failures through setup, subtests, and teardown, and prices replay against the worker cost it could save. A warm process can replace function bodies or reconstruct a verified group of affected imports. It uses fresh imports when it cannot verify the warm state. Ordinary SQL fixture edits can share an unmodified starting process.

The build prepares evidence from the pinned SQLGlot source and original tests. Runtime checks can use that evidence for statements, loops, tokenizer inputs, and executor work. Unresolved tests remain eligible by default. For experiments, `UMBERMILL_LAZY_IMPORTS=1` defers optional numerical packages in replay children, and `UMBERMILL_INFER_PASSES=1` permits an unexecuted long test to be omitted based on related passing tests.

## Credits

The test inventory and interchange format come from [Builders](https://www.joinbuilders.org/buildathon). Source verification uses the [pinned SQLGlot revision](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa). The upstream MIT license is retained in the image. Baseline measurements used [pytest](https://pytest.org/) and [coverage.py](https://coverage.readthedocs.io/). See [LICENSE](LICENSE) for this implementation.
