"""Compare both selectable 2 kHz FIRs, alone and after the lock-in CIC.

The CIC configuration is read from ``cic_decimate_by_4096.xci`` manually:

* five stages
* differential delay two
* decimation by 4096
* 125 MHz input sample rate

Both FIRs receive the same CIC output in ``lock_in.v``.  The cascade magnitude
therefore differs only through their FIR coefficient sets.  The phase plot
shows FIR phase only because this analytical CIC calculation models magnitude;
the omitted CIC phase/delay is common to both selectable paths.  The step and
group-delay panels likewise compare the selectable FIR sections directly.
"""

import re
from pathlib import Path

import numpy as np


CIC_STAGES = 5
CIC_DELAY = 2
CIC_DECIMATION = 4096

FS_IN = 125e6
FS_OUT = FS_IN / CIC_DECIMATION

SCRIPT_DIR = Path(__file__).parent
COE_PATHS = {
    "Minimum phase (register 0)": (
        SCRIPT_DIR / "fir_filter_coefs" / "fir_lowpass_2000Hz.coe"
    ),
    "Linear phase (register 1)": (
        SCRIPT_DIR / "fir_filter_coefs" / "fir_linear_phase_2000Hz.coe"
    ),
}
OUT_PNG = SCRIPT_DIR / "fir_2kHz_cic_cascade_comparison.png"


def load_coe_int_taps(path):
    """Load the decimal integer tap vector from a Xilinx COE file."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(
        r"coefdata\s*=\s*(.*?)\s*;", text, flags=re.IGNORECASE | re.DOTALL
    )
    if not match:
        raise RuntimeError(f"Could not find coefdata block in {path}")
    return np.array(
        [int(value) for value in re.findall(r"[-+]?\d+", match.group(1))],
        dtype=np.float64,
    )


def cic_response(frequency):
    """Return the normalized CIC-decimator magnitude response."""
    response = np.empty_like(frequency, dtype=np.float64)
    zero = np.isclose(frequency, 0)
    response[zero] = float((CIC_DELAY * CIC_DECIMATION) ** CIC_STAGES)

    numerator = np.sin(np.pi * CIC_DELAY * frequency[~zero] / FS_OUT)
    denominator = np.sin(np.pi * frequency[~zero] / FS_IN)
    response[~zero] = (numerator / denominator) ** CIC_STAGES
    return response / response[0]


def fir_response(taps, nfft):
    """Return the FIR response on the positive-frequency FFT grid."""
    response = np.fft.rfft(taps, n=nfft)
    return response / response[0]


def db(response):
    return 20 * np.log10(np.maximum(np.abs(response), 1e-300))


def first_crossing(frequency, magnitude_db, threshold=-3):
    indices = np.flatnonzero(magnitude_db <= threshold)
    return frequency[indices[0]] if indices.size else frequency[-1]


def main():
    print("=" * 72)
    print("Selectable 2 kHz FIR comparison, including the common CIC cascade")
    print("=" * 72)
    print(f"CIC: N={CIC_STAGES}, M={CIC_DELAY}, R={CIC_DECIMATION}")
    print(f"Sample rates: {FS_IN / 1e6:.3f} MHz -> {FS_OUT / 1e3:.3f} kHz")

    nfft = 131072
    frequency = np.fft.rfftfreq(nfft, d=1 / FS_OUT)
    angular_frequency = 2 * np.pi * frequency / FS_OUT
    cic = cic_response(frequency)
    cic_db = db(cic)

    responses = {}
    for label, path in COE_PATHS.items():
        taps = load_coe_int_taps(path)
        fir = fir_response(taps, nfft)
        combined = cic * fir
        phase_rad = np.unwrap(np.angle(fir))
        step = np.cumsum(taps) / np.sum(taps)
        responses[label] = {
            "taps": taps,
            "fir": fir,
            "fir_db": db(fir),
            "combined": combined,
            "combined_db": db(combined),
            "phase_deg": np.rad2deg(phase_rad),
            "group_delay_samples": -np.gradient(phase_rad, angular_frequency),
            "step": step,
            "step_time_ms": np.arange(len(taps)) / FS_OUT * 1e3,
        }
        print(f"{label}: {len(taps)} taps from {path.name}")

    index_2k = np.argmin(np.abs(frequency - 2000))
    print(f"\nAt {frequency[index_2k]:.2f} Hz:")
    print(f"  CIC magnitude: {cic_db[index_2k]:+.3f} dB")
    for label, response in responses.items():
        print(
            f"  {label}: FIR {response['fir_db'][index_2k]:+.3f} dB, "
            f"cascade {response['combined_db'][index_2k]:+.3f} dB, "
            f"phase {response['phase_deg'][index_2k]:+.1f} deg"
        )
        print(
            f"    -3 dB: FIR "
            f"{first_crossing(frequency, response['fir_db']):.1f} Hz, cascade "
            f"{first_crossing(frequency, response['combined_db']):.1f} Hz"
        )
        step_50_index = np.flatnonzero(response["step"] >= 0.5)[0]
        print(
            f"    group delay: {response['group_delay_samples'][0]:.2f} samples "
            f"at DC, {response['group_delay_samples'][index_2k]:.2f} samples "
            f"at 2 kHz; step 50% at {response['step_time_ms'][step_50_index]:.3f} ms, "
            f"peak {np.max(response['step']):.3f}"
        )

    labels = list(responses)
    passband = frequency <= 2000
    fir_magnitude_delta = np.max(
        np.abs(
            responses[labels[0]]["fir_db"][passband]
            - responses[labels[1]]["fir_db"][passband]
        )
    )
    print(f"\nMaximum FIR magnitude mismatch from 0 to 2 kHz: "
          f"{fir_magnitude_delta:.8f} dB")

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {
        labels[0]: "#d97706",
        labels[1]: "#0284c7",
    }
    styles = {labels[0]: "-", labels[1]: "--"}
    figure, axes = plt.subplots(3, 2, figsize=(14, 13.5))

    full_axis = axes[0, 0]
    full_axis.plot(
        frequency / 1e3,
        cic_db,
        color="0.45",
        linestyle=":",
        linewidth=1.4,
        label="CIC magnitude",
    )
    for label, response in responses.items():
        full_axis.plot(
            frequency / 1e3,
            response["combined_db"],
            color=colors[label],
            linestyle=styles[label],
            linewidth=2,
            label=f"CIC + {label}",
        )
    full_axis.set(
        title="Cascade magnitude: full decimated Nyquist range",
        xlabel="Frequency (kHz)",
        ylabel="Magnitude (dB)",
        xlim=(0, FS_OUT / 2e3),
        ylim=(-100, 3),
    )
    full_axis.legend(loc="upper right", fontsize=8)
    full_axis.grid(True, alpha=0.3)

    cascade_axis = axes[0, 1]
    cascade_axis.plot(
        frequency,
        cic_db,
        color="0.45",
        linestyle=":",
        linewidth=1.4,
        label="CIC magnitude",
    )
    for label, response in responses.items():
        cascade_axis.plot(
            frequency,
            response["combined_db"],
            color=colors[label],
            linestyle=styles[label],
            linewidth=2,
            label=f"CIC + {label}",
        )
    cascade_axis.axvline(2000, color="0.4", linestyle=":", linewidth=1)
    cascade_axis.axvline(2500, color="0.4", linestyle=":", linewidth=1)
    cascade_axis.set(
        title="Cascade magnitude: passband and transition",
        xlabel="Frequency (Hz)",
        ylabel="Magnitude (dB)",
        xlim=(0, 3000),
        ylim=(-25, 2),
    )
    cascade_axis.legend(loc="lower left", fontsize=8)
    cascade_axis.grid(True, alpha=0.3)

    fir_axis = axes[1, 0]
    for label, response in responses.items():
        fir_axis.plot(
            frequency,
            response["fir_db"],
            color=colors[label],
            linestyle=styles[label],
            linewidth=2,
            label=label,
        )
    fir_axis.axvline(2000, color="0.4", linestyle=":", linewidth=1)
    fir_axis.axvline(2500, color="0.4", linestyle=":", linewidth=1)
    fir_axis.set(
        title="FIR magnitude comparison",
        xlabel="Frequency (Hz)",
        ylabel="Magnitude (dB, normalized at DC)",
        xlim=(0, 5000),
        ylim=(-80, 2),
    )
    fir_axis.legend(loc="lower left", fontsize=8)
    fir_axis.grid(True, alpha=0.3)

    phase_axis = axes[1, 1]
    phase_range = frequency <= 2500
    for label, response in responses.items():
        phase_axis.plot(
            frequency[phase_range],
            response["phase_deg"][phase_range],
            color=colors[label],
            linestyle=styles[label],
            linewidth=2,
            label=label,
        )
    phase_axis.axvline(2000, color="0.4", linestyle=":", linewidth=1)
    phase_axis.set(
        title="FIR phase (common CIC phase/delay excluded)",
        xlabel="Frequency (Hz)",
        ylabel="Unwrapped phase (degrees)",
        xlim=(0, 2500),
    )
    phase_axis.legend(loc="lower left", fontsize=8)
    phase_axis.grid(True, alpha=0.3)

    step_axis = axes[2, 0]
    for label, response in responses.items():
        step_axis.plot(
            response["step_time_ms"],
            response["step"],
            color=colors[label],
            linestyle=styles[label],
            linewidth=2,
            label=label,
        )
    step_axis.axhline(1, color="0.4", linestyle=":", linewidth=1)
    step_axis.set(
        title="Normalized FIR step response",
        xlabel="Time after input step (ms)",
        ylabel="Normalized output",
        xlim=(0, (len(responses[labels[0]]["taps"]) - 1) / FS_OUT * 1e3),
        ylim=(-0.15, 1.28),
    )
    step_axis.legend(loc="lower right", fontsize=8)
    step_axis.grid(True, alpha=0.3)

    delay_axis = axes[2, 1]
    delay_range = frequency <= 2500
    for label, response in responses.items():
        delay_axis.plot(
            frequency[delay_range],
            response["group_delay_samples"][delay_range],
            color=colors[label],
            linestyle=styles[label],
            linewidth=2,
            label=label,
        )
    delay_axis.axvline(2000, color="0.4", linestyle=":", linewidth=1)
    delay_axis.set(
        title="FIR group delay (common CIC delay excluded)",
        xlabel="Frequency (Hz)",
        ylabel="Group delay (output samples)",
        xlim=(0, 2500),
        ylim=(0, 65),
    )
    delay_axis.secondary_yaxis(
        "right",
        functions=(lambda samples: samples / FS_OUT * 1e3,
                   lambda milliseconds: milliseconds * FS_OUT / 1e3),
    ).set_ylabel("Group delay (ms)")
    delay_axis.legend(loc="lower right", fontsize=8)
    delay_axis.grid(True, alpha=0.3)

    figure.suptitle(
        "lock_in.v selectable 2 kHz FIR filters: frequency and time response",
        fontsize=13,
    )
    figure.tight_layout()
    figure.savefig(OUT_PNG, dpi=170, bbox_inches="tight")
    print(f"\nSaved: {OUT_PNG}")
    print("=" * 72)


if __name__ == "__main__":
    main()
