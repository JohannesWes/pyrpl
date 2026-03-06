"""
Analyze the combined CIC + FIR filter cascade in lock_in.v

CIC Decimator configuration (from cic_decimate_by_4096.xci):
  - Number of stages (N): 5
  - Differential delay (M): 2  
  - Decimation rate (R): 4096
  - Input sample rate: 125 MHz
  - Output sample rate: 125 MHz / 4096 = 30.5176 kHz

The FIR filter (fir_lowpass_2000Hz) combines:
  1. Lowpass filtering (2 kHz cutoff)
  2. Inverse-sinc CIC droop compensation

This script plots:
  1. CIC frequency response (showing passband droop)
  2. FIR frequency response
  3. Combined CIC + FIR response (should be flat in passband)
"""

import re
import numpy as np
from pathlib import Path

# CIC parameters (from XCI)
CIC_STAGES = 5       # N
CIC_DELAY = 2        # M (differential delay)
CIC_DECIMATION = 4096  # R

# Sample rates
FS_IN = 125e6                    # CIC input: 125 MHz
FS_OUT = FS_IN / CIC_DECIMATION  # CIC output / FIR input: ~30.5 kHz

SCRIPT_DIR = Path(__file__).parent
COE_PATH = SCRIPT_DIR / "fir_filter_coefs" / "fir_lowpass_2000Hz.coe"
OUT_PNG = SCRIPT_DIR / "fir_lowpass_2000Hz_cic_cascade.png"


def load_coe_int_taps(path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"coefdata\s*=\s*(.*?)\s*;", text, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        raise RuntimeError(f"Could not find coefdata block in {path}")
    nums = re.findall(r"[-+]?\d+", m.group(1))
    return np.array([int(x) for x in nums], dtype=np.float64)


def cic_response(f, fs_in, N, M, R):
    """
    Compute CIC decimator frequency response.
    
    H_CIC(f) = [sin(pi * M * f / fs_out) / sin(pi * f / fs_in)]^N
    
    where fs_out = fs_in / R
    
    At f=0, use L'Hopital: H_CIC(0) = (M * R)^N
    """
    fs_out = fs_in / R
    H = np.zeros_like(f, dtype=np.float64)
    
    for i, freq in enumerate(f):
        if np.isclose(freq, 0):
            H[i] = float((M * R) ** N)
        else:
            num_arg = np.pi * M * freq / fs_out
            den_arg = np.pi * freq / fs_in
            num = np.sin(num_arg)
            den = np.sin(den_arg)
            if np.abs(den) < 1e-15:
                H[i] = 0.0
            else:
                H[i] = (num / den) ** N
    
    return H


def main():
    print("=" * 70)
    print("CIC + FIR Cascade Analysis (lock_in.v)")
    print("=" * 70)
    
    print(f"\nCIC Decimator Configuration:")
    print(f"  Stages (N):           {CIC_STAGES}")
    print(f"  Differential delay:   {CIC_DELAY}")
    print(f"  Decimation rate (R):  {CIC_DECIMATION}")
    print(f"  Input sample rate:    {FS_IN/1e6:.3f} MHz")
    print(f"  Output sample rate:   {FS_OUT:.3f} Hz ({FS_OUT/1e3:.3f} kHz)")
    
    # Load FIR coefficients
    h_fir = load_coe_int_taps(COE_PATH)
    print(f"\nFIR Filter: {len(h_fir)} taps")
    
    # Create frequency vector (0 to Nyquist of decimated rate)
    nfft = 65536
    f = np.linspace(0, FS_OUT / 2, nfft)
    
    # === CIC Response ===
    H_cic = cic_response(f, FS_IN, CIC_STAGES, CIC_DELAY, CIC_DECIMATION)
    H_cic_normalized = H_cic / H_cic[0]  # Normalize to 0 dB at DC
    H_cic_db = 20 * np.log10(np.abs(H_cic_normalized) + 1e-300)
    
    # === FIR Response ===
    # Use freqz-style computation
    H_fir = np.zeros(len(f), dtype=complex)
    for i, freq in enumerate(f):
        # H(e^jw) = sum(h[n] * e^(-j*w*n))
        w = 2 * np.pi * freq / FS_OUT
        n = np.arange(len(h_fir))
        H_fir[i] = np.sum(h_fir * np.exp(-1j * w * n))
    
    H_fir_normalized = H_fir / H_fir[0]  # Normalize to 0 dB at DC
    H_fir_db = 20 * np.log10(np.abs(H_fir_normalized) + 1e-300)
    
    # === Combined Response ===
    H_combined = H_cic_normalized * H_fir_normalized
    H_combined_db = 20 * np.log10(np.abs(H_combined) + 1e-300)
    
    # Find -3 dB points
    idx_fir = np.where(H_fir_db <= -3)[0]
    fc_fir = f[idx_fir[0]] if len(idx_fir) else f[-1]
    
    idx_combined = np.where(H_combined_db <= -3)[0]
    fc_combined = f[idx_combined[0]] if len(idx_combined) else f[-1]
    
    # CIC droop at 2 kHz
    idx_2k = np.argmin(np.abs(f - 2000))
    cic_droop_2k = H_cic_db[idx_2k]
    fir_boost_2k = H_fir_db[idx_2k]
    combined_2k = H_combined_db[idx_2k]
    
    print(f"\nAt 2 kHz:")
    print(f"  CIC droop:       {cic_droop_2k:.2f} dB")
    print(f"  FIR boost:       {fir_boost_2k:+.2f} dB")
    print(f"  Combined:        {combined_2k:+.2f} dB")
    
    print(f"\n-3 dB cutoff:")
    print(f"  FIR alone:       {fc_fir:.1f} Hz")
    print(f"  CIC + FIR:       {fc_combined:.1f} Hz")
    
    # === Plot ===
    print("\nGenerating plot...")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    
    # --- Top Left: Full range ---
    ax1 = axes[0, 0]
    ax1.plot(f/1e3, H_cic_db, 'r-', lw=1.5, label='CIC (sinc^5 droop)', alpha=0.8)
    ax1.plot(f/1e3, H_fir_db, 'b-', lw=1.5, label='FIR (LP + compensation)', alpha=0.8)
    ax1.plot(f/1e3, H_combined_db, 'g-', lw=2, label='Combined (CIC + FIR)')
    ax1.axhline(-3, color='gray', ls='--', lw=1, alpha=0.5)
    ax1.set_xlabel('Frequency (kHz)', fontsize=11)
    ax1.set_ylabel('Magnitude (dB)', fontsize=11)
    ax1.set_title('Full Frequency Range (0 to Nyquist)', fontsize=12, fontweight='bold')
    ax1.set_xlim(0, FS_OUT/2e3)
    ax1.set_ylim(-80, 10)
    ax1.legend(loc='upper right', fontsize=9)
    ax1.grid(True, alpha=0.3)
    
    # --- Top Right: Passband zoom ---
    ax2 = axes[0, 1]
    ax2.plot(f, H_cic_db, 'r-', lw=1.5, label='CIC droop', alpha=0.8)
    ax2.plot(f, H_fir_db, 'b-', lw=1.5, label='FIR (includes inv-sinc)', alpha=0.8)
    ax2.plot(f, H_combined_db, 'g-', lw=2.5, label='Combined (flat!)')
    ax2.axhline(-3, color='gray', ls='--', lw=1, alpha=0.5, label='-3 dB')
    ax2.axhline(0, color='gray', ls='-', lw=0.5, alpha=0.5)
    ax2.axvline(2000, color='purple', ls=':', lw=1, alpha=0.7, label='2 kHz')
    ax2.set_xlabel('Frequency (Hz)', fontsize=11)
    ax2.set_ylabel('Magnitude (dB)', fontsize=11)
    ax2.set_title('Passband Detail (0-3 kHz)', fontsize=12, fontweight='bold')
    ax2.set_xlim(0, 3000)
    ax2.set_ylim(-10, 5)
    ax2.legend(loc='lower left', fontsize=9)
    ax2.grid(True, alpha=0.3)
    
    # Annotate droop compensation
    ax2.annotate(f'CIC droop: {cic_droop_2k:.1f} dB', 
                 xy=(2000, cic_droop_2k), xytext=(2200, cic_droop_2k-2),
                 fontsize=9, color='red',
                 arrowprops=dict(arrowstyle='->', color='red', lw=0.8))
    ax2.annotate(f'FIR boost: {fir_boost_2k:+.1f} dB', 
                 xy=(2000, fir_boost_2k), xytext=(2200, fir_boost_2k+1.5),
                 fontsize=9, color='blue',
                 arrowprops=dict(arrowstyle='->', color='blue', lw=0.8))
    
    # --- Bottom Left: CIC response detail ---
    ax3 = axes[1, 0]
    # Show theoretical vs actual CIC
    ax3.plot(f, H_cic_db, 'r-', lw=2, label=f'CIC: sinc^{CIC_STAGES}, M={CIC_DELAY}, R={CIC_DECIMATION}')
    ax3.axhline(-3, color='gray', ls='--', lw=1, alpha=0.5)
    ax3.set_xlabel('Frequency (Hz)', fontsize=11)
    ax3.set_ylabel('Magnitude (dB)', fontsize=11)
    ax3.set_title(f'CIC Decimator Response (N={CIC_STAGES}, M={CIC_DELAY}, R={CIC_DECIMATION})', 
                  fontsize=12, fontweight='bold')
    ax3.set_xlim(0, 5000)
    ax3.set_ylim(-15, 2)
    ax3.legend(loc='lower left', fontsize=9)
    ax3.grid(True, alpha=0.3)
    
    # Mark key frequencies
    for freq_hz in [500, 1000, 2000, 3000, 5000]:
        if freq_hz <= 5000:
            idx = np.argmin(np.abs(f - freq_hz))
            droop = H_cic_db[idx]
            ax3.plot(freq_hz, droop, 'ro', markersize=5)
            ax3.annotate(f'{droop:.1f}dB', xy=(freq_hz, droop), 
                        xytext=(freq_hz+100, droop+0.8), fontsize=8)
    
    # --- Bottom Right: Phase response ---
    ax4 = axes[1, 1]
    
    phase_fir = np.unwrap(np.angle(H_fir))
    phase_cic = np.unwrap(np.angle(H_cic_normalized))
    phase_combined = np.unwrap(np.angle(H_combined))
    
    ax4.plot(f/1e3, phase_fir, 'b-', lw=1.5, label='FIR phase', alpha=0.8)
    ax4.plot(f/1e3, phase_combined, 'g-', lw=2, label='Combined phase')
    ax4.set_xlabel('Frequency (kHz)', fontsize=11)
    ax4.set_ylabel('Phase (rad)', fontsize=11)
    ax4.set_title('Phase Response', fontsize=12, fontweight='bold')
    ax4.set_xlim(0, FS_OUT/2e3)
    ax4.legend(loc='upper right', fontsize=9)
    ax4.grid(True, alpha=0.3)
    
    fig.suptitle('lock_in.v: CIC Decimator + FIR Compensation Filter Analysis\n' +
                 f'CIC: sinc^{CIC_STAGES} (M={CIC_DELAY}, R={CIC_DECIMATION}) | ' +
                 f'FIR: {len(h_fir)} taps (LP + inv-sinc compensation)',
                 fontsize=13, fontweight='bold', y=0.995)
    
    plt.tight_layout()
    fig.savefig(OUT_PNG, dpi=170, bbox_inches='tight')
    
    print(f"\nSaved: {OUT_PNG}")
    print("=" * 70)


if __name__ == "__main__":
    main()
