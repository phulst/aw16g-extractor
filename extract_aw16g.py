#!/usr/bin/env python3
"""
AW16G backup extractor.

Reads a Yamaha AW16G .16G backup file and writes one mono 16-bit WAV per track
for every song that fits inside this disk image.

This is a from-scratch reimplementation of the file-format walking logic from
Jeff Leary's AWare-Audio (Tcl), fixed for two issues that prevented it from
working on this particular backup:

  1. The original assumes the user songs occupy a contiguous run of entries
     in the song-info table starting from the entry whose "offset" field is
     zero. In real backups, user songs can be sparsely scattered through the
     1000-entry table - we have to scan the whole table and pick the entries
     whose computed song-block location lands inside the file.
  2. The original has no guard against song locations computed past EOF, so
     a multi-disk backup with disks missing crashes inside binary scan.

Output: one WAV file per non-empty virtual track, organised under
out_dir/<song-name>/.
"""

import os
import struct
import sys
import wave

# ---- format constants (mirror AWare-Audio's awnamespace.tcl) ---------------

BLOCK_SIZE              = 0x20000      # 128 KB audio frame
AUDIO_FIRSTBLOCK_OFFSET = 24           # header bytes at the start of frame 0

DISKINFO_LOCATION         = 0x15800
DISKINFO_SIG              = b"CFS "
DISKINFO_SONGCOUNT_OFFSET = 0x14       # u16 big-endian
DISKINFO_DISKNUM_OFFSET   = 0x23       # u8

SONGINFO_LOCATION    = 0x35800
SONGINFO_SIZE        = 128
SONGINFO_MAX_COUNT   = 1000
SONGINFO_NAME_OFFSET = 0x00
SONGINFO_NAME_SIZE   = 24
SONGINFO_OFFSET_OFFSET = 0x20          # u32 big-endian -- block index

SONGBLOCK_LOCATION = 0x515800
SONGBLOCK_SIZE     = 0x17F800

TRACKINFO_OFFSET      = 0x800
TRACKINFO_SIZE        = 16
TRACKINFO_MAX_COUNT   = 16 * 8         # 16 tracks * 8 virtual takes
TRACKINFO_NAME_OFFSET = 0x00
TRACKINFO_NAME_SIZE   = 8
TRACKINFO_REGION_OFFSET = 0x0c         # u16 big-endian; 0xFFFF = no region

REGIONINFO_OFFSET = 0x3800
REGIONINFO_SIZE   = 48
REGIONINFO_START_OFFSET  = 0x0c        # u32: start sample in track timeline
REGIONINFO_TOTAL_OFFSET  = 0x10        # u32: total samples in this region
REGIONINFO_OFFSET_OFFSET = 0x14        # u32: samples to skip into first frame
REGIONINFO_NEXT_OFFSET   = 0x1e        # u16: next region index
REGIONINFO_MAP_OFFSET    = 0x20        # u16: first map entry index

MAPINFO_OFFSET = 0x9B000
MAPINFO_SIZE   = 8
# map entry: u16 prev, u16 next, u32 audio-frame number
MAP_NEXT_OFFSET    = 2
MAP_POINTER_OFFSET = 4

BLOCK_TERM = 0xFFFF                    # "no more" sentinel


def u8(b, off):  return struct.unpack_from(">B", b, off)[0]
def u16(b, off): return struct.unpack_from(">H", b, off)[0]
def u32(b, off): return struct.unpack_from(">I", b, off)[0]


def clean_name(raw: bytes) -> str:
    """Trim at NUL, scrub control characters, collapse whitespace."""
    name = raw.split(b"\x00", 1)[0]
    name = name.decode("ascii", errors="replace").strip()
    # Replace filesystem-hostile characters
    out = []
    for ch in name:
        if ch.isalnum() or ch in " _-.":
            out.append(ch)
        else:
            out.append("-")
    return "".join(out).strip(" -") or "untitled"


def read_at(fh, offset, length):
    fh.seek(offset)
    return fh.read(length)


def parse_disk_header(fh, file_size):
    buf = read_at(fh, DISKINFO_LOCATION, 256)
    if not buf.startswith(DISKINFO_SIG):
        raise SystemExit(f"Not a CFS/16G file - signature at 0x{DISKINFO_LOCATION:x} was {buf[:4]!r}")
    songcount = u16(buf, DISKINFO_SONGCOUNT_OFFSET)
    disknum   = u8(buf, DISKINFO_DISKNUM_OFFSET)
    print(f"Disk header: signature={buf[:8]!r}  disk_number={disknum}  songcount={songcount}")
    return songcount, disknum


def find_songs(fh, file_size, expected_count):
    """Walk the 1000-entry songinfo table; keep entries whose computed
    song-block start is inside the file and has plausible track data."""
    raw = read_at(fh, SONGINFO_LOCATION, SONGINFO_MAX_COUNT * SONGINFO_SIZE)
    songs = []
    for i in range(SONGINFO_MAX_COUNT):
        block = raw[i*SONGINFO_SIZE:(i+1)*SONGINFO_SIZE]
        name_raw = block[SONGINFO_NAME_OFFSET:SONGINFO_NAME_OFFSET+SONGINFO_NAME_SIZE]
        # skip empty slots
        if not name_raw.replace(b"\x00", b"").strip():
            continue
        offset_blocks = u32(block, SONGINFO_OFFSET_OFFSET)
        song_loc = offset_blocks * BLOCK_SIZE + SONGBLOCK_LOCATION
        if song_loc + SONGBLOCK_SIZE > file_size:
            continue                                       # data lives on another disk
        # plausibility check: track 0's name field should look like V_TR01_1
        head = read_at(fh, song_loc + TRACKINFO_OFFSET, 8)
        if not head.startswith(b"V_TR"):
            continue
        songs.append({
            "songinfo_index": i,
            "name": clean_name(name_raw),
            "location": song_loc,
            "offset_blocks": offset_blocks,
        })
    print(f"Found {len(songs)} song(s) with data in this file " +
          (f"(songcount header says {expected_count})" if expected_count is not None else ""))
    return songs


def parse_tracks(fh, song_loc):
    """Read all 128 track header entries for a song.
    Returns list of dicts with at least: index, name, region_ptr."""
    raw = read_at(fh, song_loc + TRACKINFO_OFFSET, TRACKINFO_MAX_COUNT * TRACKINFO_SIZE)
    tracks = []
    for i in range(TRACKINFO_MAX_COUNT):
        t = raw[i*TRACKINFO_SIZE:(i+1)*TRACKINFO_SIZE]
        region_ptr = u16(t, TRACKINFO_REGION_OFFSET)
        if region_ptr >= BLOCK_TERM:
            continue
        name = clean_name(t[TRACKINFO_NAME_OFFSET:TRACKINFO_NAME_OFFSET+TRACKINFO_NAME_SIZE])
        tracks.append({"index": i, "name": name, "region_ptr": region_ptr})
    return tracks


def read_region(fh, song_loc, region_id):
    loc = song_loc + REGIONINFO_OFFSET + region_id * REGIONINFO_SIZE
    buf = read_at(fh, loc, REGIONINFO_SIZE)
    return {
        "id":               region_id,
        "start_sample":     u32(buf, REGIONINFO_START_OFFSET),
        "total_samples":    u32(buf, REGIONINFO_TOTAL_OFFSET),
        "offset_samples":   u32(buf, REGIONINFO_OFFSET_OFFSET),
        "next_region":      u16(buf, REGIONINFO_NEXT_OFFSET),
        "map_ptr":          u16(buf, REGIONINFO_MAP_OFFSET),
    }


def read_map_entry(fh, song_loc, map_id):
    loc = song_loc + MAPINFO_OFFSET + map_id * MAPINFO_SIZE
    buf = read_at(fh, loc, MAPINFO_SIZE)
    return {
        "next_map":         u16(buf, MAP_NEXT_OFFSET),
        "audio_frame":      u32(buf, MAP_POINTER_OFFSET),
    }


def extract_track_pcm(fh, song_loc, track, file_size):
    """Walk a track's region list and frame map. Returns (pcm_le_bytes,
    truncated) where truncated is True if any audio frame would have been read
    past EOF (i.e. lives on a missing disk)."""
    bits = 16
    sample_bytes = bits // 8
    out = bytearray()
    samples_written_total = 0
    truncated = False

    region_ptr = track["region_ptr"]
    while region_ptr < BLOCK_TERM:
        region = read_region(fh, song_loc, region_ptr)
        if region["total_samples"] == 0 or region["start_sample"] < 0:
            region_ptr = region["next_region"]
            continue

        # Pad with silence if this region starts after where we are
        if region["start_sample"] > samples_written_total:
            gap = region["start_sample"] - samples_written_total
            out.extend(b"\x00" * gap * sample_bytes)
            samples_written_total += gap
        # Rewind logic from the original tool (rare overlap) - we just clip.
        elif region["start_sample"] < samples_written_total:
            # truncate output back to the region's start
            new_len = region["start_sample"] * sample_bytes
            del out[new_len:]
            samples_written_total = region["start_sample"]

        # Walk this region's frame map
        samples_written_in_region = 0
        first_frame = True
        map_ptr = region["map_ptr"]
        # We also need to know if this is the LAST frame in the region to
        # clip overshoot. We peek next.
        while map_ptr < BLOCK_TERM:
            entry = read_map_entry(fh, song_loc, map_ptr)
            audio_frame = entry["audio_frame"]
            # Audio frames are indexed from songblock_location (the start of
            # the disk's audio area), NOT from each song's metadata location.
            # For song 0 the two are equal because its metadata sits at the
            # start of the audio area, but for songs 1+ they diverge.
            frame_loc = SONGBLOCK_LOCATION + audio_frame * BLOCK_SIZE
            frame_size = BLOCK_SIZE
            is_last_frame_in_region = entry["next_map"] >= BLOCK_TERM

            if first_frame:
                # Skip past the offset-samples worth of header at the start of
                # the first frame in a region.
                if region["offset_samples"] < AUDIO_FIRSTBLOCK_OFFSET:
                    frame_size -= AUDIO_FIRSTBLOCK_OFFSET
                    frame_loc  += AUDIO_FIRSTBLOCK_OFFSET
                else:
                    frame_size -= region["offset_samples"] * sample_bytes
                    frame_loc  += region["offset_samples"] * sample_bytes
                first_frame = False

            if is_last_frame_in_region:
                # Don't read past the region's declared sample count.
                remaining_samples = region["total_samples"] - samples_written_in_region
                remaining_bytes = remaining_samples * sample_bytes
                if frame_size > remaining_bytes:
                    frame_size = remaining_bytes

            if frame_size > 0:
                # Ensure even sample count
                frame_size -= frame_size % sample_bytes
                if frame_loc + frame_size > file_size:
                    truncated = True
                    break
                pcm_be = read_at(fh, frame_loc, frame_size)
                if len(pcm_be) < frame_size:
                    # we ran out of file - stop cleanly
                    truncated = True
                    break
                # AW stores big-endian; WAV wants little-endian. Byteswap.
                pcm_le = bytes(struct.pack(
                    "<" + "h" * (len(pcm_be) // 2),
                    *struct.unpack(">" + "h" * (len(pcm_be) // 2), pcm_be)
                ))
                out.extend(pcm_le)
                n = len(pcm_le) // sample_bytes
                samples_written_in_region += n
                samples_written_total += n

            map_ptr = entry["next_map"]

        if truncated:
            break
        region_ptr = region["next_region"]

    return bytes(out), truncated


def write_wav(path, pcm_le_bytes, rate=44100, bits=16, channels=1):
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(pcm_le_bytes)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <AW_00000.16G> [out_dir]")
        sys.exit(1)
    src = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "extracted"
    file_size = os.path.getsize(src)
    os.makedirs(out_dir, exist_ok=True)

    with open(src, "rb") as fh:
        songcount, _ = parse_disk_header(fh, file_size)
        songs = find_songs(fh, file_size, songcount)

        any_missing = False
        used_dirs_lower = set()
        for song in songs:
            # Disambiguate names that would collide on a case-insensitive
            # filesystem (e.g. two songs named "Amber alert" and "AMBER ALERT"
            # would both write into the same folder on macOS).
            base = song["name"]
            candidate = base
            n = 2
            while candidate.lower() in used_dirs_lower:
                candidate = f"{base} ({n})"
                n += 1
            used_dirs_lower.add(candidate.lower())
            song_dir = os.path.join(out_dir, candidate)
            os.makedirs(song_dir, exist_ok=True)
            if candidate != base:
                print(f"\n>> Song '{song['name']}' at 0x{song['location']:08x}  -> {candidate!r} (name collision)")
            else:
                print(f"\n>> Song '{song['name']}' at 0x{song['location']:08x}")
            tracks = parse_tracks(fh, song["location"])
            print(f"   {len(tracks)} valid track(s)")
            song_truncated = False
            for tr in tracks:
                pcm, truncated = extract_track_pcm(fh, song["location"], tr, file_size)
                samples = len(pcm) // 2
                secs = samples / 44100
                fname = f"{tr['index']:03d}_{tr['name']}.wav"
                out_path = os.path.join(song_dir, fname)
                tag = "  [TRUNCATED -- needs next disk]" if truncated else ""
                if samples > 0:
                    write_wav(out_path, pcm)
                    print(f"   wrote {fname:30s} {samples:>10,} samples  ({secs:7.2f}s){tag}")
                else:
                    print(f"   skip  {fname:30s} (no audio on this disk){tag}")
                if truncated:
                    song_truncated = True
            if song_truncated:
                any_missing = True

        if any_missing:
            print(
                "\nNOTE: This .16G appears to be the first disk of a multi-disk backup.\n"
                "      Songs whose audio lies past the end of this file need the\n"
                "      subsequent disks (AW_00001.16G, AW_00002.16G, ...) to be\n"
                "      extracted fully."
            )

    print("\nDone.")


if __name__ == "__main__":
    main()
