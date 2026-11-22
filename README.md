# Umbermill

Umbermill selects Python tests from an independent patch and a baseline dependency map. It reads the test inventory and patches from `/data` and writes a JSON record for each patch. The container can run offline after the image is built.

Baseline evidence covers test execution, imports, fixture reads, source observers, and inherited dialect settings. The default planner analyzes every file in a patch before assigning a replay route. It can finish source-only decisions immediately, replay compatible function edits in a warm interpreter, or start a fresh interpreter for loading and layout changes.

For module initialization failures, Umbermill combines cold imports with observations from test collection and the running suite. For certain literal changes in the optimizer or executor, it checks the original TPC-DS workload on the recorded platform. The check uses primitive values observed in the preceding tests, the executor test and its worker processes. A changed outcome or an observation the check cannot verify keeps the test on the schedule.

Eligible selections run against a reconstructed patch. The worker tracks failures through setup, subtests, and teardown. The replay policy decides which tests it can afford to run, which results justify a shorter follow-up, and which unresolved candidates still need workers. Related passing tests can inform whether an expensive test needs to run. The scheduler balances the final selection against the two-second cost of opening each worker.

The build prepares evidence from the pinned SQLGlot source and original tests. Runtime checks can use that evidence for statements, loops, tokenizer inputs, and executor work. For experiments, `UMBERMILL_LAZY_IMPORTS=1` defers optional numerical packages in replay children.

## Credits

The test inventory and interchange format come from [Builders](https://www.joinbuilders.org/buildathon). Source verification uses the [pinned SQLGlot revision](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa). The upstream MIT license is retained in the image. Baseline measurements used [pytest](https://pytest.org/) and [coverage.py](https://coverage.readthedocs.io/). See [LICENSE](LICENSE) for this implementation.
