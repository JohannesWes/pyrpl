# PyRPL Communication Architecture Analysis

## Overview: Three-Layer Communication Stack

PyRPL's communication with the Red Pitaya FPGA involves **three distinct layers**:

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: Python ←→ Red Pitaya Linux (ARM CPU)              │
│  Protocol: Custom binary protocol over TCP/IP               │
│  Transport: Ethernet (typically) or WiFi                    │
└─────────────────────────────────────────────────────────────┘
                            ↓↑
┌─────────────────────────────────────────────────────────────┐
│  Layer 2: Linux (ARM) ←→ FPGA Registers                     │
│  Protocol: Memory-mapped I/O (MMIO) via /dev/mem            │
│  Transport: AXI4-Lite bus (SoC internal)                    │
└─────────────────────────────────────────────────────────────┘
                            ↓↑
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: FPGA Register Map ←→ Verilog Modules              │
│  Protocol: Address decoding in red_pitaya_dsp.v             │
│  Address Space: 0x40300000 + module_num * 0x10000           │
└─────────────────────────────────────────────────────────────┘
```

---

## Layer 1: Python ↔ Red Pitaya Linux (Custom TCP Binary Protocol)

### Protocol Specification

The protocol is defined in `monitor_server.c` (lines 37-56) and implemented in `redpitaya_client.py`.

**8-Byte Header Format:**

```
┌──────┬──────┬─────────┬─────────────────────────┐
│ Byte │ Byte │ Bytes   │ Bytes 5-8               │
│  1   │  2   │  3-4    │                         │
├──────┼──────┼─────────┼─────────────────────────┤
│ Cmd  │ Rsvd │ Length  │ Address                 │
│ char │ 0x00 │ uint16  │ uint32                  │
└──────┴──────┴─────────┴─────────────────────────┘

Cmd: 'r' = read, 'w' = write, 'c' = close
Length: Number of 4-byte units (max 65535)
Address: FPGA register address (e.g., 0x40300004)
```

### Python Client Implementation

**File:** `redpitaya_client.py:107-123`

```python
def _reads(self, addr, length):
    # Construct 8-byte header
    header = b'r' + bytes(bytearray([
        0,                              # Reserved byte
        length & 0xFF,                  # Length low byte
        (length >> 8) & 0xFF,           # Length high byte
        addr & 0xFF,                    # Address bytes (little-endian)
        (addr >> 8) & 0xFF,
        (addr >> 16) & 0xFF,
        (addr >> 24) & 0xFF
    ]))

    # Send header
    self.socket.send(header)

    # Receive header echo + data
    data = self.socket.recv(length * 4 + 8)

    # Verify echo and return data as numpy array
    return np.frombuffer(data[8:], dtype=np.uint32)
```

### C Server Implementation

**File:** `monitor_server.c:218-224`

```c
if (buffer[0] == 'r') {  // Read command
    // Read from FPGA memory
    read_values(address, rw_buffer, data_length);

    // Send header + data back to client
    n = send(newsockfd, (void*)data_buffer,
             data_length * sizeof(unsigned long) + 8, 0);
}
```

---

## Layer 2: Linux (ARM) ↔ FPGA (Memory-Mapped I/O via AXI Bus)

This is where the "magic" happens - **how can a userspace program access FPGA registers?**

### The `/dev/mem` Device

**File:** `monitor_server.c:248-268`

```c
unsigned long* read_values(unsigned long a_addr,
                          unsigned long* a_values_buffer,
                          unsigned long a_len) {
    int fd = -1;

    // 1. Open /dev/mem - gives access to physical memory
    if((fd = open("/dev/mem", O_RDWR | O_SYNC)) == -1) FATAL;

    // 2. Map FPGA address space into process virtual memory
    //    MAP_SIZE = 131072 (128KB window)
    //    a_addr & ~MAP_MASK aligns to 128KB boundary
    map_base = mmap(0, MAP_SIZE, PROT_READ | PROT_WRITE,
                    MAP_SHARED, fd, a_addr & ~MAP_MASK);

    // 3. Calculate virtual address offset
    void* virt_addr = map_base + (a_addr & MAP_MASK);

    // 4. Read from mapped memory (becomes AXI bus transaction)
    for (i = 0; i < a_len; i++) {
        a_values_buffer[i] = ((unsigned long*) virt_addr)[i];
    }

    // 5. Cleanup
    munmap(map_base, MAP_SIZE);
    close(fd);
}
```

### What is `/dev/mem`?

`/dev/mem` is a **character device in Linux** that provides access to the system's physical memory. On the Zynq SoC:

- **Physical Address 0x40000000 - 0x7FFFFFFF** is mapped to **AXI GP0 (General Purpose) port**
- This address range connects the ARM CPU to the FPGA fabric
- Memory reads/writes automatically translate to **AXI4-Lite bus transactions**

### Address Space Mapping

**From `monitor_server.c:85-88` and Zynq documentation:**

```
Physical Memory Map (Red Pitaya / Zynq-7010/7020):
┌──────────────────┬──────────────────────────────┐
│ 0x00000000       │ DDR RAM (ARM CPU memory)     │
│ to 0x3FFFFFFF    │                              │
├──────────────────┼──────────────────────────────┤
│ 0x40000000       │ AXI GP0 Port → FPGA          │
│ to 0x7FFFFFFF    │ (PyRPL uses 0x40000000-      │
│                  │  0x41000000 = 16MB)          │
├──────────────────┼──────────────────────────────┤
│ 0x80000000+      │ Other peripherals            │
└──────────────────┴──────────────────────────────┘
```

**PyRPL Module Address Allocation** (`red_pitaya_dsp.v:45-46`):

```
Module i occupies: 0x40300000 + i * 0x10000 to 0x4030FFFF (64KB each)

Examples:
- PID0 (i=0):  0x40300000 - 0x4030FFFF
- PID1 (i=1):  0x40310000 - 0x4031FFFF
- IIR  (i=4):  0x40340000 - 0x4034FFFF
- IQ0  (i=5):  0x40350000 - 0x4035FFFF
- Scope:       0x40100000 - 0x4010FFFF
- ASG:         0x40200000 - 0x4020FFFF
```

---

## Layer 3: FPGA Internal Architecture (AXI Bus → Verilog Modules)

### AXI4-Lite Bus Protocol

The Zynq PS (ARM) communicates with PL (FPGA) via the **AXI4-Lite protocol** - ARM's standard on-chip interconnect.

**Key Components:**

1. **PS Side:** Zynq Processing System generates AXI transactions
2. **AXI Slave:** `axi_slave.v` converts AXI protocol to simplified Red Pitaya bus
3. **Address Decoder:** `red_pitaya_dsp.v` routes transactions to correct module
4. **Module Registers:** Verilog modules respond to read/write commands

### Signal Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│ Zynq PS (ARM Cortex-A9 running Linux)                       │
│ • Running monitor_server                                    │
│ • Issues AXI read/write via /dev/mem mmap                   │
└────────────────────────┬────────────────────────────────────┘
                         │ M_AXI_GP0 (Master AXI GP0 port)
                         ↓
┌─────────────────────────────────────────────────────────────┐
│ red_pitaya_ps.v (PS Wrapper + AXI Slave)                    │
│ • axi_slave module (lines 273-331)                          │
│ • Converts AXI → sys_addr, sys_wdata, sys_wen, sys_rdata    │
└────────────────────────┬────────────────────────────────────┘
                         │ Simplified "RP Bus"
                         │ sys_addr[31:0], sys_wdata[31:0]
                         ↓
┌─────────────────────────────────────────────────────────────┐
│ red_pitaya_dsp.v (Address Decoder & DSP Modules)            │
│ • Decodes sys_addr to select module                         │
│ • Routes data to/from individual module registers           │
└────────────┬────────────┬────────────┬──────────────────────┘
             │            │            │
        ┌────▼───┐   ┌────▼───┐   ┌────▼───┐
        │ PID0   │   │ IIR    │   │ Scope  │  ... etc
        │ Module │   │ Module │   │ Module │
        └────────┘   └────────┘   └────────┘
        0x40300000   0x40340000   0x40100000
```

### AXI Slave Implementation

**File:** `axi_slave.v:17-47` (see diagram in comments)

The AXI slave bridges the complex AXI4-Lite protocol (5 channels: write address, write data, write response, read address, read data) to a simpler Red Pitaya bus with just:
- `sys_addr_o` - Address
- `sys_wdata_o` - Write data
- `sys_wen_o` - Write enable
- `sys_ren_o` - Read enable
- `sys_rdata_i` - Read data
- `sys_ack_i` - Acknowledge

### Address Decoding in DSP Module

**File:** `red_pitaya_dsp.v`

The DSP module examines `sys_addr[19:16]` to determine which module (0-15) the transaction targets, then forwards the transaction to that module's register interface.

---

## Complete Example: Writing to a PID Register

Let's trace a complete write operation from Python to FPGA:

```python
# Python code
pyrpl.pid0.setpoint = 1000  # Set PID setpoint register
```

### Step-by-Step Flow:

**1. Python Descriptor** (`attributes.py`, FloatRegister)
```python
# Converts 1000 → fixed-point, determines address
addr = 0x40300000 + 0x08  # PID0 base + setpoint offset
value_uint32 = int(1000 * 2**14)  # Convert to Q14 fixed-point
```

**2. Python Client** (`redpitaya_client.py:125-143`)
```python
def _writes(self, addr, values):
    header = b'w' + bytes(bytearray([
        0, 1, 0,  # Length = 1 word
        0x08, 0x00, 0x30, 0x40  # Address 0x40300008
    ]))
    self.socket.send(header + np.array([value_uint32]).tobytes())
```

**3. TCP/IP Stack**
- Python socket → OS network stack → Ethernet PHY
- Packet transmitted to Red Pitaya IP address
- Red Pitaya receives, routes to monitor_server on port 2222

**4. C Server** (`monitor_server.c:225-234`)
```c
// Receive header
recv(newsockfd, buffer, 8, MSG_WAITALL);
address = 0x40300008;
data_length = 1;

// Receive data
recv(newsockfd, rw_buffer, 4, MSG_WAITALL);

// Write to FPGA
write_values(address, rw_buffer, data_length);
```

**5. mmap & AXI Transaction** (`monitor_server.c:270-289`)
```c
// Map physical address to virtual
fd = open("/dev/mem", O_RDWR | O_SYNC);
map_base = mmap(0, 131072, PROT_READ | PROT_WRITE,
                MAP_SHARED, fd, 0x40300000);

// Write triggers AXI bus transaction
virt_addr = map_base + 0x08;
*((unsigned long*)virt_addr) = value_uint32;
```

**6. Zynq PS → PL** (Hardware)
- ARM CPU write instruction → L1 cache → AXI interconnect
- AXI GP0 master port generates:
  - Write address phase: AWADDR = 0x40300008
  - Write data phase: WDATA = value_uint32
  - Write response: BRESP received

**7. FPGA AXI Slave** (`red_pitaya_ps.v:273-331`)
- Receives AXI transaction on M_AXI_GP0 port
- axi_slave module converts to:
  - `sys_addr = 0x40300008`
  - `sys_wdata = value_uint32`
  - `sys_wen = 1` (write enable high)

**8. DSP Address Decoder** (`red_pitaya_dsp.v`)
```verilog
// Decode address bits [19:16] = 0 → PID0 module
module_select = sys_addr[19:16];  // = 0
// Forward to PID0 with offset 0x08
```

**9. PID Module Register** (Verilog)
```verilog
// In red_pitaya_pid_block.v
always @(posedge clk) begin
    if (sys_wen && sys_addr[15:0] == 16'h0008) begin
        setpoint_reg <= sys_wdata[13:0];  // Store in register
    end
end
```

**10. PID Algorithm** (Real-time processing)
```verilog
// Every clock cycle (125 MHz = 8ns)
error = setpoint_reg - input_signal;
output = Kp * error + Ki * integral + Kd * derivative;
```

---

## Key Architectural Insights

### 1. **Zero-Copy Data Path**
From `mmap()` to FPGA register is **direct memory access** - no data copying occurs. The CPU write instruction becomes an AXI bus transaction automatically.

### 2. **Synchronization**
- **O_SYNC flag** in `open("/dev/mem", O_RDWR | O_SYNC)` ensures writes complete before returning (no CPU caching)
- AXI protocol includes handshaking (VALID/READY signals) for transaction completion

### 3. **Performance Characteristics**
- **Latency:** ~10-100 µs per register access (network + syscall + AXI)
- **Throughput:** Limited by TCP/IP, not AXI (AXI can do ~1 GB/s)
- **Real-time:** FPGA runs at 125 MHz independent of Python - no jitter

### 4. **Address Space Isolation**
- Each module gets 64KB (0x10000 bytes)
- Addresses `0x403z00zz` are reserved for routing registers (not forwarded to modules)
- Prevents module address conflicts

---

## Additional Communication Channels

### DMA via AXI HP Ports

For high-throughput data transfers (e.g., scope data acquisition), PyRPL uses **AXI HP (High Performance)** ports:

- **File:** `red_pitaya_ps.v:143-212` defines `axi_master` modules
- **Purpose:** Direct Memory Access from FPGA to ARM DDR RAM
- **Bandwidth:** Up to 1200 MB/s per port (much faster than GP0)
- **Use case:** Streaming scope data, large buffer transfers

The Scan module can write directly to DDR memory via these ports for high-speed data acquisition.

---

## Why This Architecture?

**Advantages:**
1. **Simple protocol** - Easy to implement in any language
2. **Network transparent** - Works over Ethernet/WiFi/USB
3. **Standard Linux** - Uses `/dev/mem`, no custom kernel drivers
4. **Real-time FPGA** - 125 MHz DSP independent of network latency
5. **Flexible** - Can add modules by changing FPGA bitfile
6. **Low resource** - Minimal CPU/memory overhead

**Disadvantages:**
1. **Latency** - Network + syscalls add ~50 µs overhead per register access
2. **Security** - Requires root and `/dev/mem` access
3. **Single client** - Only one TCP connection at a time (though multiple clients can be spawned)
4. **No DMA for registers** - Each register access is individual (but bulk transfers use HP ports)

---

## References in Codebase

**Python Side:**
- `pyrpl/redpitaya_client.py:37-178` - MonitorClient implementation
- `pyrpl/redpitaya.py:465-473` - Client initialization
- `pyrpl/attributes.py` - Register descriptors that generate addresses

**C Server:**
- `pyrpl/monitor_server/monitor_server.c` - Complete server implementation

**FPGA Side:**
- `pyrpl/fpga/rtl/axi_slave.v` - AXI to RP bus converter
- `pyrpl/fpga/rtl/red_pitaya_ps.v` - PS wrapper with AXI slave
- `pyrpl/fpga/rtl/red_pitaya_top.v` - Top level connections
- `pyrpl/fpga/rtl/red_pitaya_dsp.v` - Module address decoding and signal routing

**Hardware:**
- Zynq-7010/7020 Technical Reference Manual (Xilinx UG585)
- AXI4-Lite specification (ARM IHI0022E)
- Red Pitaya schematics and documentation

---

## Debugging Tips

### Python Side
```python
import logging
logging.getLogger('pyrpl.redpitaya_client').setLevel(logging.DEBUG)
# Shows all register reads/writes with addresses and values
```

### C Server Side
```c
// In monitor_server.c, set DEBUG_MONITOR to 1 (line 92)
#define DEBUG_MONITOR 1
// Recompile and upload to Red Pitaya
// Logs will appear on SSH console
```

### FPGA Side
- Use Vivado ILA (Integrated Logic Analyzer) to capture AXI transactions
- Monitor `sys_addr`, `sys_wdata`, `sys_wen`, `sys_ack` signals
- Check timing reports for address decoding logic

---

## Conclusion

This architecture is an elegant example of **layered abstraction** - each layer handles one concern (network transport, memory mapping, address decoding) to enable high-level Python code to control low-level FPGA hardware with minimal latency. The key insight is leveraging **memory-mapped I/O** via `/dev/mem` to make FPGA registers appear as normal memory to the Linux system, accessible via standard `mmap()` calls.
