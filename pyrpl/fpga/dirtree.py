#!/usr/bin/env python3
"""
Directory Tree Generator

Creates a text file containing a directory tree visualization of a specified directory,
with configurable maximum depth and directory exclusion.

Usage:
    python dirtree.py /path/to/directory [options]

Options:
    -d, --depth N       Maximum depth to traverse (default: unlimited)
    -o, --output FILE   Output file path (default: dirtree.txt)
    -e, --exclude DIR   Directory to exclude (can be used multiple times)
    -h, --help          Show this help message
"""

import os
import sys
import argparse
from pathlib import Path


def generate_tree(directory, output_file, max_depth=None, excluded_dirs=None, prefix="", depth=0):
    """
    Recursively generate a directory tree representation.
    
    Args:
        directory (str): The directory to process
        output_file (file): Open file object to write to
        max_depth (int, optional): Maximum depth to traverse
        excluded_dirs (list, optional): List of directory names to exclude
        prefix (str): Current line prefix for formatting
        depth (int): Current recursion depth
    """
    if excluded_dirs is None:
        excluded_dirs = []
    
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
        if entry.is_dir() and entry.name not in excluded_dirs:
            generate_tree(entry.path, output_file, max_depth, excluded_dirs, next_prefix, depth + 1)


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Generate a directory tree text file")
    parser.add_argument("directory", help="Directory to generate tree from")
    parser.add_argument("-d", "--depth", type=int, help="Maximum depth to traverse")
    parser.add_argument("-o", "--output", default="dirtree.txt", help="Output file path")
    parser.add_argument("-e", "--exclude", action="append", default=[], 
                        help="Directory name to exclude (can be used multiple times)")
    
    args = parser.parse_args()
    
    # Validate directory
    directory = Path(args.directory)
    if not directory.exists() or not directory.is_dir():
        print(f"Error: '{directory}' is not a valid directory", file=sys.stderr)
        sys.exit(1)
    
    # Create output file
    try:
        with open(args.output, 'w', encoding='utf-8') as f:
            # Write header
            f.write(f"Directory Tree: {directory}\n")
            f.write("=" * (len(str(directory)) + 15) + "\n\n")
            
            # Write tree
            dir_name = os.path.basename(directory) or directory
            f.write(f"{dir_name}\n")
            generate_tree(directory, f, args.depth, args.exclude)
            
            print(f"Directory tree saved to {args.output}")
    
    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()