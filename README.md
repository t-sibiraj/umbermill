# Umbermill

Umbermill selects Python tests from an independent patch and a baseline dependency map. It reads the test inventory and patches from `/data` and writes a JSON record for each patch. The container can run offline after the image is built.

The map records baseline test execution, imports, and fixture reads. Tests with incomplete evidence stay eligible. The scheduler balances the selected tests across workers using the supplied timings.

## Credits

The test inventory and interchange format come from [Builders](https://www.joinbuilders.org/buildathon). Source verification uses the [pinned SQLGlot revision](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa). The upstream MIT license is retained in the image. Baseline measurements used [pytest](https://pytest.org/) and [coverage.py](https://coverage.readthedocs.io/). See [LICENSE](LICENSE) for this implementation.
