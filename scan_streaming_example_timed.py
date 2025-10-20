"""
Instrumented streaming example with detailed timing measurements.
This helps identify bottlenecks in the data acquisition pipeline.
"""
import numpy as np
import matplotlib.pyplot as plt
from pyrpl import Pyrpl
import time
from collections import defaultdict

# Timing statistics collector
class TimingStats:
    def __init__(self):
        self.timings = defaultdict(list)
        self.counts = defaultdict(int)
    
    def record(self, category, duration):
        self.timings[category].append(duration)
        self.counts[category] += 1
    
    def report(self):
        print("\n" + "="*70)
        print("TIMING ANALYSIS REPORT")
        print("="*70)
        for category in sorted(self.timings.keys()):
            times = self.timings[category]
            count = self.counts[category]
            if times:
                avg = np.mean(times) * 1000  # convert to ms
                min_t = np.min(times) * 1000
                max_t = np.max(times) * 1000
                total = np.sum(times)
                print(f"\n{category}:")
                print(f"  Count: {count}")
                print(f"  Avg: {avg:.3f} ms")
                print(f"  Min: {min_t:.3f} ms")
                print(f"  Max: {max_t:.3f} ms")
                print(f"  Total: {total:.3f} s")

stats = TimingStats()

# You need to set scan to your actual scan object
# Example: scan = redpitaya_finite_sampling._pyrpl.rp.scan
# For standalone testing, uncomment below:
# p = Pyrpl(config='test_streaming')
# scan = p.rp.scan

print("Starting instrumented data acquisition...")
print("Make sure 'scan' object is defined before running this script!")
print("Example: scan = redpitaya_finite_sampling._pyrpl.rp.scan\n")

# Configure streaming for demodulated data
t0 = time.time()
scan.stream_start(input_source='demod')
stats.record("stream_start", time.time() - t0)

# Acquisition parameters
sample_rate = 125e6 / 4096  # Demodulated data rate: ~30.5 kHz
duration = 0.1  # 100 ms
expected_samples = int(sample_rate * duration)

print(f"Sample rate: {sample_rate:.1f} Hz")
print(f"Expected samples: {expected_samples}")
print(f"BRAM depth: {2**12} samples ({(2**12 / sample_rate * 1000):.1f} ms buffer)")
print(f"Target duration: {duration*1000:.0f} ms\n")

# Collect data - use list to concatenate numpy arrays
all_data = []
start_time = time.time()
total_samples = 0
loop_count = 0
empty_reads = 0
overflow_count = 0

print("Collecting data...\n")
print(f"{'Loop':<6} {'Samples':<8} {'Read(ms)':<10} {'Convert(ms)':<12} {'Status(ms)':<12} {'Sleep(ms)':<10} {'Total(ms)':<10}")
print("-" * 80)

while total_samples < expected_samples:
    loop_start = time.time()
    loop_count += 1
    
    # Time: Read from stream
    t1 = time.time()
    new_data = scan.stream_read(max_samples=4096)
    t_read = time.time() - t1
    stats.record("stream_read_call", t_read)
    
    if len(new_data) > 0:
        # Time: Convert data
        t2 = time.time()
        local_data = np.array(new_data.tolist(), dtype=np.int32)
        t_convert = time.time() - t2
        stats.record("data_conversion", t_convert)
        
        all_data.append(local_data)
        samples_read = len(local_data)
        total_samples += samples_read
        stats.record("samples_per_read", samples_read)
        
        # Time: Check status (to understand overhead)
        t3 = time.time()
        active, overflow, wr_ptr, samples_written = scan.stream_status()
        t_status = time.time() - t3
        stats.record("stream_status_call", t_status)
        
        if overflow:
            overflow_count += 1
            print(f"\n!!! OVERFLOW detected at loop {loop_count} !!!")
        
        # Time: Sleep
        t4 = time.time()
        time.sleep(0.005)  # 5 ms poll interval
        t_sleep = time.time() - t4
        
        loop_duration = time.time() - loop_start
        
        # Print timing for this iteration
        if loop_count % 5 == 0 or samples_read > 100:  # Print every 5th loop or large reads
            print(f"{loop_count:<6} {samples_read:<8} {t_read*1000:<10.3f} {t_convert*1000:<12.3f} "
                  f"{t_status*1000:<12.3f} {t_sleep*1000:<10.3f} {loop_duration*1000:<10.3f}")
    else:
        empty_reads += 1
        stats.record("empty_reads", 1)
        
        # Still sleep on empty reads
        time.sleep(0.005)
    
    # Timeout after 2 seconds (safety)
    if time.time() - start_time > 2.0:
        print("\nTimeout reached!")
        break

# Stop streaming
t_stop = time.time()
scan.stream_stop()
stats.record("stream_stop", time.time() - t_stop)

total_duration = time.time() - start_time

print("\n" + "="*80)
print(f"ACQUISITION SUMMARY")
print("="*80)
print(f"Collected {total_samples} samples in {total_duration:.3f}s")
print(f"Target samples: {expected_samples}")
print(f"Actual sample rate: {total_samples/total_duration:.1f} Hz (expected {sample_rate:.1f} Hz)")
print(f"Total loops: {loop_count}")
print(f"Empty reads: {empty_reads} ({empty_reads/loop_count*100:.1f}%)")
print(f"Overflows detected: {overflow_count}")

# Concatenate all numpy arrays into one
if len(all_data) > 0:
    data = np.concatenate(all_data)
else:
    data = np.array([], dtype=np.int32)
    print("\nWARNING: No data collected!")

# Print detailed timing statistics
stats.report()

# Calculate theoretical limits
print("\n" + "="*70)
print("THEORETICAL ANALYSIS")
print("="*70)
print(f"Data generation rate: {sample_rate:.1f} Hz = {sample_rate * 4 / 1024:.2f} KB/s")
print(f"BRAM buffer duration: {(2**12 / sample_rate):.3f} s = {(2**12 / sample_rate * 1000):.1f} ms")
print(f"Required read frequency: >{sample_rate / 4096:.2f} Hz (to avoid overflow)")
print(f"Actual read frequency: {loop_count / total_duration:.2f} Hz")
print(f"Average samples per read: {total_samples / loop_count:.1f}")
print(f"Average loop duration: {total_duration / loop_count * 1000:.2f} ms")

# Create time axis
if len(data) > 0:
    time_axis = np.arange(len(data)) / sample_rate * 1000  # Convert to milliseconds
    
    # Plot the data
    plt.figure(figsize=(12, 8))
    
    plt.subplot(2, 1, 1)
    plt.plot(time_axis, data, linewidth=0.5)
    plt.xlabel('Time (ms)')
    plt.ylabel('Demodulated Signal (counts)')
    plt.title(f'Scan Module Streaming: {duration*1000:.0f}ms of Demodulated Data')
    plt.grid(True, alpha=0.3)
    
    # Plot timing histogram
    plt.subplot(2, 1, 2)
    read_times = np.array(stats.timings['stream_read_call']) * 1000
    plt.hist(read_times, bins=50, alpha=0.7, label='Read times')
    plt.axvline(np.mean(read_times), color='r', linestyle='--', label=f'Mean: {np.mean(read_times):.2f} ms')
    plt.xlabel('Duration (ms)')
    plt.ylabel('Count')
    plt.title('stream_read() Call Duration Distribution')
    plt.legend()
    plt.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig('scan_streaming_data_timed.png', dpi=150)
    print("\nPlot saved as 'scan_streaming_data_timed.png'")
    plt.show()
    
    # Optional: Print statistics
    print(f"\nDATA STATISTICS:")
    print(f"  Mean: {np.mean(data):.2f}")
    print(f"  Std Dev: {np.std(data):.2f}")
    print(f"  Min: {np.min(data)}")
    print(f"  Max: {np.max(data)}")
else:
    print("\nSkipping plot (no data)")

# Recommendations
print("\n" + "="*70)
print("RECOMMENDATIONS")
print("="*70)
avg_read_time = np.mean(stats.timings['stream_read_call']) if stats.timings['stream_read_call'] else 0
avg_loop_time = total_duration / loop_count if loop_count > 0 else 0
buffer_time = (2**12 / sample_rate)

if overflow_count > 0:
    print("⚠ OVERFLOWS DETECTED!")
    print(f"  - Average loop time: {avg_loop_time*1000:.2f} ms")
    print(f"  - Buffer capacity: {buffer_time*1000:.1f} ms")
    if avg_loop_time > buffer_time / 2:
        print(f"  - ⚠ Loop too slow! Should be < {buffer_time/2*1000:.1f} ms")
        print("  - Consider:")
        print("    1. Reduce sleep time (currently 5 ms)")
        print("    2. Optimize data conversion")
        print("    3. Increase BRAM depth in FPGA")
        print("    4. Read more frequently")
else:
    print("✓ No overflows - system is keeping up!")
    print(f"  - Safety margin: {(buffer_time / avg_loop_time):.1f}x")

if avg_read_time > 0.010:  # > 10 ms
    print(f"\n⚠ stream_read() is slow (avg {avg_read_time*1000:.2f} ms)")
    print("  - This may be due to:")
    print("    1. RPyC network latency")
    print("    2. Multiple register reads in stream_read()")
    print("    3. Data transfer overhead")
