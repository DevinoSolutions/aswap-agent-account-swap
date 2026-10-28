A backup root as aswap wrote it before `state.db`: one JSON file per document.

`tests/e2e/test_state.py` copies it into a fresh HOME and checks that the real
binary imports every file into `state.db`. The tokens are fake; the e2e fake
server registers them (see `LEGACY` in that test).
