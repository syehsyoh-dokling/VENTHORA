# Original engines (vendored, unmodified)

These files are copied byte-for-byte from the author's earlier systems. VENTHORA calls them
through thin adapters in `src/venthora/engines/` and never edits them;
`tests/test_originals.py` fails if any hash below changes.

| Engine | File here | Original source (local, 23 Oct 2026) | SHA-256 |
|---|---|---|---|
| Theorem: quant toolkit, FX arithmetic | `theorem_qf/fx.py` | `Agenthon-2026/submissions/2026-10-13/t1_know86_ctx/qf_toolkit/fx.py` | `05b99f2b2f24cdce9882b29b2d2a65c021de87a43c63b092a5c1cc723439c101` |
| Horizon: M0 scenario engine (trailing-window drift + covariance, seeded joint paths) | `horizon_core/m0_exact.py` | `Agenthon-2026/t2work/sim/agent_gate/forecast_core/m0_exact.py` (newscalegate, 22 Oct 2026) | `aeae14d1f7ed0613d1e59c36f7d89214839dc65b2d7e090c430d36e555d57021` |
| Verity: corpus with date embargo | `verity_core/corpus.py` | `Agenthon-2026/t4work/v21/agent/analysis_core/corpus.py` (v21, 22 Oct 2026) | `1542685578620866f3b2f37dc4c36793a65c927076163ee4b2d321b1b94be4be` |
| Verity: BM25 sentence retriever | `verity_core/retriever.py` | `Agenthon-2026/t4work/v21/agent/analysis_core/retriever.py` | `ed13b66a3b34f2917ac262df526ca04e1fcbfd44b8d79ba0b87c29fb3a894050` |
| Verity: citation validator | `verity_core/validators.py` | `Agenthon-2026/t4work/v21/agent/analysis_core/validators.py` | `3677da639c553e16795bb57446e14bcd7f8d4ffd81f13a27194076e3e80614e6` |

SeismoForge (`github.com/syehsyoh-dokling/SeismoForge`) contributes its evidence-gate
architecture, which is re-implemented for finance as ProofCore. Its OpenSees physics layer has no
role in treasury and is not vendored.

What each one does inside VENTHORA:

- **Theorem `fx.net_currency_exposure`** independently nets every planned conversion leg. ProofCore
  requires that net to equal the projected currency shortfalls.
- **Horizon `m0_exact.draws`** generates seeded joint paths for EUR/USD and GBP/USD from ECB history.
  The p95 adverse 3-business-day move becomes the FX stress buffer.
- **Verity `corpus` + `retriever` + `validators`** index the inbox, find the passage behind a
  claim the language model extracted, and refuse the claim unless the cited span carries it and the
  document predates the embargo.

Limit carried over from the original: Verity's citation check measures term overlap
(`MIN_SPAN_OVERLAP = 0.6`), not entailment. VENTHORA therefore also requires the extracted delay to
appear literally in the cited span (see `evidence/reader.py`).
