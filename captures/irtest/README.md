Synthetic test IRs, and what Torpedo Remote 5.4.32 uploaded for them
(extracted from `../2026-10-15-ir-upload.log`, saved with `ir.write_wav`):

- `REMOTE-44K1.wav`: `IRTEST-44K1.wav`, 20 ms, Align "Original" (resampled to 48 kHz).
- `REMOTE-48K-MINPHASE.wav`: `IRTEST-48K.wav`, 20 ms, Align "Min phase".

`tests/test_ir.py` checks `ir.prepare` against them.
