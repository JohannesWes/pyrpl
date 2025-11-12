# Comprehensive Analysis of PyRPL Data Streaming Architecture and Improvement Plan

**Author:** Claude
**Date:** 2025-11-12
**Branch:** scan_module_dev_johannes_filtering_tests

---

## Executive Summary

After thorough analysis of your code, Red Pitaya's streaming solutions, and the communication architecture, I can provide clarity on your streaming bottlenecks and propose concrete improvements. **Your main assumption about connection establishment being the bottleneck is incorrect** - the TCP connection is persistent and reused. The real limitations are:

1. **BRAM buffer size** (4096 samples = 134 ms at 30.5 kHz)
2. **Python polling frequency** requirements
3. **Memory access latency** (not network latency)
4. **Lack of DDR buffering** (which Red Pitaya's streaming solutions use extensively)

---

## Part 1: Clarifying Your Understanding of the Communication Architecture

### Connection Lifecycle - CORRECTING A KEY MISCONCEPTION

Your assumption about "3 generalized steps" is **incorrect** for the current PyRPL architecture:

**What Actually Happens:**

```
Session Start (ONCE):
├─ 1. Python creates TCP socket
├─ 2. Socket connects to monitor_server on Red Pitaya (port 2222)
└─ 3. Connection stays OPEN for entire session

During Streaming (REPEATED):
├─ For each read:
│   ├─ Python sends 8-byte header: ['r', 0x00, length, address]
│   ├─ monitor_server receives header (already connected)
│   ├─ monitor_server calls read_values():
│   │   ├─ open("/dev/mem")
│   │   ├─ mmap() to map FPGA address to virtual memory
│   │   ├─ memcpy from BRAM (via AXI bus)
│   │   ├─ munmap()
│   │   └─ close(fd)
│   ├─ monitor_server sends back: [8-byte header echo + data]
│   └─ Python receives and processes data

Session End (ONCE):
└─ Python sends 'c' command, closes socket
```

**Key Insight:** The TCP connection is **persistent** throughout the session (`redpitaya_client.py:57-81`). Connection establishment happens ONCE, not for every read.

### What IS the Bottleneck?

The expensive operation is **NOT network connection establishment**, but rather:

**The /dev/mem mmap/munmap cycle** (`monitor_server.c:248-268`):

```c
unsigned long* read_values(unsigned long a_addr, ...) {
    int fd = open("/dev/mem", O_RDWR | O_SYNC);  // ~10-20 µs (syscall)
    map_base = mmap(...);                         // ~20-50 µs (page table setup)

    for (i = 0; i < a_len; i++) {
        a_values_buffer[i] = ((unsigned long*) virt_addr)[i];  // ~0.1 µs per word
    }

    munmap(map_base, MAP_SIZE);                   // ~10-20 µs
    close(fd);                                    // ~5-10 µs
    return a_values_buffer;
}
```

**Total overhead per read: 45-100 µs + (length × 0.1 µs) + network latency**

---

## Part 2: Detailed Timing Analysis

### Current Scan Module Streaming Performance

**FPGA Side (`scan_new.v:565-587`):**
- Sample rate: 125 MHz / 4096 = **30,517.58 Hz** (lock-in decimation)
- Valid sample every: **32.768 µs**
- BRAM depth: **4096 samples**
- Buffer fill time: 4096 / 30,517.58 Hz = **134.2 ms**

**Communication Path Timing:**

| Operation | Latency | Notes |
|-----------|---------|-------|
| Python: Create read request | ~1-5 µs | Pure Python overhead |
| Python → RP: TCP send (8 bytes) | ~50-200 µs | Ethernet latency, depends on network |
| RP: recv() header | ~10-20 µs | Kernel buffer, minimal overhead |
| RP: open("/dev/mem") | ~10-20 µs | Syscall overhead |
| RP: mmap() setup | ~20-50 µs | Page table manipulation |
| RP: Read BRAM via AXI | ~0.1 µs/word | Memory bandwidth ~400 MB/s |
| RP: munmap() cleanup | ~10-20 µs | Page table cleanup |
| RP: close(fd) | ~5-10 µs | File descriptor cleanup |
| RP → Python: TCP send (data) | ~50-200 µs + data_size/bandwidth | Gigabit Ethernet = 125 MB/s theoretical |
| Python: recv() and process | ~10-50 µs + data_size/bandwidth | Depends on data size |
| **TOTAL PER READ** | **166-566 µs + data transfer time** | Dominantly memory operations, not network |

**Example:** Reading 100 samples (400 bytes):
- Fixed overhead: ~166-566 µs
- Data transfer: 400 bytes / 125 MB/s ≈ 3.2 µs
- **Total: ~170-570 µs**

### Critical Constraint

With a 134 ms buffer, you must read at least every **~130 ms** (leaving 4 ms margin). This means:

- **Maximum read period: 130 ms**
- **Minimum read frequency: ~7.7 Hz**
- **This is NOT a tight constraint for the communication layer** (which operates in µs)
- **This IS a constraint on Python thread responsiveness**

**Your concern about "other tasks limited in duration" is partially correct:**
- Tasks in the *same Python thread* cannot block for >130 ms
- Solution: Use separate threads or async I/O (not a fundamental architecture problem)

---

## Part 3: How Red Pitaya's Official Streaming Works

Red Pitaya achieves **62.5 MB/s streaming** using a fundamentally different architecture:

### Architecture Comparison

**PyRPL Current (Register-Based):**
```
ADC/Demod → BRAM (4096 samples) → AXI GP0 → /dev/mem → TCP → Python
            ↑                       ↑
            12 BRAM blocks          Slow (single-word access)
```

**Red Pitaya Streaming (DMA-Based):**
```
ADC → AXI HP0/HP1 → DDR3 RAM (up to 412 MB) → ARM CPU → TCP → PC
      ↑             ↑                           ↑
      FIFO buffer   Fast burst transfers       Double-buffering
```

### Key Differences

| Aspect | PyRPL Current | Red Pitaya Streaming |
|--------|---------------|----------------------|
| Buffer Location | FPGA BRAM | DDR3 RAM |
| Buffer Size | 4096 samples (16 KB) | Up to 412 MB |
| Buffer Time @ 30 kHz | 134 ms | Up to 3.7 hours! |
| Data Path | AXI GP0 (slow) | AXI HP0/HP1 (fast) |
| Access Method | mmap per read | DMA + double-buffer |
| Throughput | Limited by polling | 62.5-80 MB/s |
| CPU Involvement | Every read | Only for network TX |

### Red Pitaya's Double-Buffer Strategy

From the streaming documentation:

```
DDR3 RAM Layout:
┌────────────────────┐ ← Start address
│                    │
│   Buffer A (1 MB)  │ ← FPGA writes here
│                    │
├────────────────────┤ ← A done, switch
│                    │
│   Buffer B (1 MB)  │ ← CPU reads from A, FPGA writes to B
│                    │
└────────────────────┘

Ping-Pong Operation:
1. FPGA fills Buffer A while CPU is idle
2. Buffer A full → FPGA signals completion
3. FPGA switches to Buffer B, CPU reads A
4. Buffer B full → FPGA switches to A, CPU reads B
5. If CPU hasn't finished reading → overflow error
```

**This eliminates polling entirely** - the FPGA autonomously writes to DDR via DMA, and the CPU simply reads completed buffers.

---

## Part 4: Existing PyRPL Infrastructure for DDR Access

**Good news:** PyRPL already has the necessary infrastructure! I found:

### AXI Master Modules (`axi_master.v`)

PyRPL has **two AXI master instances** connected to **HP0 and HP1** ports (`red_pitaya_ps.v:143-212`):

```verilog
axi_master #(
  .DW (64),   // 64-bit data width
  .AW (32),   // 32-bit address
  .ID (0)
) axi_master [1:0] (
  // Connected to HP0 and HP1 FPGA→DDR interfaces
  // Currently used by scope module for waveform capture
  ...
);
```

### AXI Write FIFO (`axi_wr_fifo.v`)

This module provides:
- **Small FIFO buffering** (32 entries)
- **Burst write optimization** to DDR
- **Configurable start/stop addresses** (circular buffer support)
- **Overflow detection**
- **Wrap-around** for continuous streaming

**This is exactly what you need!**

### Current Usage

The scope module (`red_pitaya_scope.v:453-573`) already uses these AXI masters for DMA. You can follow the same pattern for the scan module.

---

## Part 5: Comprehensive Streaming Improvement Options

### Option 1: Increase BRAM Size (Quick Fix, Limited Gains)

**Implementation:**
- Increase `MAX_STEPS_BITS` from 12 to 13 or 14
- Doubles/quadruples buffer size

**Pros:**
- Minimal code changes (`scan_new.v:131`)
- No architecture changes

**Cons:**
- Current usage: 12/60 BRAM blocks
- Doubling → 24/60 blocks (40% of FPGA)
- Only buys you 2-4x more time (268-536 ms)
- Doesn't fundamentally solve the problem

**Verdict:** *Not recommended* - Wastes precious FPGA resources for marginal improvement.

---

### Option 2: Optimize monitor_server (Modest Improvement)

**Implementation:**
- Keep `/dev/mem` open and mapped persistently (`monitor_server.c:108-124`)
- Eliminate open/mmap/munmap overhead per read

**Estimated gain:**
- Remove ~45-90 µs per read
- **Not helpful for your use case** - you're constrained by buffer size, not read latency

**Verdict:** *Nice to have*, but doesn't address your core problem.

---

### Option 3: Optimize Python Polling (Marginal Improvement)

**Implementation:**
- Use threading or asyncio for non-blocking reads
- Implement predictive polling (read when ~90% full)
- Batch processing of multiple samples

**Pros:**
- Better Python responsiveness
- Can overlap processing with acquisition

**Cons:**
- Still limited by BRAM size
- Doesn't enable true continuous streaming

**Verdict:** *Complementary* to other solutions, not sufficient alone.

---

### Option 4: Add DDR3 Streaming via AXI HP (RECOMMENDED)

**This is the proper solution.** Implement the same architecture as Red Pitaya's streaming and PyRPL's scope.

#### Architecture

```
Demod Output (30.5 kHz)
    ↓
Small FIFO (32 entries) ← burst accumulation
    ↓
axi_wr_fifo module ← reuse existing
    ↓
AXI Master → HP0/HP1 port
    ↓
DDR3 RAM (configurable region, e.g., 8 MB)
    ↓
Python reads via /dev/mem (at leisure, e.g., every 1 second)
```

#### Implementation Steps

**1. Verilog Changes (`scan_new.v`)**

Add AXI write interface (similar to scope):

```verilog
// Add to module ports (around line 165)
output  wire [32-1:0] axi_waddr_o,      // write address
output  wire [64-1:0] axi_wdata_o,      // write data
output  wire [8-1:0]  axi_wsel_o,       // write byte select
output  wire          axi_wvalid_o,     // write data valid
output  wire [4-1:0]  axi_wlen_o,       // write burst length
output  wire          axi_wfixed_o,     // write burst type
input   wire          axi_werr_i,       // write error
input   wire          axi_wrdy_i,       // write ready

// Instantiate axi_wr_fifo (around line 650)
axi_wr_fifo #(
    .DW(64),  // 64-bit for efficient DDR access
    .AW(32),
    .FW(5)    // 32-entry FIFO
) stream_ddr_writer (
    .axi_clk_i(clk),
    .axi_rstn_i(rstn),
    // Connect to AXI master
    .axi_waddr_o(axi_waddr_o),
    .axi_wdata_o(axi_wdata_o),
    ... // other AXI signals
    // Data input from streaming engine
    .wr_data_i({32'b0, demod_input_i}),  // pad to 64-bit
    .wr_val_i(reg_stream_enable && demod_input_valid_i),
    // Configuration (new registers)
    .ctrl_start_addr_i(reg_ddr_start_addr),  // NEW: DDR start address
    .ctrl_stop_addr_i(reg_ddr_stop_addr),    // NEW: DDR stop address
    .ctrl_trig_size_i(4'h8),                 // burst when 8 samples ready
    .ctrl_wrap_i(1'b1),                      // wrap around (circular buffer)
    .ctrl_clr_i(reg_stream_reset_cmd),
    .stat_overflow_o(reg_ddr_overflow),      // NEW: overflow flag
    .stat_cur_addr_o(reg_ddr_cur_addr)       // NEW: current write position
);
```

**2. Add Configuration Registers**

```verilog
// New register addresses (around line 186)
localparam ADDR_DDR_START_ADDR  = 20'h00030;  // DDR buffer start
localparam ADDR_DDR_STOP_ADDR   = 20'h00034;  // DDR buffer stop
localparam ADDR_DDR_CUR_ADDR    = 20'h00038;  // Current write position (RO)
localparam ADDR_DDR_OVERFLOW    = 20'h0003C;  // Overflow flag (RO)

// Registers (around line 230)
reg [32-1:0] reg_ddr_start_addr;  // e.g., 0x10000000
reg [32-1:0] reg_ddr_stop_addr;   // e.g., 0x10800000 (8 MB buffer)
wire [32-1:0] reg_ddr_cur_addr;   // from axi_wr_fifo
wire reg_ddr_overflow;            // from axi_wr_fifo
```

**3. Connect to AXI Master (`red_pitaya_ps.v`)**

The scan module needs to be connected to one of the existing AXI masters (HP0 or HP1). This requires arbitration if sharing with scope, or using the dedicated master.

**4. Python Interface (`pyrpl/hardware_modules/scan.py`)**

```python
class Scan(HardwareModule):
    # Existing attributes...

    # New DDR streaming attributes
    ddr_start_addr = IntRegister(0x30, doc="DDR buffer start address")
    ddr_stop_addr = IntRegister(0x34, doc="DDR buffer stop address")
    ddr_cur_addr = IntRegister(0x38, doc="Current DDR write address", read_only=True)
    ddr_overflow = BoolRegister(0x3C, doc="DDR buffer overflow", read_only=True)

    def stream_ddr_setup(self, buffer_size_mb=8):
        """Configure DDR streaming with specified buffer size."""
        # Allocate DDR region (coordinate with Linux to reserve memory)
        # For simplicity, use fixed region: 0x10000000 - 0x18000000 (128 MB available)
        start_addr = 0x10000000
        stop_addr = start_addr + (buffer_size_mb * 1024 * 1024) - 1

        self.ddr_start_addr = start_addr
        self.ddr_stop_addr = stop_addr
        return start_addr, stop_addr

    def stream_ddr_read_chunk(self, num_samples):
        """Read samples from DDR buffer via monitor_server."""
        # Calculate read address based on ddr_cur_addr
        # Read via client.reads() at DDR physical address
        # This bypasses BRAM entirely!
        cur_addr = self.ddr_cur_addr

        # Read backwards from current position to avoid wrap issues
        read_addr = cur_addr - (num_samples * 4)  # 4 bytes per sample
        if read_addr < self.ddr_start_addr:
            read_addr += (self.ddr_stop_addr - self.ddr_start_addr)

        # Direct DDR read
        data = self._client.reads(read_addr, num_samples)
        return data
```

#### Performance Gains

**With 8 MB DDR buffer:**
- Buffer capacity: 8 MB / 4 bytes = **2,097,152 samples**
- Buffer time @ 30 kHz: 2,097,152 / 30,517 Hz = **68.7 seconds**

**Comparison:**

| Metric | Current (BRAM) | With DDR Streaming |
|--------|----------------|-------------------|
| Buffer size | 16 KB | 8 MB (500x larger) |
| Buffer time | 134 ms | 68.7 seconds (512x longer) |
| Read frequency required | 7.7 Hz (every 130 ms) | 0.015 Hz (every 66 seconds) |
| Python thread constraint | Must respond in 130 ms | Must respond in 66 seconds |
| FPGA resources | 12 BRAM blocks | 12 BRAM + AXI master |
| Max throughput | ~122 KB/s (4096 samples/134ms) | ~2 MB/s (burst reads) |

**You can now safely run minute-long experiments without worrying about buffer overflow!**

---

### Option 5: Hybrid Approach (BEST for Flexibility)

Combine Options 3 and 4:

1. **Keep BRAM streaming** for short, interactive acquisitions
2. **Add DDR streaming mode** for long, continuous acquisitions
3. **Python selects mode** based on acquisition duration

**Implementation:**
```python
# In scan.py
def stream_start(self, duration_ms, mode='auto'):
    if mode == 'auto':
        mode = 'ddr' if duration_ms > 1000 else 'bram'

    if mode == 'bram':
        # Use existing BRAM streaming (scan_new.v:565-587)
        self.stream_control = 0x3  # enable + reset
    elif mode == 'ddr':
        # Use new DDR streaming
        self.stream_ddr_setup(buffer_size_mb=min(duration_ms * 0.12 / 1000, 64))
        self.stream_control = 0x3
```

---

## Part 6: Additional Optimizations

### A. Optimize monitor_server for Burst Reads

**Current:** Every read does open/mmap/munmap

**Better:** Keep persistent mapping for frequently-accessed regions

```c
// In monitor_server.c
static void* persistent_map_base = NULL;
static int persistent_fd = -1;

void initialize_persistent_map() {
    if ((persistent_fd = open("/dev/mem", O_RDWR | O_SYNC)) == -1) FATAL;
    persistent_map_base = mmap(0, 0x2000000, PROT_READ | PROT_WRITE,
                               MAP_SHARED, persistent_fd, 0x10000000);
    // Maps entire DDR streaming region (32 MB)
}

unsigned long* read_values_fast(unsigned long a_addr, ...) {
    if (a_addr >= 0x10000000 && a_addr < 0x12000000) {
        // Use persistent mapping (no syscall overhead!)
        void* virt_addr = persistent_map_base + (a_addr - 0x10000000);
        memcpy(a_values_buffer, virt_addr, a_len * sizeof(unsigned long));
        return a_values_buffer;
    } else {
        // Fall back to old method for other addresses
        return read_values(a_addr, a_values_buffer, a_len);
    }
}
```

**Benefit:** Reduces read latency from 166-566 µs to **~10-30 µs** for DDR reads.

### B. Use UDP Instead of TCP (Optional)

For truly high-throughput streaming (>62.5 MB/s):

- TCP has ack overhead (~40% of bandwidth in high-speed scenarios)
- UDP eliminates this (packet loss acceptable for some applications)
- Red Pitaya streaming uses TCP, so 62.5 MB/s is proven achievable

### C. Implement Read-Ahead Buffering in Python

```python
class StreamBuffer:
    def __init__(self, scan, buffer_chunks=4):
        self.scan = scan
        self.buffer = deque(maxlen=buffer_chunks)
        self.thread = threading.Thread(target=self._read_loop, daemon=True)

    def _read_loop(self):
        while self.running:
            chunk = self.scan.stream_ddr_read_chunk(10000)
            self.buffer.append(chunk)
            time.sleep(0.1)  # Read every 100 ms

    def get_data(self):
        return self.buffer.popleft()  # Non-blocking
```

---

## Part 7: Recommended Implementation Path

### Phase 1: Quick Win (1-2 days)
1. Optimize monitor_server for persistent mapping (**Option 2**)
2. Implement Python async polling (**Option 3**)
3. **Expected gain:** 2-3x better responsiveness, no architecture change

### Phase 2: DDR Streaming (1-2 weeks)
1. Add `axi_wr_fifo` instance to scan_new.v (**Option 4**)
2. Add DDR configuration registers
3. Connect to AXI master (HP0 or HP1)
4. Implement Python DDR read methods
5. **Expected gain:** 500x longer buffer (134 ms → 68 seconds)

### Phase 3: Production Hardening (1 week)
1. Add double-buffering logic for continuous streaming
2. Implement overflow recovery
3. Add bandwidth monitoring
4. Create hybrid mode selector (**Option 5**)

---

## Part 8: Answers to Your Specific Questions

### Q: "I assume establishing connection is most time-expensive"
**A:** ❌ **NO.** The TCP connection is persistent (stays open for entire session). The expensive part is **mmap/munmap syscalls** (~45-90 µs) and **BRAM size limitation**.

### Q: "Duration of step 2 depends on data length, steps 1 and 3 are fixed"
**A:** ✅ **Partially correct**, but you have the magnitudes wrong:
- "Connection setup" (happens once): ~50-200 µs
- Per-read overhead: ~166-566 µs (dominated by mmap, NOT network)
- Data transfer: ~3 µs per 1000 bytes over gigabit Ethernet
- **The bottleneck is NOT data transfer, it's memory syscalls**

### Q: "Would be favorable to stream large chunks"
**A:** ✅ **Absolutely correct!** This is why DDR buffering is the solution:
- Current: Read 4096 samples every 134 ms = 30.5 KB reads
- With DDR: Read 1,000,000 samples every 30 seconds = 3.8 MB reads
- **Fewer reads = less overhead = higher efficiency**

### Q: "Store data in DDR memory before streaming"
**A:** ✅ **Exactly right!** This is what Red Pitaya streaming does, and what I recommend in Option 4.

### Q: "Red Pitaya streaming_manager - how do they stream so fast?"
**A:** They use:
1. **AXI HP ports** (not GP0 like monitor_server)
2. **DDR3 RAM** (not BRAM)
3. **Double-buffering** (FPGA fills one while CPU reads other)
4. **DMA** (no per-sample CPU involvement)
5. **Optimized ARM CPU code** (dedicated to network TX)
6. **Result:** 62.5-80 MB/s sustained

### Q: "Deep Memory Mode - how does it work?"
**A:** It's Red Pitaya's API for:
- Allocating DDR regions (up to 412 MB)
- Streaming ADC directly to DDR via DMA
- Reading DDR via `/dev/mem` (like PyRPL, but from DDR not BRAM)
- **You can use the same principle** without their specific API

---

## Part 9: Code You Can Use Right Now

Here's a proof-of-concept for reading from DDR (if you manually configure an `axi_wr_fifo` instance):

```python
# In your Python code
def test_ddr_streaming():
    # Assuming you've set up DDR streaming to 0x10000000 - 0x10800000
    DDR_BASE = 0x10000000
    SAMPLES = 100000

    # Direct DDR read (bypasses BRAM entirely!)
    data = pyrpl.rp.client.reads(DDR_BASE, SAMPLES)

    # Process at your leisure - no rush!
    # The FPGA is filling DDR independently in the background
    analyze_data(data)

    time.sleep(10)  # Take your time...

    # Read next chunk
    data2 = pyrpl.rp.client.reads(DDR_BASE + SAMPLES*4, SAMPLES)
```

---

## Conclusion

**Your intuition about needing larger buffers and DDR storage is absolutely correct.** The current BRAM-based streaming is a reasonable design for short acquisitions, but fundamentally cannot scale to long continuous streaming due to FPGA resource constraints.

**The solution is already partially implemented in PyRPL** - the scope module uses `axi_wr_fifo` and AXI masters for DMA to DDR. You should:

1. **Short term:** Optimize monitor_server and Python polling (modest gain)
2. **Medium term:** Implement DDR streaming using existing infrastructure (500x improvement)
3. **Long term:** Add double-buffering and continuous streaming support (unlimited duration)

The good news: **You have all the building blocks already.** PyRPL has AXI masters, write FIFOs, and the communication infrastructure. You just need to connect them to the scan module and add the Python interface.

---

## References

**Code Files Analyzed:**
- `pyrpl/fpga/rtl/scan_new.v` - Scan module FPGA implementation
- `pyrpl/monitor_server/monitor_server.c` - C server for memory access
- `pyrpl/redpitaya_client.py` - Python TCP client
- `pyrpl/docs/developer_guide/communication_architecture.md` - Architecture docs
- `pyrpl/fpga/rtl/axi_master.v` - AXI master for DDR access
- `pyrpl/fpga/rtl/axi_wr_fifo.v` - Write FIFO for burst transfers
- `pyrpl/fpga/rtl/red_pitaya_scope.v` - Example of DDR streaming usage
- `pyrpl/fpga/rtl/red_pitaya_ps.v` - PS wrapper with AXI connections

**External Resources:**
- Red Pitaya Data Stream Control: https://redpitaya.readthedocs.io/en/latest/appsFeatures/applications/streaming/appStreaming.html
- Red Pitaya Deep Memory Acquisition: https://redpitaya.readthedocs.io/en/latest/appsFeatures/remoteControl/deepMemoryMode.html
- Red Pitaya Forum discussions on DMA and streaming
- Zynq-7000 Technical Reference Manual (UG585)

---

## Next Steps

If you'd like to proceed with implementation, I can help with:

1. **Full Verilog code** for DDR streaming integration into scan module
2. **Python API design** for the new streaming modes
3. **FPGA connection details** (wiring scan module to AXI master)
4. **Memory reservation** in Linux for dedicated DDR regions
5. **Testing procedures** and validation strategies

Please let me know which aspect you'd like to tackle first!
