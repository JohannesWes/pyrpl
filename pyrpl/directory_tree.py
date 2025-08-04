import os
import argparse

def generate_tree(start_path='.', max_depth=3):
    tree_lines = []

    header = (
        "# Directory Tree Listing\n"
        "# Each line is prefixed with a number indicating its depth level in the tree.\n"
        "# Example:\n"
        "# 0root/\n"
        "# 1    folder1/\n"
        "# 2        file.txt\n"
        "# Depth levels increase with nesting.\n"
        "\n"
    )

    def walk(dir_path, depth):
        if depth > max_depth:
            return
        try:
            entries = sorted(os.listdir(dir_path))
        except PermissionError:
            tree_lines.append(f"{depth}{'    ' * depth}[Permission Denied] {dir_path}")
            return

        for entry in entries:
            full_path = os.path.join(dir_path, entry)
            prefix = f"{depth}{'    ' * depth}"
            tree_lines.append(f"{prefix}{entry}/" if os.path.isdir(full_path) else f"{prefix}{entry}")
            if os.path.isdir(full_path):
                walk(full_path, depth + 1)

    walk(start_path, 0)
    return header + '\n'.join(tree_lines)

def main():
    parser = argparse.ArgumentParser(description='Generate a directory tree up to a specified depth.')
    parser.add_argument('-d', '--depth', type=int, default=3, help='Maximum depth of directory tree (default: 3)')
    args = parser.parse_args()

    tree = generate_tree('.', args.depth)
    with open('directory_tree.txt', 'w', encoding='utf-8') as f:
        f.write(tree)

    print(f"Directory tree up to depth {args.depth} written to directory_tree.txt")

if __name__ == '__main__':
    main()
