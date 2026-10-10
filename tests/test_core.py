"""Tests for NanoSnip's logic: timecodes, cuts, snapping and the ffmpeg commands it builds."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import nanosnip
from nanosnip import (
    ExportOptions,
    Filmstrip,
    MediaInfo,
    build_command,
    encoder_args,
    fmt_time,
    kept_segments,
    snap_time,
)


@pytest.mark.parametrize("seconds, expected", [
    (0, "00:00.000"),
    (5.25, "00:05.250"),
    (59.9996, "01:00.000"),     # rounding must carry into the next minute
    (3661.5, "1:01:01.500"),
    (-3, "00:00.000"),
])
def test_fmt_time(seconds, expected):
    assert fmt_time(seconds) == expected


def test_fmt_time_without_ms():
    assert fmt_time(75.4, with_ms=False) == "01:15"


@pytest.fixture
def clip(monkeypatch):
    monkeypatch.setattr(nanosnip.shutil, "which", lambda name: f"/usr/bin/{name}")
    return MediaInfo(path=Path("/in/clip.mp4"), is_video=True, width=1920, height=1080,
                     duration=10.0, has_audio=True)


def test_trim_only_copies_streams(clip):
    cmd = build_command(clip, ExportOptions(start=2.0, end=8.0), Path("/out/a.mp4"))
    assert cmd[:3] == ["/usr/bin/ffmpeg", "-hide_banner", "-y"]
    assert ["-ss", "2.000", "-to", "8.000"] == cmd[3:7]
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
    assert "-vf" not in cmd
    assert cmd[-1] == "/out/a.mp4"


def test_full_length_skips_seek(clip):
    cmd = build_command(clip, ExportOptions(start=0.0, end=10.0), Path("/out/a.mp4"))
    assert "-ss" not in cmd and "-to" not in cmd


def test_filters_run_mirror_before_crop(clip):
    opts = ExportOptions(end=10.0, hflip=True, vflip=True, crop=(640, 360, 10, 20))
    cmd = build_command(clip, opts, Path("/out/a.mp4"))
    assert cmd[cmd.index("-vf") + 1] == "hflip,vflip,crop=640:360:10:20"
    assert ["-c:a", "aac", "-b:a", "192k"] == cmd[cmd.index("-c:a"):cmd.index("-c:a") + 4]
    assert "-pix_fmt" in cmd and "+faststart" in cmd


def test_webm_uses_opus_and_no_pix_fmt(clip):
    opts = ExportOptions(end=10.0, hflip=True, encoder="libvpx-vp9")
    cmd = build_command(clip, opts, Path("/out/a.webm"))
    assert cmd[cmd.index("-c:a") + 1] == "libopus"
    assert "-pix_fmt" not in cmd


def test_dropping_audio(clip):
    cmd = build_command(clip, ExportOptions(end=10.0, keep_audio=False), Path("/out/a.mp4"))
    assert "-an" in cmd


def test_missing_ffmpeg_raises(clip, monkeypatch):
    monkeypatch.setattr(nanosnip.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        build_command(clip, ExportOptions(end=10.0), Path("/out/a.mp4"))


@pytest.mark.parametrize("encoder, flag, value", [
    ("libx264", "-crf", "21"),
    ("libx265", "-crf", "25"),
    ("libvpx-vp9", "-crf", "31"),
    ("h264_nvenc", "-cq", "23"),
])
def test_encoder_quality(encoder, flag, value):
    args = encoder_args(encoder, "balanced")
    assert args[args.index(flag) + 1] == value


def test_unknown_quality_falls_back_to_balanced():
    assert encoder_args("libx264", "nonsense") == encoder_args("libx264", "balanced")


# -- cuts -------------------------------------------------------------------
def test_kept_segments_drops_cut_pieces():
    assert kept_segments([0, 2, 5, 9], [False, True, False]) == [(0, 2), (5, 9)]


def test_kept_segments_joins_neighbouring_kept_pieces():
    assert kept_segments([1, 3, 4, 8], [False, False, True]) == [(1, 4)]


def test_kept_segments_everything_kept():
    assert kept_segments([0, 10], [False]) == [(0, 10)]


def test_snap_time_picks_the_closest_target_in_range():
    assert snap_time(4.97, [0.0, 5.0, 4.9], 0.1) == 5.0
    assert snap_time(3.0, [0.0, 5.0], 0.1) is None


def test_cut_command_trims_and_joins_each_piece(clip):
    opts = ExportOptions(start=1.0, end=8.0, segments=[(1.0, 3.0), (5.0, 8.0)])
    cmd = build_command(clip, opts, Path("/out/a.mp4"))
    assert "-ss" not in cmd
    assert cmd[cmd.index("-to") + 1] == "8.000" and cmd.index("-to") < cmd.index("-i")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:v]trim=start=1.000:end=3.000,setpts=PTS-STARTPTS[v0]" in graph
    assert "[0:a]atrim=start=5.000:end=8.000,asetpts=PTS-STARTPTS[a1]" in graph
    assert graph.endswith("[v0][a0][v1][a1]concat=n=2:v=1:a=1[joined][aout]")
    assert cmd[cmd.index("-map") + 1] == "[joined]"
    assert "[aout]" in cmd and "copy" not in cmd
    assert cmd[cmd.index("-c:a") + 1] == "aac"


def test_cut_command_mirrors_and_crops_after_joining(clip):
    opts = ExportOptions(end=10.0, hflip=True, crop=(640, 360, 0, 0),
                         segments=[(0.0, 2.0), (4.0, 10.0)])
    cmd = build_command(clip, opts, Path("/out/a.mp4"))
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.endswith("[joined]hflip,crop=640:360:0:0[vout]")
    assert cmd[cmd.index("-map") + 1] == "[vout]"
    assert "-to" not in cmd            # the last piece runs to the end of the clip
    assert "-vf" not in cmd


def test_cut_command_without_audio(clip):
    opts = ExportOptions(end=8.0, keep_audio=False, segments=[(0.0, 2.0), (5.0, 8.0)])
    cmd = build_command(clip, opts, Path("/out/a.mp4"))
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "atrim" not in graph and "concat=n=2:v=1:a=0[joined]" in graph
    assert "-an" in cmd and "[aout]" not in cmd


def test_single_kept_piece_stays_lossless(clip):
    opts = ExportOptions(start=2.0, end=8.0, segments=[(2.0, 8.0)])
    cmd = build_command(clip, opts, Path("/out/a.mp4"))
    assert "-filter_complex" not in cmd
    assert cmd[cmd.index("-c") + 1] == "copy"


# -- the filmstrip's cut model -----------------------------------------------
@pytest.fixture
def strip():
    nanosnip.QApplication.instance() or nanosnip.QApplication([])
    s = Filmstrip()
    s.duration = 10.0
    s.start, s.end = 0.0, 10.0
    s.setEnabled(True)
    return s


def test_split_and_cut_a_piece(strip):
    assert strip.split_at(3.0) and strip.split_at(6.0)
    assert strip.pieces() == [(0.0, 3.0), (3.0, 6.0), (6.0, 10.0)]
    assert strip.toggle_piece(1)
    assert strip.kept() == [(0.0, 3.0), (6.0, 10.0)]
    assert strip.kept_duration() == pytest.approx(7.0)


def test_cannot_split_right_next_to_an_edge(strip):
    assert not strip.split_at(0.05)
    assert strip.split_at(5.0)
    assert not strip.split_at(5.05)


def test_last_kept_piece_cannot_be_cut(strip):
    strip.split_at(5.0)
    assert strip.toggle_piece(0)
    assert not strip.toggle_piece(1)
    assert strip.kept() == [(5.0, 10.0)]


def test_cut_piece_stays_cut_when_split_again(strip):
    strip.split_at(4.0)
    strip.toggle_piece(1)                      # cut 4–10
    strip.split_at(7.0)                        # both halves of the cut piece stay cut
    assert strip.removed == [False, True, True]


def test_removing_a_split_keeps_the_piece_if_either_half_was_kept(strip):
    strip.split_at(5.0)
    strip.toggle_piece(1)
    strip.remove_split(0)
    assert strip.splits == [] and strip.removed == [False]


def test_trimming_past_a_split_drops_it(strip):
    strip.split_at(2.0)
    strip.split_at(6.0)
    strip.toggle_piece(1)                      # cut 2–6
    strip.set_range(3.0, 10.0)                 # start moves inside the cut piece
    assert strip.splits == [6.0]
    assert strip.removed == [True, False]
    assert strip.kept() == [(6.0, 10.0)]


def test_playback_skips_cut_pieces(strip):
    strip.split_at(2.0)
    strip.split_at(5.0)
    strip.toggle_piece(1)                      # cut 2–5
    assert strip.skip_target(3.0) == 5.0
    assert strip.skip_target(1.0) is None
    assert strip.skip_target(6.0) is None


def test_mp4_trim_keeps_the_exact_start(clip):
    # make_zero would pull an MP4 back to the keyframe before the trim point.
    cmd = build_command(clip, ExportOptions(start=4.0, end=10.0), Path("/out/a.mp4"))
    assert "-avoid_negative_ts" not in cmd


def test_mkv_trim_shifts_timestamps(clip):
    cmd = build_command(clip, ExportOptions(start=4.0, end=10.0), Path("/out/a.mkv"))
    assert cmd[cmd.index("-avoid_negative_ts") + 1] == "make_zero"


def test_drags_snap_to_the_playhead_splits_and_seconds(strip):
    strip.resize(1000 + strip.PAD * 2, strip.height())   # 100 px per second
    strip._anchor = 5.0                                  # the playhead when the drag began
    assert strip._snapped(4.96) == 5.0                   # 4 px away: snaps
    assert strip._snapped(4.80) == 4.80                  # 20 px away: left alone
    strip.split_at(7.3)
    assert strip._snapped(7.25) == 7.3                   # onto a split
    assert strip._snapped(7.25, ("split:0",)) == 7.25    # a split never snaps to itself
    assert strip._snapped(2.02) == 2.0                   # whole seconds, when zoomed in this far
