import os
import glob
import subprocess
import shutil
import time
import sys
import tempfile
import stat

def force_delete(action, name, exc):
    """Callback for shutil.rmtree to force delete read-only files."""
    try:
        os.chmod(name, stat.S_IWRITE)
        os.remove(name)
    except Exception:
        pass

def process_single_ip(ip_name, source_file, tcl_script, is_xci, script_dir, max_retries=2):
    """
    Generates a single IP using the Build-in-Temp strategy.
    
    Args:
        ip_name (str): Name of the IP.
        source_file (str): Path to the source file (.coe or .xci).
        tcl_script (str): Path to the TCL script to run.
        is_xci (bool): True if source is XCI, False if COE.
        script_dir (str): Base directory of the script.
        max_retries (int): Number of retries.
    
    Returns:
        bool: True if successful, False otherwise.
    """
    final_ip_dir = os.path.join(script_dir, ip_name)
    final_dcp_file = os.path.join(final_ip_dir, f"{ip_name}.dcp")
    
    print(f"\nProcessing IP: {ip_name}")
    
    for attempt in range(1, max_retries + 1):
        # Create a unique temporary directory for THIS build attempt
        build_tmp_dir = tempfile.mkdtemp(prefix=f"vivado_build_{ip_name}_")
        generated_ip_path = os.path.join(build_tmp_dir, ip_name) if not is_xci else build_tmp_dir # XCI regen outputs directly to build dir usually, or check TCL
        # Note: regenerate_xci_ip.tcl outputs to build_tmp_dir directly if we told it to?
        # Let's check regenerate_xci_ip.tcl:
        # set output_dir [lindex $argv 2] -> This is build_tmp_dir
        # So the IP files are directly in build_tmp_dir.
        # But wait, generate_single_fir_ip.tcl creates a subfolder `ip_name` inside output_base_dir.
        # regenerate_xci_ip.tcl uses output_dir as the base.
        # We need to unify this.
        
        # Adjust expected output path based on script behavior
        if is_xci:
            # regenerate_xci_ip.tcl generates directly into output_dir
            generated_output_dir = build_tmp_dir
        else:
            # generate_single_fir_ip.tcl creates a subdir
            generated_output_dir = os.path.join(build_tmp_dir, ip_name)

        generated_dcp_path = os.path.join(generated_output_dir, f"{ip_name}.dcp")
        
        # Copy Source file to temp dir
        local_source_file = os.path.join(build_tmp_dir, os.path.basename(source_file))
        shutil.copy2(source_file, local_source_file)

        print(f"  [Attempt {attempt}] Building in temp: {build_tmp_dir}")
        
        # Prepare Environment
        env = os.environ.copy()
        env["XILINX_LOCAL_USER_DATA"] = os.path.join(build_tmp_dir, ".Xil")

        # Run Vivado
        cmd = [
            "vivado", 
            "-mode", "batch", 
            "-source", tcl_script, 
            "-tclargs", local_source_file, ip_name, build_tmp_dir
        ]
        
        try:
            # shell=True required for Windows Vivado execution
            # CRITICAL: Run from build_tmp_dir
            result = subprocess.run(
                cmd, 
                cwd=build_tmp_dir,
                env=env,
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                text=True,
                shell=True
            )
            
            # Check if build succeeded in temp dir
            if os.path.exists(generated_dcp_path) and os.path.getsize(generated_dcp_path) > 10000:
                print(f"  ✅ Build successful in temp dir.")
                
                # Atomic-ish install
                try:
                    if os.path.exists(final_ip_dir):
                        print(f"  Removing old IP at: {final_ip_dir}")
                        shutil.rmtree(final_ip_dir, onerror=force_delete)
                        
                        # Double check
                        if os.path.exists(final_ip_dir):
                            time.sleep(1)
                            shutil.rmtree(final_ip_dir, onerror=force_delete)
                    
                    # Move
                    # If is_xci, generated_output_dir IS build_tmp_dir. We can't move the whole temp dir because it contains .Xil
                    # We need to move the *content* or just copy the relevant files?
                    # Or better: We should have told the TCL script to generate into a subdir?
                    # Actually, if we use shutil.copytree or move content...
                    
                    # Let's simplifiy: Modify regenerate_xci_ip.tcl to behave like the FIR one (make subdir)?
                    # No, let's just handle it here.
                    
                    if is_xci:
                        # Move all relevant files, or the whole dir excluding .Xil?
                        # It's cleaner to move the whole dir, but we need to ignore .Xil
                        shutil.copytree(generated_output_dir, final_ip_dir, ignore=shutil.ignore_patterns(".Xil", "vivado.log", "vivado.jou", "*.backup.log"))
                        # Note: copytree requires dest to NOT exist (which we deleted)
                    else:
                        shutil.move(generated_output_dir, final_ip_dir)
                    
                    print(f"  ✅ Installed to: {final_ip_dir}")
                    return True
                except Exception as move_err:
                    print(f"  ❌ Move/Install failed: {move_err}")
            else:
                print(f"  ❌ Build failed. DCP missing in temp dir.")
                # Save log
                fail_log_path = os.path.join(script_dir, f"vivado_fail_{ip_name}.log")
                with open(fail_log_path, "w") as f:
                    f.write(result.stdout)
                    f.write("\n=== STDERR ===\n")
                    f.write(result.stderr)
                print(f"  Log saved to: {fail_log_path}")
                print("\n".join(result.stdout.splitlines()[-10:]))
                if result.stderr:
                        print("STDERR tail:")
                        print("\n".join(result.stderr.splitlines()[-10:]))

        except Exception as e:
            print(f"  ❌ EXCEPTION: {e}")

        # Cleanup
        try:
            shutil.rmtree(build_tmp_dir, ignore_errors=True)
        except:
            pass 
        
        time.sleep(2) # Brief pause before retry

    print(f"  ⛔ FATAL: Failed to generate {ip_name}")
    return False

def main():
    # Configuration
    script_dir = os.path.dirname(os.path.abspath(__file__))
    coef_dir = os.path.join(script_dir, "fir_filter_coefs")
    
    # Scripts
    fir_tcl_script = os.path.join(script_dir, "generate_single_fir_ip.tcl")
    xci_regen_script = os.path.join(script_dir, "regenerate_xci_ip.tcl")
    
    max_retries = 2
    
    print("=" * 60)
    print("Robust All-IP Generation Script (FIR + XCI)")
    print("=" * 60)

    # 1. Identify IPs
    tasks = []
    
    # FIR IPs (COE based)
    coe_files = glob.glob(os.path.join(coef_dir, "*.coe"))
    for coe in coe_files:
        ip_name = os.path.splitext(os.path.basename(coe))[0]
        tasks.append({
            "name": ip_name,
            "source": coe,
            "tcl": fir_tcl_script,
            "is_xci": False
        })

    # Extra IPs (XCI based)
    # List of (name, relative_path_to_xci)
    extra_ips = [
        ("cic_decimate_by_4096", os.path.join(script_dir, "cic_decimate_by_4096", "cic_decimate_by_4096.xci"))
    ]
    
    for name, xci_path in extra_ips:
        if os.path.exists(xci_path):
            tasks.append({
                "name": name,
                "source": xci_path,
                "tcl": xci_regen_script,
                "is_xci": True
            })
        else:
            print(f"WARNING: XCI file for {name} not found at {xci_path}")

    print(f"Found {len(tasks)} IPs to generate.")
    
    success_count = 0
    failure_count = 0

    # 2. Process
    for task in tasks:
        if process_single_ip(
            task["name"], 
            task["source"], 
            task["tcl"], 
            task["is_xci"], 
            script_dir, 
            max_retries
        ):
            success_count += 1
        else:
            failure_count += 1

    # 3. Summary
    print("\n" + "=" * 60)
    print("Generation Summary")
    print("=" * 60)
    print(f"  Total IPs: {len(tasks)}")
    print(f"  Success  : {success_count}")
    print(f"  Failed   : {failure_count}")
    
    if failure_count == 0:
        print("\n✅ All IPs are ready.")
        sys.exit(0)
    else:
        print("\n❌ Failure.")
        sys.exit(1)

if __name__ == "__main__":
    main()
