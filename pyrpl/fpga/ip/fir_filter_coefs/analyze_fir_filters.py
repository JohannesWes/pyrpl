"""
FIR Filter Coefficient Analysis Script

Reads the .coe coefficient files and computes/plots the frequency response
to verify each filter has a different cutoff frequency.

Usage:
    python analyze_fir_filters.py
"""

import numpy as np
import matplotlib.pyplot as plt
from scipy import signal
from pathlib import Path


def load_coe_file(filepath):
    """Load coefficients from a Xilinx .coe file."""
    coefficients = []
    reading_data = False
    
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            
            # Skip empty lines and comments
            if not line or line.startswith(';'):
                continue
            
            # Check for coefdata start
            if 'coefdata' in line.lower():
                reading_data = True
                # Check if there's data on the same line after =
                if '=' in line:
                    parts = line.split('=')
                    if len(parts) > 1 and parts[1].strip():
                        data_part = parts[1].strip().rstrip(',;')
                        if data_part:
                            try:
                                coefficients.append(int(data_part))
                            except ValueError:
                                pass
                continue
            
            # Read coefficient data
            if reading_data:
                # Remove trailing comma or semicolon
                value = line.rstrip(',;').strip()
                if value:
                    try:
                        coefficients.append(int(value))
                    except ValueError:
                        pass
    
    return np.array(coefficients, dtype=np.float64)


def compute_frequency_response(coefficients, sample_rate, num_points=1024):
    """Compute the frequency response of the FIR filter."""
    # Normalize coefficients (optional - for comparison)
    coef_norm = coefficients / np.max(np.abs(coefficients))
    
    # Compute frequency response
    w, h = signal.freqz(coef_norm, worN=num_points, fs=sample_rate)
    
    # Convert to dB
    h_db = 20 * np.log10(np.abs(h) + 1e-10)  # Adding small value to avoid log(0)
    
    return w, h_db


def find_cutoff_frequency(frequencies, response_db, threshold_db=-3):
    """Find the -3dB cutoff frequency."""
    # Normalize to 0 dB at DC
    response_norm = response_db - response_db[0]
    
    # Find first crossing of threshold
    for i, (f, db) in enumerate(zip(frequencies, response_norm)):
        if db < threshold_db:
            # Interpolate for more accurate result
            if i > 0:
                f_prev = frequencies[i-1]
                db_prev = response_norm[i-1]
                # Linear interpolation
                slope = (db - db_prev) / (f - f_prev)
                f_cutoff = f_prev + (threshold_db - db_prev) / slope
                return f_cutoff
            return f
    
    return frequencies[-1]  # If never crosses threshold


def main():
    # Path to coefficient files
    coef_dir = Path(r"C:\Users\aj92uwef\PycharmProjects\pyrpl_new\pyrpl\fpga\ip\fir_filter_coefs")
    
    # Sample rate after CIC decimation (125 MHz / 4096)
    sample_rate = 125e6 / 4096  # ~30.517 kHz
    
    print("=" * 70)
    print("FIR Filter Coefficient Analysis")
    print("=" * 70)
    print(f"\nSample rate after CIC decimation: {sample_rate:.3f} Hz")
    print(f"Nyquist frequency: {sample_rate/2:.3f} Hz\n")
    
    # Find all .coe files
    coe_files = sorted(coef_dir.glob("*.coe"))
    
    if not coe_files:
        print(f"ERROR: No .coe files found in {coef_dir}")
        return
    
    # Store results for plotting
    filters = {}
    
    print("-" * 70)
    print(f"{'Filter Name':<25} {'Num Taps':>10} {'-3dB Cutoff (Hz)':>18} {'Status':<15}")
    print("-" * 70)
    
    for coe_file in coe_files:
        name = coe_file.stem
        coefficients = load_coe_file(coe_file)
        
        if len(coefficients) == 0:
            print(f"{name:<25} {'N/A':>10} {'ERROR':>18} {'Failed to load':<15}")
            continue
        
        # Compute frequency response
        freq, response_db = compute_frequency_response(coefficients, sample_rate)
        
        # Find -3dB cutoff
        cutoff = find_cutoff_frequency(freq, response_db)
        
        # Determine expected cutoff from filename
        if '500' in name:
            expected = 500
        elif '2000' in name:
            expected = 2000
        elif '5000' in name:
            expected = 5000
        else:
            expected = None
        
        # Check if cutoff is approximately correct
        if expected:
            error_pct = abs(cutoff - expected) / expected * 100
            if error_pct < 30:  # Within 30% tolerance
                status = f"OK ({error_pct:.0f}% err)"
            else:
                status = f"MISMATCH!"
        else:
            status = "Unknown"
        
        print(f"{name:<25} {len(coefficients):>10} {cutoff:>18.1f} {status:<15}")
        
        filters[name] = {
            'coefficients': coefficients,
            'frequencies': freq,
            'response_db': response_db,
            'cutoff': cutoff
        }
    
    print("-" * 70)
    
    # Check if filters are actually different
    print("\n" + "=" * 70)
    print("Coefficient Comparison")
    print("=" * 70)
    
    names = list(filters.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            name1, name2 = names[i], names[j]
            coef1 = filters[name1]['coefficients']
            coef2 = filters[name2]['coefficients']
            
            # Check if identical
            if len(coef1) == len(coef2):
                if np.allclose(coef1, coef2):
                    print(f"WARNING: {name1} and {name2} have IDENTICAL coefficients!")
                else:
                    diff = np.sum(np.abs(coef1 - coef2))
                    print(f"OK: {name1} vs {name2}: DIFFERENT (total abs diff: {diff:.0f})")
            else:
                print(f"OK: {name1} vs {name2}: DIFFERENT (tap counts: {len(coef1)} vs {len(coef2)})")
    
    # Plot frequency responses
    print("\n" + "=" * 70)
    print("Generating frequency response plot...")
    print("=" * 70)
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10))
    
    colors = {'fir_lowpass_500Hz': 'blue', 
              'fir_lowpass_2000Hz': 'green', 
              'fir_lowpass_5000Hz': 'red'}
    
    for name, data in filters.items():
        color = colors.get(name, 'gray')
        label = name.replace('fir_lowpass_', '').replace('Hz', ' Hz')
        
        # Plot full response
        ax1.plot(data['frequencies'], data['response_db'], 
                 label=f"{label} (cutoff: {data['cutoff']:.0f} Hz)",
                 color=color, linewidth=2)
        
        # Plot zoomed view (0-3000 Hz)
        mask = data['frequencies'] <= 3000
        ax2.plot(data['frequencies'][mask], data['response_db'][mask], 
                 label=f"{label} (cutoff: {data['cutoff']:.0f} Hz)",
                 color=color, linewidth=2)
    
    # Configure full view plot
    ax1.set_xlabel('Frequency (Hz)', fontsize=12)
    ax1.set_ylabel('Magnitude (dB)', fontsize=12)
    ax1.set_title('FIR Filter Frequency Response (Full Range)', fontsize=14)
    ax1.axhline(y=-3, color='gray', linestyle='--', alpha=0.7, label='-3 dB line')
    ax1.legend(loc='best')
    ax1.grid(True, alpha=0.3)
    ax1.set_xlim([0, sample_rate / 2])
    ax1.set_ylim([-60, 5])
    
    # Configure zoomed view plot
    ax2.set_xlabel('Frequency (Hz)', fontsize=12)
    ax2.set_ylabel('Magnitude (dB)', fontsize=12)
    ax2.set_title('FIR Filter Frequency Response (Zoomed: 0-3000 Hz)', fontsize=14)
    ax2.axhline(y=-3, color='gray', linestyle='--', alpha=0.7, label='-3 dB line')
    for cutoff in [500, 2000, 5000]:
        ax2.axvline(x=cutoff, color='gray', linestyle=':', alpha=0.5)
    ax2.legend(loc='best')
    ax2.grid(True, alpha=0.3)
    ax2.set_xlim([0, 3000])
    ax2.set_ylim([-40, 5])
    
    plt.tight_layout()
    
    # Save plot
    output_path = coef_dir / 'filter_analysis.png'
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"\nPlot saved to: {output_path}")
    
    plt.show()


if __name__ == "__main__":
    main()
