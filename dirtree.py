#!/usr/bin/env python3
"""
Directory Tree Generator

Creates a text file containing a directory tree visualization of a specified directory,
with configurable maximum depth and directory exclusion.

Usage:
    python dirtree.py /path/to/directory [options]
    python dirtree.py --config CONFIG_NAME [options]

Options:
    -d, --depth N       Maximum depth to traverse (default: unlimited)
    -o, --output FILE   Output file path (default: dirtree.txt)
    -e, --exclude DIR   Directory to exclude (can be used multiple times)
    -c, --config NAME   Use a predefined configuration
    -h, --help          Show this help message
"""

import os
import sys
import argparse
from pathlib import Path


def generate_tree(directory, output_file, max_depth=None, excluded_dirs=None, prefix="", depth=0, base_dir=None):
    """
    Recursively generate a directory tree representation.

    Args:
        directory (str): The directory to process
        output_file (file): Open file object to write to
        max_depth (int, optional): Maximum depth to traverse
        excluded_dirs (list, optional): List of directory patterns to exclude
        prefix (str): Current line prefix for formatting
        depth (int): Current recursion depth
        base_dir (str): Base directory for relative path calculations
    """
    if excluded_dirs is None:
        excluded_dirs = []

    if base_dir is None:
        base_dir = directory

    # Check if we've reached max depth
    if max_depth is not None and depth > max_depth:
        output_file.write(f"{prefix}└── [...]\n")
        return

    # Get sorted directory contents
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
        entries_count = len(entries)
    except PermissionError:
        output_file.write(f"{prefix}└── [Permission Denied]\n")
        return
    except Exception as e:
        output_file.write(f"{prefix}└── [Error: {str(e)}]\n")
        return

    # Process each entry
    for i, entry in enumerate(entries):
        is_last = i == entries_count - 1

        # Determine line characters
        if is_last:
            connector = "└── "
            next_prefix = prefix + "    "
        else:
            connector = "├── "
            next_prefix = prefix + "│   "

        # Write current entry
        output_file.write(f"{prefix}{connector}{entry.name}\n")

        # Recurse into directories if not excluded
        if entry.is_dir():
            # Generate relative path from base directory
            rel_path = os.path.relpath(entry.path, base_dir).replace('\\', '/')

            # Check if this directory should be excluded
            should_exclude = False
            for excluded_dir in excluded_dirs:
                # Check for exact match or directory pattern match
                if rel_path == excluded_dir or entry.name == excluded_dir:
                    should_exclude = True
                    break

            if not should_exclude:
                generate_tree(entry.path, output_file, max_depth, excluded_dirs,
                              next_prefix, depth + 1, base_dir)


def main():
    # Predefined configurations
    configs = {
        "pyrpl": {
            "directory": r"C:\Users\aj92uwef\PycharmProjects\pyrpl_new",
            "depth": 3,
            "exclude": ["pyrpl/test", "aqt_local", ".venv310", ".docker", "docs", "pyrpl.egg-info", ".git", ".idea",
                        "pyrpl/fpga/GEMINI INPUT FILES", "pyrpl/fpga/rtl_txt"],
            "output": "dirtree_pyrpl.txt"
        },
        "fpga": {
            "directory": r"C:\Users\aj92uwef\PycharmProjects\pyrpl_new\pyrpl\fpga",
            "depth": 3,
            "exclude": ["pyrpl/test", "aqt_local", ".venv310", ".docker", "docs", "pyrpl.egg-info", ".git", ".idea",
                        "pyrpl/fpga/GEMINI INPUT FILES", "pyrpl/fpga/rtl_txt"],
            "output": "dirtree_pyrpl_fpga.txt"
        }
    }

    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Generate a directory tree text file")
    parser.add_argument("directory", nargs="?", help="Directory to generate tree from")
    parser.add_argument("-c", "--config", choices=configs.keys(), help="Use a predefined configuration")
    parser.add_argument("-d", "--depth", type=int, help="Maximum depth to traverse")
    parser.add_argument("-o", "--output", help="Output file path")
    parser.add_argument("-e", "--exclude", action="append", default=[],
                        help="Directory name to exclude (can be used multiple times)")

    args = parser.parse_args()

    # Apply predefined configuration if specified
    if args.config:
        config = configs[args.config]
        directory = Path(config["directory"])
        depth = config["depth"]
        output = config["output"]
        exclude = config["exclude"]

        # Override with command-line arguments if provided
        if args.directory:
            directory = Path(args.directory)
        if args.depth is not None:
            depth = args.depth
        if args.output:
            output = args.output
        if args.exclude:
            exclude = args.exclude
    else:
        # Use command-line arguments
        if not args.directory:
            parser.error("directory is required when no configuration is specified")
        directory = Path(args.directory)
        depth = args.depth
        output = args.output or "dirtree.txt"
        exclude = args.exclude

    # Validate directory
    if not directory.exists() or not directory.is_dir():
        print(f"Error: '{directory}' is not a valid directory", file=sys.stderr)
        sys.exit(1)

    # Create output file
    try:
        with open(output, 'w', encoding='utf-8') as f:
            # Write header
            f.write(f"Directory Tree: {directory}\n")
            f.write("=" * (len(str(directory)) + 15) + "\n\n")

            # Write tree
            dir_name = os.path.basename(str(directory)) or str(directory)
            f.write(f"{dir_name}\n")
            generate_tree(directory, f, depth, exclude, base_dir=directory)

            print(f"Directory tree saved to {output}")

    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()