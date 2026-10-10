# Timing evidence and implementation

The app distinguishes display scale, sample spacing within a capture, and host transfer time.

Since TraceForge 0.5.2, acquisition keeps all eight inputs running. Channel switches change visibility only, preserving the acquisition clock and recording continuity. Current nominal rates are 440 Sa/s per channel in rolling mode and at most 300 kSa/s per channel in fast mode. The lower-channel-count rates and padding behavior below describe the driver and earlier selective-acquisition releases; hiding traces does not activate those faster rates.

## Primary references

- Hantek's [1008B product specifications](https://www.hantek.com/m/productDetail/13169) list the timebase controls and a maximum 2.4 MSa/s sampling rate.
- Hantek's [1008 series manual](https://www.hantek.com/product/hantek1008/hantek1008_manual.pdf), Appendix A, identifies 2.4 MSa/s as the single-channel acquisition rate. A small selectable time/div value is not the same as an equally small sample interval.
- The user's Scope1008 1.0.30 installer supplies `DefaultComponent/HTNTLVDll.dll`; it was inspected statically. The Windows executable was not run for this implementation.

## Vendor binary evidence

In `HTNTLVDll.dll` (preferred image base `0x10000000`), `dsoSetSampleRate` at `0x10007860` sends A3 followed by the timebase ID. The open-source driver uses the same 1–2–5 index mapping, so ID `0x0F` corresponds to 100 µs/div.

`dsoSetHTrigPos`, starting at `0x10006CC0`, branches at `0x10006D51` on a timebase ID of at most `0x0F`. Its fastest branch uses the double at `0x100453A0`, whose value is **0.4166666666666667**. This is consistent with a 2.4 MHz conversion clock expressed in microseconds. Slower branches use larger intervals (for example, 0.8750001093750137 and 1.25). The function also accounts for channel count when arranging the sample buffer. This is evidence for the nominal clock interpretation, not a direct clock measurement.

`dsoSetCHNum` at `0x10005e50` rounds 3/5/7 channels up to an even count when the timebase ID is below `0x18`. `dsoSetCHOpen` at `0x10005f10` enables the first unused physical input as padding and stores its identity. The Linux app follows that scan layout, discards the padding channel's data and records the actual selected channel order separately.

The slot periods read from the DLL are:

| A3 ID | Hardware time/div | Conversion slot, µs | DLL address |
|---|---|---:|---|
| 0x0F | 100 µs | 0.4166666666666667 | 0x100453A0 |
| 0x10 | 200 µs | 0.8750001093750137 | 0x100453B8 |
| 0x11 | 500 µs | 1.25 | 0x100453C8 |
| 0x12 | 1 ms | 2.5 | 0x100453E8 |
| 0x13 | 2 ms | 5.0 | 0x10045400 |
| 0x14 | 5 ms | 13.333333333333334 | 0x10045420 |
| 0x15 | 10 ms | 27.000027000027 | 0x10045458 |
| 0x16 | 20 ms | 55.94405594405595 | 0x10045470 |
| 0x17 | 50 ms | 125.99218848431397 | 0x10045498 |

The per-channel sample interval is the slot period multiplied by the scan count, including padding. The maximum nominal rate is therefore 2.4 MSa/s for one input, 1.2 MSa/s for two, 600 kSa/s for 3–4, 400 kSa/s for 5–6, and 300 kSa/s for 7–8. Display choices below 100 µs/div zoom a frame captured using the 0x0F setting. No extra samples are synthesized.

The AC command now follows the vendor's 50% capture-window calculation: the two-byte position uses the nominal buffer size divided by scan count, and pre/post delays use the chosen conversion-slot period. This establishes buffer configuration, not independently verified triggering. Fast packets are still kept as separate frames; no host transfer time is inserted into their sample coordinates.

Live tests established that a six-slot scan returns a 4,000-value buffer: four channels have 667 values and two have 666. Only the first 666 full scans are retained. The final four values are discarded, with that policy and count stored in metadata. Other unexpected uneven frame lengths remain errors.

At 100 ms/div and longer the app uses rolling mode. The base rate is nominal 440 Sa/s/ch at eight channels. It uses the upstream driver's approximate factors for 1–8 inputs: 4.56, 3.03, 2.27, 1.82, 1.51, 1.3, 1.14, 1.0. A rate inferred from USB delivery intervals is not substituted for a calibrated device clock.

## Display and recording behavior

All 25 display choices use ten divisions. The range is 1 µs/div through 1 min/div, with 1–2–5 steps and a final 60-second division. Axis units and cursor differences use µs, ms, s or min. Coordinates remain seconds internally; exports retain nominal seconds, and fast exports also provide microseconds within the frame. The rolling buffer holds 600 nominal seconds at the full received rate.

Changing acquisition mode or the hardware clock finalizes an active recording, reconnects and starts a new named part if recording was active. This prevents a single file from mixing rates. Time coordinates restart for the new acquisition; no continuity across that reconnect is claimed. Channel visibility and vertical display offsets do not reconfigure the scope or split recordings. Offsets shift plotted Y coordinates only; sample values and all time coordinates remain unchanged.

Fast-frame transfers, sparse channel layouts, recording boundaries and all time controls are checked in the application integration tests. These checks do not establish absolute timebase accuracy. A battery can check voltage and polarity; an independently known-frequency source is still needed to validate the clock.
