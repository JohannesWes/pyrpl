import os
import shutil

def convert_files_to_txt(source_folder, destination_folder):
    """
    Copies .v and .sv files from a source folder to a destination folder,
    renaming them with a .txt extension.

    Args:
        source_folder: The path to the source folder (e.g., "rtl").
        destination_folder: The path to the destination folder (will be created if it doesn't exist).
    """

    # Create the destination folder if it doesn't exist
    if not os.path.exists(destination_folder):
        os.makedirs(destination_folder)

    # Iterate through files in the source folder
    for filename in os.listdir(source_folder):
        if filename.endswith((".v", ".sv")):  # Check for .v and .sv files
            # Construct the full source file path
            source_file_path = os.path.join(source_folder, filename)

            # Construct the new filename with a .txt extension
            base_name, _ = os.path.splitext(filename)  # Split filename and extension
            new_filename = base_name + ".txt"

            # Construct the full destination file path
            destination_file_path = os.path.join(destination_folder, new_filename)

            # Copy the file and rename it
            shutil.copy2(source_file_path, destination_file_path)  # copy2 preserves metadata
            print(f"Copied '{source_file_path}' to '{destination_file_path}'")



if __name__ == "__main__":
    source_directory = "rtl"  # The folder containing your .v and .sv files
    destination_directory = "rtl_txt" # Name your output directory here

    # Check if source directory exists.  Important to avoid errors.
    if not os.path.exists(source_directory):
        print(f"Error: Source directory '{source_directory}' not found.")
    else:
         convert_files_to_txt(source_directory, destination_directory)

    print("File conversion and copying complete.")