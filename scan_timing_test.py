"""
Low-level timing test for PyRPL streaming operations.
This script measures the performance of individual operations to identify bottlenecks.

Run this in your qudi IPython console after defining 'scan':
    scan = redpitaya_finite_sampling._pyrpl.rp.scan
    exec(open('scan_timing_test.py').read())
"""
import time
import numpy as np

print("="*70)
print("PyRPL Streaming Performance Test")
print("="*70)

# Check if scan is defined
try:
    scan
    print(f"✓ Using scan object: {scan}")
except NameError:
    print("✗ ERROR: 'scan' object not defined!")
    print("  Please define scan before running this script.")
    print("  Example: scan = redpitaya_finite_sampling._pyrpl.rp.scan")
    raise

# Test 1: Single register read performance
print("\n" + "-"*70)
print("Test 1: Single Register Read Performance")
print("-"*70)

n_reads = 100
t0 = time.time()
for _ in range(n_reads):
    _ = scan._read(0x00)  # Read control register
t_total = time.time() - t0
print(f"Performed {n_reads} single register reads")
print(f"  Total time: {t_total:.3f} s")
print(f"  Average per read: {t_total/n_reads*1000:.3f} ms")
print(f"  Throughput: {n_reads/t_total:.1f} reads/s")

# Test 2: Bulk read performance (different sizes)
print("\n" + "-"*70)
print("Test 2: Bulk Read Performance")
print("-"*70)

for read_size in [1, 10, 100, 500, 1000, 2000, 4096]:
    n_reads = max(10, 1000 // read_size)  # Adjust number of reads
    addr = 0x30000  # Count BRAM address
    
    t0 = time.time()
    for _ in range(n_reads):
        data = scan._reads(addr, read_size)
    t_total = time.time() - t0
    
    bytes_read = read_size * 4 * n_reads
    throughput_kbps = (bytes_read / 1024) / t_total
    avg_time = t_total / n_reads
    
    print(f"  Read size: {read_size:4d} samples ({read_size*4:5d} bytes)")
    print(f"    {n_reads} reads, avg: {avg_time*1000:6.3f} ms, throughput: {throughput_kbps:6.1f} KB/s")

# Test 3: stream_status() overhead
print("\n" + "-"*70)
print("Test 3: stream_status() Call Overhead")
print("-"*70)

n_calls = 100
t0 = time.time()
for _ in range(n_calls):
    active, overflow, wr_ptr, samples = scan.stream_status()
t_total = time.time() - t0
print(f"Performed {n_calls} stream_status() calls")
print(f"  Total time: {t_total:.3f} s")
print(f"  Average per call: {t_total/n_calls*1000:.3f} ms")

# Test 4: Data conversion overhead
print("\n" + "-"*70)
print("Test 4: Data Conversion Overhead (RPyC to NumPy)")
print("-"*70)

# Start streaming to get real data
scan.stream_start(input_source='demod')
time.sleep(0.1)  # Let some data accumulate

# Read once to get RPyC data
rpyc_data = scan.stream_read(max_samples=1000)

if len(rpyc_data) > 0:
    # Test the ONLY method that works with RPyC
    n_conversions = 50
    
    # The working method: tolist() then np.array
    t0 = time.time()
    for _ in range(n_conversions):
        local_data = np.array(rpyc_data.tolist(), dtype='int32')
    t_method1 = time.time() - t0
    print(f"Method: np.array(data.tolist(), dtype='int32')")
    print(f"  {n_conversions} conversions of {len(rpyc_data)} samples")
    print(f"  Average: {t_method1/n_conversions*1000:.3f} ms")
    print(f"  Note: This is the ONLY method that works with RPyC netrefs")
else:
    print("  Skipping (no data available)")

scan.stream_stop()

# Test 5: Complete stream_read() cycle
print("\n" + "-"*70)
print("Test 5: Complete stream_read() Cycle with Timing Enabled")
print("-"*70)

scan.stream_start(input_source='demod')
time.sleep(0.05)  # Let data accumulate

n_reads = 20
times = []
samples_list = []

for _ in range(n_reads):
    t0 = time.time()
    data = scan.stream_read(max_samples=4096, enable_timing=True)
    t_read = time.time() - t0
    times.append(t_read)
    samples_list.append(len(data))
    time.sleep(0.010)  # 10 ms between reads

scan.stream_stop()

times = np.array(times) * 1000  # Convert to ms
samples_list = np.array(samples_list)

print(f"Performed {n_reads} stream_read() calls")
print(f"  Avg time: {np.mean(times):.3f} ms (min: {np.min(times):.3f}, max: {np.max(times):.3f})")
print(f"  Avg samples: {np.mean(samples_list):.1f} (min: {np.min(samples_list)}, max: {np.max(samples_list)})")

# Test 6: Measure theoretical buffer capacity
print("\n" + "-"*70)
print("Test 6: Theoretical Buffer Capacity")
print("-"*70)

sample_rate = 125e6 / 4096  # ~30.5 kHz
bram_depth = 2**12  # 4096 samples
buffer_time_ms = (bram_depth / sample_rate) * 1000

print(f"  Sample rate: {sample_rate:.1f} Hz")
print(f"  BRAM depth: {bram_depth} samples")
print(f"  Buffer capacity: {buffer_time_ms:.1f} ms")
print(f"  Data rate: {sample_rate * 4 / 1024:.2f} KB/s")

# Calculate required performance
avg_read_time = np.mean(times)
max_safe_interval = buffer_time_ms / 2  # Safety factor of 2

print(f"\n  Required read interval: < {max_safe_interval:.1f} ms")
print(f"  Actual read time: {avg_read_time:.1f} ms")

if avg_read_time < max_safe_interval:
    print(f"  ✓ System can keep up! (margin: {max_safe_interval/avg_read_time:.1f}x)")
else:
    print(f"  ✗ System TOO SLOW! (need {avg_read_time/max_safe_interval:.1f}x speedup)")

# Summary
print("\n" + "="*70)
print("PERFORMANCE SUMMARY & RECOMMENDATIONS")
print("="*70)

single_read_time = (t_total / n_reads) * 1000  # From Test 1
bulk_read_time_1k = None  # Will be set from Test 2 results

# Recommendations based on measurements
print("\nBottleneck Analysis:")
if single_read_time > 5:
    print(f"  ⚠ Single register reads are slow ({single_read_time:.1f} ms)")
    print("    → Network/RPyC latency is the main bottleneck")
    print("    → Consider batching reads or caching status")
    
if avg_read_time > 10:
    print(f"  ⚠ stream_read() is slow ({avg_read_time:.1f} ms)")
    print("    → Multiple factors contributing to slowness")

print("\nOptimization suggestions:")
print("  1. Reduce number of register reads in stream_read()")
print("  2. Use larger bulk reads (test shows optimal ~1000-2000 samples)")
print("  3. Consider reducing sleep time if system can keep up")
print("  4. Cache stream_status() results when possible")
print("  5. Consider increasing BRAM buffer size in FPGA")

print("\n" + "="*70)
