# aw16g-extractor

Extract audio tracks from Yamaha AW-16G `.16G` backup files as WAV.

A small, single-file Python 3 script for recovering audio from the proprietary
backup format the Yamaha AW-16G Professional Audio Workstation writes to CD.
No dependencies — standard library only.

## Why this exists

The only public extractor for these files is
[AWare-Audio](https://github.com/jeffleary00/AWare-Audio) by Jeff Leary
(SillyMonkey Software), a Tcl/Tk tool first written for the AW4416/AW2816 and
later extended to the AW16G. The format constants and walking strategy used
here come from that project. Without it this script wouldn't exist — go give
it a star.

However, the AW16G code path in AWare-Audio crashes on real-world backups
because of two bugs:

1. It assumes user songs occupy *consecutive* entries in the AW16G's
   1000-entry song-info table, starting at the entry whose `offset` field is
   zero. In practice user songs are sparsely scattered among that table
   (which also contains factory presets and stale slots), so the second and
   third "songs" the tool picks up are usually junk.
2. There's no bounds check on the final song-block location, so those junk
   entries produce seeks past EOF and the next `binary scan` crashes with
   `can't use empty string as operand of "&"`.

This script scans the whole table and keeps entries whose computed song-block
start (a) lies inside the file and (b) contains plausible `V_TR01_1` track
headers, which sidesteps both issues.

## Requirements

- Python 3.x. Standard library only.

## Usage

```sh
python3 extract_aw16g.py <path-to-.16G-file> [output-directory]
```

Output directory defaults to `./extracted`.

Example:

```sh
$ python3 extract_aw16g.py AW_00000.16G
Disk header: signature=b'CFS 3.00'  disk_number=0  songcount=3
Found 3 song(s) with data in this file (songcount header says 3)

>> Song 'Just a girl' at 0x00515800
   9 valid track(s)
   wrote 000_V_TR01_1.wav               18,339,264 samples  ( 415.86s)
   wrote 008_V_TR02_1.wav               18,419,236 samples  ( 417.67s)
   ...
```

## Output

```
extracted/
  <song name>/
    000_V_TR01_1.wav      <- main track 1, virtual take 1
    008_V_TR02_1.wav      <- main track 2, virtual take 1
    009_V_TR02_2.wav      <- main track 2, virtual take 2
    ...
```

Files are mono, 16-bit signed PCM, 44.1 kHz — the AW16G's only audio format.
The numeric prefix is the track's slot index (tracks 1–16 occupy slots 0, 8,
16, 24, …, 120; virtual takes 2–8 of each track occupy the slots in between).
Empty / unused virtual takes are skipped.

## Loading the WAVs into a DAW

Logic Pro and GarageBand: drag the song folder onto the tracks area. The DAW
creates one track per WAV, all aligned to the project start — which is what
you want, since every AW16G region in a song shares a common timeline anchored
at sample 0.

## Multi-disk backups

CD-spanning backups produce a numbered sequence (`AW_00000.16G`,
`AW_00001.16G`, …). Just point the script at disk 0 and put the other disk
files alongside it — sibling files are auto-discovered by filename pattern.
At startup you'll see one line per disk:

```
Opened 2 disk file(s):
  disk 0: aw_00000.16g  size=736,843,776  max_frames=5621  prev=0  off=41  ...
  disk 1: aw_00001.16g  size=24,467,456   max_frames=186   prev=-1 off=-5579 ...
```

Each audio-frame read is routed to the disk that holds it based on each
disk's header fields (`previous_frames`, `offset_frames`, `max_frames`).
On disk 0 the audio data sits at the fixed `0x515800` offset; on disks ≥1
it fills the tail of the file (base = `file_size - max_frames*0x20000`,
which is `0x15800` for a full CD).

If a song still references frames not present in the disks you supplied,
you'll see `[TRUNCATED -- needs next disk]` next to that track and a hint
about which disk number is missing.

## Limitations

- No 24-bit support. The AW-16G is a 16-bit machine; the related AW4416 /
  AW2816 can do 24-bit but those models aren't handled here — use
  AWare-Audio for those.
- Loads the song-info table into memory (~128 KB) but otherwise streams.

## Credits

- **[AWare-Audio](https://github.com/jeffleary00/AWare-Audio)** by Jeff Leary
  — the source of all the format constants and the original walking
  algorithm. This project is essentially a Python reimplementation with
  three bugs fixed:
    1. The AW16G song-info walk; see the "AWare-Audio bugs" section above.
    2. The audio-frame base address for songs other than the first. The
       upstream tool computes `frame_loc = song_metadata_loc + frame *
       block_size`, which gives the wrong answer for the second and later
       songs in a multi-song single-disk backup (their `song_metadata_loc`
       isn't the start of the audio area). The correct base is the fixed
       `songblock_location` constant. For song 0 the two coincide, which
       is why the bug went unnoticed.
    3. (Same area as bug 1.) Bounds-checking on the computed song-block
       location.

## License

BSD 2-Clause. See [LICENSE](LICENSE). The format-constant block and the
song/track/region/map walking strategy are derived from AWare-Audio, which is
also BSD-2-Clause.
