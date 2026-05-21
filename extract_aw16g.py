#!/usr/bin/env python3
"""
AW16G backup extractor.

Reads a Yamaha AW16G .16G backup file (or set of files for a multi-disk
backup) and writes one mono 16-bit WAV per virtual track, organised under
out_dir/<song-name>/.

This is a from-scratch reimplementation of the file-format walking logic from
Jeff Leary's AWare-Audio (Tcl), with three differences:

  1. Robust song-info walk: the AWare-Audio AW16G code path assumes user
     songs occupy a contiguous run of entries in the 1000-entry song-info
     table starting from the entry whose "offset" field is zero. In real
     backups they're sparsely scattered (factory presets and stale slots
     interleave), so we scan the whole table and keep entries whose computed
     song-block location lands inside the file and has a plausible track
     header.
  2. Correct audio-frame base for songs after the first: AWare-Audio bases
     frame offsets on the song's metadata location, which is right only for
     song 0. We use the fixed SONGBLOCK_LOCATION constant instead.
  3. Multi-disk support: pass any disk's path and we auto-discover sibling
     files (AW_00000.16G, AW_00001.16G, ...). Each audio-frame read is
     routed to the disk that holds the frame based on the disk-info header
     fields (previous_frames, offset_frames, max_frames).
"""

import os
import re
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
    """Lightweight header parse — used to print a summary line. Returns
    (songcount, disknum). For full multi-disk header parsing, see open_disk_file."""
    buf = read_at(fh, DISKINFO_LOCATION, 256)
    if not buf.startswith(DISKINFO_SIG):
        raise SystemExit(f"Not a CFS/16G file - signature at 0x{DISKINFO_LOCATION:x} was {buf[:4]!r}")
    songcount = u16(buf, DISKINFO_SONGCOUNT_OFFSET)
    disknum   = u8(buf, DISKINFO_DISKNUM_OFFSET)
    print(f"Disk header: signature={buf[:8]!r}  disk_number={disknum}  songcount={songcount}")
    return songcount, disknum


# ---- multi-disk handling ---------------------------------------------------

DISKINFO_MAXFRAMES_OFFSET = 0x2a   # s16 BE
DISKINFO_PREVFRAMES_OFFSET = 0x2c  # s16 BE -- -1 means "this isn't disk 0"
DISKINFO_OFFSETFRAMES_OFFSET = 0x2e  # s16 BE


def open_disk_file(path):
    """Open one .16G file and read its disk header.
    Returns a dict describing the disk."""
    fh = open(path, "rb")
    sz = os.path.getsize(path)
    fh.seek(DISKINFO_LOCATION)
    hdr = fh.read(256)
    if not hdr.startswith(DISKINFO_SIG):
        fh.close()
        raise SystemExit(f"{path}: bad CFS signature at 0x{DISKINFO_LOCATION:x}")
    songcount  = u16(hdr, DISKINFO_SONGCOUNT_OFFSET)
    disknum    = u8(hdr, DISKINFO_DISKNUM_OFFSET)
    max_frames = struct.unpack(">h", hdr[DISKINFO_MAXFRAMES_OFFSET:DISKINFO_MAXFRAMES_OFFSET+2])[0]
    prev_fr    = struct.unpack(">h", hdr[DISKINFO_PREVFRAMES_OFFSET:DISKINFO_PREVFRAMES_OFFSET+2])[0]
    off_fr     = struct.unpack(">h", hdr[DISKINFO_OFFSETFRAMES_OFFSET:DISKINFO_OFFSETFRAMES_OFFSET+2])[0]
    # Audio data base. On disk 0 it's the fixed SONGBLOCK_LOCATION (audio sits
    # after the song-info table). On disks >=1 there's no song-info table, so
    # the audio fills the tail of the file: base = file_size - max_frames*BLOCK_SIZE.
    if disknum == 0:
        audio_base = SONGBLOCK_LOCATION
    else:
        audio_base = sz - max_frames * BLOCK_SIZE
    return {
        "path":            path,
        "fh":              fh,
        "size":            sz,
        "disknum":         disknum,
        "songcount":       songcount,
        "max_frames":      max_frames,
        "previous_frames": prev_fr,
        "offset_frames":   off_fr,
        "audio_base":      audio_base,
    }


def open_disks(first_path):
    """Open the given .16G plus any sibling disks (incrementing numeric suffix).
    Returns a list of disk dicts in [disk 0, disk 1, ...] order.

    The AW-16G names backup files like AW_00000.16G, AW_00001.16G, ... but
    users sometimes rename them (e.g. "anna_1.16g", "anna_2.16g"). We extract
    the trailing numeric suffix from the input filename and probe for siblings
    with that integer incremented — so the numeric suffix in the filename
    doesn't have to match the disknum field inside the file. macOS
    case-insensitivity is handled by trying common case variants."""
    disks = [open_disk_file(first_path)]
    if disks[0]["disknum"] != 0:
        print(f"Note: {first_path} reports disknum={disks[0]['disknum']}, not 0; only this file will be read.\n"
              f"      For multi-disk extraction, point the script at the first (disknum=0) file.\n")
        return disks
    dirname  = os.path.dirname(first_path) or "."
    basename = os.path.basename(first_path)
    name, ext = os.path.splitext(basename)
    m = re.search(r"(\d+)$", name)
    if not m:
        return disks
    prefix, digits = name[:m.start()], m.end() - m.start()
    # Sibling search starts at the suffix found in the input filename + 1, so
    # we never re-open the same file as disk 0.
    fname_idx = int(m.group(1)) + 1
    first_path_abs = os.path.abspath(first_path)
    while True:
        cand_name = f"{prefix}{fname_idx:0{digits}d}{ext}"
        # Try the natural case first, then upper / lower as fallbacks. Using
        # a list rather than a set keeps the probe order deterministic.
        seen = set()
        variants = []
        for v in (cand_name, cand_name.upper(), cand_name.lower()):
            if v not in seen:
                variants.append(v)
                seen.add(v)
        cand_path = None
        for v in variants:
            full = os.path.join(dirname, v)
            if os.path.exists(full) and os.path.abspath(full) != first_path_abs:
                cand_path = full
                break
        if cand_path is None:
            break
        d = open_disk_file(cand_path)
        expected_disknum = len(disks)   # we have N disks so far; next should be #N
        if d["disknum"] != expected_disknum:
            print(f"WARNING: {cand_path} reports disknum={d['disknum']}, expected {expected_disknum}; using anyway")
        disks.append(d)
        fname_idx += 1
    return disks


def find_frame_on_disks(disks, global_frame, extent=BLOCK_SIZE):
    """Locate the (disk, file_offset) for a global frame number (used for
    both audio frames and song-metadata blocks, which live in the same
    address space). Returns None if no disk in the set holds that frame, or
    if `extent` bytes from the resolved offset would run past the disk's EOF.

    Routing rule per disk:
        adj = global_frame + (offset_frames - 1) if previous_frames == -1
              global_frame                       otherwise (disk 0)
        valid on this disk iff 0 <= adj < max_frames
        file offset = audio_base + adj * BLOCK_SIZE

    The "-1" on disks with previous_frames==-1 is empirical: the disk-info
    header stores an `offset_frames` that's one off from the actual shift
    needed to align song-metadata blocks (and audio frames) across disks.
    Verified against song-block locations on a 4-disk backup where the
    songinfo offset_blocks values 5626/8624/11325/15309/20327 line up
    exactly with the local frame numbers 46/3044/125/4109/3507 on disks
    1/1/2/2/3."""
    for d in disks:
        if d["previous_frames"] == -1:
            adj = global_frame + d["offset_frames"] - 1
        else:
            adj = global_frame
        if 0 <= adj < d["max_frames"]:
            off = d["audio_base"] + adj * BLOCK_SIZE
            if off + extent <= d["size"]:
                return d, off
    return None


def looks_like_song(fh, song_loc):
    """Plausibility check for a song-metadata block. Requires:

      - Track 0's name is non-empty printable ASCII (8-byte field, possibly
        null-padded). On the AW-16G this is the default 'V_TR01_1' or a
        user-renamed string. For stale-slot locations the trackinfo area is
        usually raw audio samples and the chance of 8 consecutive printable
        bytes is ~0.04%.
      - At least one of the 128 track-info entries has a valid region
        pointer (< 0xFFFF).
      - Not ALL 128 entries pass the region-pointer check. A real song has
        many 0xFFFF entries (unrecorded virtual takes); a stale slot whose
        random data happens to satisfy the name check is also overwhelmingly
        likely to have all 128 region_ptrs < 0xFFFF (~99.997% per slot).
    """
    raw = read_at(fh, song_loc + TRACKINFO_OFFSET, TRACKINFO_MAX_COUNT * TRACKINFO_SIZE)
    if len(raw) < TRACKINFO_MAX_COUNT * TRACKINFO_SIZE:
        return False
    name0 = raw[TRACKINFO_NAME_OFFSET:TRACKINFO_NAME_OFFSET+TRACKINFO_NAME_SIZE]
    trimmed = name0.rstrip(b"\x00")
    if not trimmed or not all(32 <= c < 127 for c in trimmed):
        return False
    valid_regions = sum(
        1
        for i in range(TRACKINFO_MAX_COUNT)
        if u16(raw, i * TRACKINFO_SIZE + TRACKINFO_REGION_OFFSET) < BLOCK_TERM
    )
    return 1 <= valid_regions < TRACKINFO_MAX_COUNT


def find_songs(disks, expected_count):
    """Walk the 1000-entry songinfo table on disk 0. For each non-empty entry,
    route the song's metadata block (offset_blocks) to whichever disk holds
    it; songs that fail the route are listed as 'skipped' (their data lives
    on a disk we don't have). Songs that route to a disk but fail the
    track-info plausibility check are silently dropped (stale slots)."""
    fh0 = disks[0]["fh"]
    raw = read_at(fh0, SONGINFO_LOCATION, SONGINFO_MAX_COUNT * SONGINFO_SIZE)
    songs = []
    skipped = []
    for i in range(SONGINFO_MAX_COUNT):
        block = raw[i*SONGINFO_SIZE:(i+1)*SONGINFO_SIZE]
        name_raw = block[SONGINFO_NAME_OFFSET:SONGINFO_NAME_OFFSET+SONGINFO_NAME_SIZE]
        if not name_raw.replace(b"\x00", b"").strip():
            continue
        offset_blocks = u32(block, SONGINFO_OFFSET_OFFSET)
        located = find_frame_on_disks(disks, offset_blocks, extent=SONGBLOCK_SIZE)
        if located is None:
            skipped.append((i, clean_name(name_raw), offset_blocks))
            continue
        disk, song_loc = located
        if not looks_like_song(disk["fh"], song_loc):
            continue
        songs.append({
            "songinfo_index": i,
            "name": clean_name(name_raw),
            "disk": disk,
            "location": song_loc,
            "offset_blocks": offset_blocks,
        })
    if skipped:
        print(f"\nNote: {len(skipped)} song-info entr{'y' if len(skipped)==1 else 'ies'} "
              "point past the disks we have (likely stale/old entries from "
              "previous backups):")
        for i, name, ob in skipped[:8]:
            print(f"  - {name!r} (songinfo idx {i}, offset_blocks=0x{ob:x})")
        if len(skipped) > 8:
            print(f"  ... and {len(skipped) - 8} more")
        print()
    print(f"Found {len(songs)} song(s) " +
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


def extract_track_pcm(disks, song, track):
    """Walk a track's region list and frame map. Returns (pcm_le_bytes,
    truncated). truncated is True if any audio frame referenced by this track
    is not present on any of the disks we have.

    Metadata (regions, maps) is read from the disk that holds this song's
    metadata block (song["disk"]). Audio frames are routed independently via
    find_frame_on_disks(), since they can live on different disks."""
    meta_fh = song["disk"]["fh"]
    song_loc = song["location"]
    bits = 16
    sample_bytes = bits // 8
    out = bytearray()
    samples_written_total = 0
    truncated = False

    region_ptr = track["region_ptr"]
    while region_ptr < BLOCK_TERM:
        region = read_region(meta_fh, song_loc, region_ptr)
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
        while map_ptr < BLOCK_TERM:
            entry = read_map_entry(meta_fh, song_loc, map_ptr)
            audio_frame = entry["audio_frame"]
            is_last_frame_in_region = entry["next_map"] >= BLOCK_TERM

            located = find_frame_on_disks(disks, audio_frame)
            if located is None:
                truncated = True
                break
            disk, frame_loc = located
            frame_size = BLOCK_SIZE

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
                frame_size -= frame_size % sample_bytes
                if frame_loc + frame_size > disk["size"]:
                    truncated = True
                    break
                pcm_be = read_at(disk["fh"], frame_loc, frame_size)
                if len(pcm_be) < frame_size:
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
    os.makedirs(out_dir, exist_ok=True)

    disks = open_disks(src)
    fh0   = disks[0]["fh"]
    sz0   = disks[0]["size"]
    print(f"Opened {len(disks)} disk file(s):")
    for d in disks:
        print(f"  disk {d['disknum']}: {os.path.basename(d['path'])}  size={d['size']:,}  "
              f"max_frames={d['max_frames']}  prev={d['previous_frames']}  off={d['offset_frames']}  "
              f"audio_base=0x{d['audio_base']:x}")
    print()

    try:
        songcount, _ = parse_disk_header(fh0, sz0)
        songs = find_songs(disks, songcount)

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
            loc_label = f"disk {song['disk']['disknum']} @ 0x{song['location']:08x}"
            if candidate != base:
                print(f"\n>> Song '{song['name']}' ({loc_label})  -> {candidate!r} (name collision)")
            else:
                print(f"\n>> Song '{song['name']}' ({loc_label})")
            tracks = parse_tracks(song["disk"]["fh"], song["location"])
            print(f"   {len(tracks)} valid track(s)")
            song_truncated = False
            for tr in tracks:
                pcm, truncated = extract_track_pcm(disks, song, tr)
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
            next_idx = max(d["disknum"] for d in disks) + 1
            print(
                f"\nNOTE: Some songs reference audio frames that aren't on any of the\n"
                f"      {len(disks)} disk file(s) we have. This backup probably continues\n"
                f"      onto disk {next_idx} (AW_{next_idx:05d}.16G). Put that file next\n"
                f"      to disk 0 and re-run to finish extracting the truncated tracks."
            )
    finally:
        for d in disks:
            d["fh"].close()

    print("\nDone.")


if __name__ == "__main__":
    main()
