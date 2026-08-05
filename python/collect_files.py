import os
import shutil
from pathlib import Path

def main():
    print("\n------------------- SCRIPT FUNCTION --------------------")
    print('This script searchs a source directory by file extension')
    print('and moves all matching files to the destination directory.')
    print('It supports forward slashes, backslashes, and extensions')
    print('with or without a leading dot (e.g., .ext or ext).')
    print('Paths can not have quotation marks.')
    print("--------------------------------------------------------\n")
    print("\n------------------- FILE EXTENSIONS -------------------")
    print('Videos: mp4, avi, mkv, mov, wmv, flv, webm, ts, mpeg, mpg, 3gp, rmvb, vob, ogv, mts, m4v')
    print('')
    print('Images: jpg, jpeg, png, gif, bmp, tiff, webp, svg, raw, heic, psd, ai')
    print('')
    print('Audios: mp3, wav, flac, aac, ogg, wma, alac, opus, aif, m4a, amr, mid')
    print("--------------------------------------------------------\n")
    print('Example: C:/Source/Directory/My Files, D:/Destination/Directory/Moved_Files, txt, pdf, jpg')

    user_input = input("Input: ")

    parts = user_input.split(',')

    if len(parts) < 3:
        print("Error: Please provide at least a Source, Destination, and one file extension.")
        return

    source_dir = parts[0].strip()
    dest_dir = parts[1].strip()

    raw_extensions = parts[2:]
    
    extensions = []
    for ext in raw_extensions:
        # Tolerate pasted quotes and a missing leading dot.
        clean_ext = ext.strip().strip('"').strip("'")
        if clean_ext:
            extensions.append(f".{clean_ext}")

    source_path = Path(source_dir)
    dest_path = Path(dest_dir)

    if not source_path.exists():
        print(f"Error: Source directory '{source_dir}' not found.")
        return

    if not dest_path.exists():
        dest_path.mkdir(parents=True, exist_ok=True)
        print(f"Created destination directory: '{dest_dir}'")

    for root, dirs, files in os.walk(source_path):
        for file in files:
            file_ext = os.path.splitext(file)[1]
            
            if file_ext.lower() in extensions:
                src_file = os.path.join(root, file)
                dst_file = dest_path / file
                
                if dst_file.exists():
                    base, ext = os.path.splitext(file)
                    counter = 1
                    new_filename = f"{base}_{counter}{ext}"
                    new_dst_file = dest_path / new_filename
                    
                    while new_dst_file.exists():
                        counter += 1
                        new_filename = f"{base}_{counter}{ext}"
                        new_dst_file = dest_path / new_filename
                    
                    print(f"Renamed and moved: {file} -> {new_filename}")
                    shutil.move(src_file, new_dst_file)
                else:
                    shutil.move(src_file, dst_file)

if __name__ == "__main__":
    main()
