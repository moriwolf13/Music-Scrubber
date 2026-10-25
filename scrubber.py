import os
import shutil
import tempfile
import zipfile
from mutagen import File
import ntpath

#Static variables
inbox_folder_name: str = "Inbox"
backup_folder_name: str = ".backups"
library_folder_name: str = "Music Library"
album_art_types: tuple = (".jpg", ".png")
music_file_types: tuple = (".mp3", ".flac")

#Set safety limits for ZIP archives
MAX_ZIP_UNCOMPRESSED_BYTES = 10 * 1024**3  # 10 GiB
MAX_ZIP_MEMBERS = 10_000

#File routing
script_folder = os.path.dirname(os.path.abspath(__file__))
drive_folder = os.path.dirname(script_folder)
inbox_folder = os.path.join(script_folder, inbox_folder_name)
backup_folder = os.path.join(script_folder, backup_folder_name)
music_library_folder = os.path.join(drive_folder, library_folder_name)

#Create required folders if they don't exist
os.makedirs(inbox_folder, exist_ok=True)
os.makedirs(backup_folder, exist_ok=True)
os.makedirs(music_library_folder, exist_ok=True)

#Defining inbox files
inbox_files = os.listdir(inbox_folder)

#Backup formatting function
def add_to_artist_backup(source_path, artist, album, member_name):
    safe_artist = artist.strip().replace("/", "_")
    safe_album = album.strip().replace("/", "_")

    artist_zip_path = os.path.join(backup_folder, f"{safe_artist}.zip")
    archive_member_path = f"{safe_album}/{member_name}"

    with zipfile.ZipFile(artist_zip_path, "a") as artist_archive:
        if archive_member_path not in artist_archive.namelist():
            artist_archive.write(source_path, arcname=archive_member_path)

#Reject unsafe ZIP paths and archives that exceed the safety limits
def check_zip_safety(archive, temp_folder, filename):
    members = archive.infolist()

    if len(members) > MAX_ZIP_MEMBERS:
        raise ValueError(f"Too many files in ZIP: {filename}")

    safe_root = os.path.abspath(temp_folder)
    total_uncompressed_size = 0

    for member in members:
        member_name = member.filename.replace("\\", "/")
        drive, _ = ntpath.splitdrive(member_name)
        path_parts = member_name.split("/")

        if (
            not member_name
            or drive
            or member_name.startswith("/")
            or ".." in path_parts
        ):
            raise ValueError(f"Unsafe path in {filename}: {member.filename}")

        destination = os.path.abspath(os.path.join(safe_root, *path_parts))

        try:
            stays_inside_temp = (
                os.path.commonpath((safe_root, destination)) == safe_root
            )
        except ValueError:
            stays_inside_temp = False

        if not stays_inside_temp:
            raise ValueError(f"Unsafe path in {filename}: {member.filename}")

        total_uncompressed_size += member.file_size
        if total_uncompressed_size > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise ValueError(f"ZIP expands beyond the 10 GiB limit: {filename}")

#Scan loop
for filename in inbox_files:

    #Defines zip files and music file types
    is_zip = filename.lower().endswith(".zip")
    is_music_file = filename.lower().endswith(music_file_types)

    if not is_zip and not is_music_file:
        print(f"Unsupported Inbox file, leaving it alone: {filename}")
        continue

    file_path = os.path.join(inbox_folder, filename)

    #Empty folder name data for clean backup reading
    backup_album = None
    backup_artist = None

    #Main file loop
    with tempfile.TemporaryDirectory() as temp_folder:
        if is_zip: # Check the ZIP before testing or extracting its contents
            with zipfile.ZipFile(file_path, "r") as archive:
                check_zip_safety(archive, temp_folder, filename)

                bad_file = archive.testzip()
                if bad_file is not None:
                    raise ValueError(f"Damaged file in {filename}: {bad_file}")

                archive.extractall(temp_folder)

        else:
            shutil.copy2(file_path, temp_folder)

        #Empty lists for sorting
        cover_files: list = []
        music_files: list = []
        unsupported_files: list = []

        #Album scrape for tracks and images
        for folder_path, subfolder_names, file_names in os.walk(temp_folder):
            for file_name in file_names:
                item_path = os.path.join(folder_path, file_name)

                #Seperate album art and tracks
                if file_name.lower().endswith(album_art_types):
                    cover_files.append(item_path)
                elif file_name.lower().endswith(music_file_types):
                    music_files.append(item_path)
                else:
                    unsupported_files.append(item_path)

        # These checks happen after the scan, once per ZIP
        for item_path in unsupported_files:
            print(f"Unsupported file, skipping: {os.path.basename(item_path)}")

        if not music_files:
            raise ValueError(f"No supported music files found in {filename}")

        #Individual track scraping for sorting
        for track_path in music_files:
            audio = File(track_path, easy=True)

            #Check for empty files
            if audio is None:
                raise ValueError(f"Mutagen could not identify audio file: {track_path}")

            if audio.tags is None:
                raise ValueError(f"No tags found in {os.path.basename(track_path)}")

            required_tags = ("artist", "album", "title")

            for tag_name in required_tags:
                values = audio.tags.get(tag_name)
                if not values or not values[0].strip():
                    raise ValueError(
                        f"Missing or empty {tag_name} tag in {os.path.basename(track_path)}"
                    )

           # Check for multiple discs
            disc_tag = audio.tags.get("discnumber")
            disc_parts = None
            total_discs = None

            if disc_tag and "/" in str(disc_tag[0]):
                disc_parts = str(disc_tag[0]).split("/", 1)
                total_discs = int(disc_parts[1])

                # Remove the disc tag only for single-disc albums
                if total_discs == 1:
                    del audio.tags["discnumber"]

            #Tag Scraping
            artist = audio.tags["artist"][0]
            album = audio.tags["album"][0]
            title = audio.tags["title"][0]
            track_file_extension = os.path.splitext(track_path)[1]

            #Capture album details for backup file renaming
            if backup_artist is None:
                album_artist_tag = audio.tags.get("albumartist")

                if album_artist_tag:
                    backup_artist = album_artist_tag[0].strip()
                else:
                    backup_artist = artist.strip()

                backup_album = album.strip()

            #Clean Tags
            clean_artist = artist.strip()
            clean_album = album.strip()
            clean_title = title.strip()

            #Filename length check
            shortened_title = title[:60].replace("/", "_")

            #Add original track to artist backup .zip, preserving multi-disc track seperation
            backup_member_name = shortened_title + track_file_extension

            if disc_parts is not None and total_discs is not None and total_discs > 1:
                backup_member_name = f"Disc {disc_parts[0]}/{backup_member_name}"

            add_to_artist_backup(
                track_path,
                backup_artist,
                backup_album,
                backup_member_name,
            )

            #Writing clean tags to audio tracks
            audio.tags["artist"] = [clean_artist]
            audio.tags["album"] = [clean_album]
            audio.tags["title"] = [clean_title]
            audio.save()

            #Filepath creation
            album_folder = os.path.join(music_library_folder, artist, album)
            destination_file = os.path.join(album_folder, shortened_title + track_file_extension)

            #Copy Message
            print(f"Copying Artist: {artist} | Album: {album} | Track: {title}")

            #Create Artist -> Album folder
            os.makedirs(album_folder, exist_ok=True)

            for cover_path in cover_files:
                cover_filename = os.path.basename(cover_path)
                cover_destination = os.path.join(album_folder, cover_filename)

                if not os.path.exists(cover_destination):
                    shutil.copy2(cover_path, cover_destination)
                    print(f"Copied cover: {cover_destination}")

            #Copy to Library
            if os.path.exists(destination_file):
                print(f"Already exists; skipping: {destination_file}")
            else:
                shutil.copy2(track_path, destination_file)
                print(f"Copied: {destination_file}")

        # Add covers and extra files to the artist backup ZIP
        if backup_artist and backup_album:
            for cover_path in cover_files:
                cover_name = os.path.basename(cover_path)
                add_to_artist_backup(
                    cover_path,
                    backup_artist,
                    backup_album,
                    cover_name,
                )

            for extra_path in unsupported_files:
                relative_path = os.path.relpath(extra_path, temp_folder)
                extra_name = "Extras/" + relative_path.replace(os.sep, "/")
                add_to_artist_backup(
                    extra_path,
                    backup_artist,
                    backup_album,
                    extra_name,
                        )

    #Inbox cleaning
    os.remove(file_path)
    print(f"{filename} processed and removed from Inbox")

print("Supported Inbox items processed!")
print("Sorting complete!")
