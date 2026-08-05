#!/usr/bin/env python3
"""Convert arbitrary images and gifs into Discord-emoji-ready files.

A local dev tool, not part of the bot. It shells out to ffmpeg and gifsicle
rather than using Pillow so that nothing lands in requirements.txt -- the
server deploy installs that file, and the bot has no business carrying an
image toolchain. Pillow is used if present, but only for animated webp,
which ffmpeg cannot demux.

    python3 tools/emojify.py emoji_src/ -o emoji_out/

Discord's constraints, which are the whole reason this exists:
  * 256 KB hard cap per emoji, whatever the format. Reaction gifs pulled off
    the internet are routinely 20x that.
  * 128x128 is the size Discord renders from. Bigger is wasted bytes.
  * Names are 2-32 chars of [a-z0-9_], so filenames need sanitising.

Output filenames are the sanitised emoji name, so whatever uploads these
later can take the name straight from the stem.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_BYTES = 256 * 1024
MAX_DIM = 128
NAME_MAX = 32
NAME_MIN = 2

# Formats Discord accepts for upload. Anything else (webp, mp4, apng...) is
# transcoded into one of these.
STATIC_EXT = ".png"
ANIMATED_EXT = ".gif"

SOURCE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".apng",
    ".bmp", ".tiff", ".tif", ".mp4", ".webm", ".mov", ".avi", ".mkv",
}

# Tried in order until the result fits under the byte cap. Each rung trades
# away something you notice less than the one before it: framerate first,
# then colour depth, then lossy compression, and only then dimensions.
ANIMATED_LADDER = [
    # (max_dim, fps, colors, lossy)
    (128, 20, 256, 0),
    (128, 15, 200, 30),
    (128, 12, 160, 60),
    (128, 10, 128, 90),
    (112, 10, 96, 120),
    (96, 10, 64, 150),
    (96, 8, 64, 200),
    (64, 8, 48, 200),
]

# Static images at 128px are almost always tiny, so this rarely gets past the
# first rung. It exists for pathological inputs.
STATIC_LADDER = [128, 112, 96, 80, 64]


class ConversionError(RuntimeError):
    pass


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise ConversionError(tail[-1] if tail else f"{cmd[0]} failed")
    return proc.stdout


def emoji_name(stem: str) -> str:
    """Filename stem -> a name Discord will accept, or "" if there isn't one.

    A stem of "%" sanitises down to nothing. Padding that into ":__:" would
    "succeed" while producing an emoji nobody can type, so it returns empty
    and the caller reports it as a failure worth renaming the file over.
    """
    name = re.sub(r"[^a-z0-9_]+", "_", stem.lower()).strip("_")
    name = re.sub(r"_{2,}", "_", name)[:NAME_MAX]
    if not name:
        return ""
    if len(name) < NAME_MIN:
        # Discord rejects single-character names, but "x" still tells you what
        # the emoji is -- pad it rather than throwing the file out.
        name = (name + "_")[:NAME_MIN]
    return name


def load_names(path: Path) -> dict[str, str]:
    """Read a `source filename = emoji_name` mapping.

    Downloads are called tenor.gif and ezgif-4-a3f9c0.gif, which derive into
    useless emoji names, so the mapping is how a human (or Claude, having
    looked at the previews) assigns real ones without renaming files.
    """
    mapping: dict[str, str] = {}
    if not path.exists():
        return mapping
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        source, sep, wanted = line.partition("=")
        if not sep:
            print(f"  warn  {path.name}:{lineno}: no '=', ignoring", file=sys.stderr)
            continue
        mapping[source.strip()] = wanted.strip()
    return mapping


def write_names_stub(sources: list[Path], path: Path) -> None:
    lines = [
        "# emojify name mapping: <source filename> = <emoji name>",
        "# Names are 2-32 characters of a-z, 0-9 and _. Anything else is",
        "# rewritten. Delete a line to fall back to the derived name.",
        "",
    ]
    for src in sources:
        lines.append(f"{src.name} = {emoji_name(src.stem) or 'rename_me'}")
    path.write_text("\n".join(lines) + "\n")


def ffmpeg_source(src: Path, animated: bool, tmp: Path) -> list[str]:
    """ffmpeg input arguments for a source file.

    Animated webp is the one format ffmpeg cannot open, so it gets exploded
    to a png sequence first and fed back in as one. Everything else is just
    the file. Both the converter and the previewer go through here so the
    workaround only exists once.
    """
    if not (animated and src.suffix.lower() == ".webp"):
        return ["-i", str(src)]
    frames_dir = tmp / "frames"
    frames_dir.mkdir(exist_ok=True)
    pattern, src_fps = explode_webp(src, frames_dir)
    return ["-framerate", f"{src_fps:.3f}", "-i", pattern]


def make_preview(src: Path, dst: Path, frames: int) -> None:
    """Render a look-at-me image: one frame if static, a 3-frame strip if not.

    A strip rather than a single frame because the first frame of a reaction
    gif is very often a fade-in or a blank plate, which tells you nothing
    about what the gif actually is.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        source = ffmpeg_source(src, frames > 1, Path(tmpdir))
        if frames <= 1:
            run([
                "ffmpeg", "-y", "-v", "error", *source, "-frames:v", "1",
                "-vf", "scale=192:192:force_original_aspect_ratio=decrease",
                "-f", "image2", "-c:v", "png", str(dst),
            ])
            return
        mid, last = frames // 2, frames - 1
        run([
            "ffmpeg", "-y", "-v", "error", *source,
            "-vf", (f"select='eq(n\\,0)+eq(n\\,{mid})+eq(n\\,{last})',"
                    "scale=192:192:force_original_aspect_ratio=decrease,"
                    "pad=192:192:-1:-1:color=gray,tile=3x1"),
            "-fps_mode", "passthrough", "-frames:v", "1",
            "-f", "image2", "-c:v", "png", str(dst),
        ])


def human(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f}K"
    return f"{size / (1024 * 1024):.1f}M"


def pillow():
    """Pillow, or None. Optional: only animated webp needs it."""
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        return None
    import PIL.Image
    return PIL.Image


def probe_webp(path: Path) -> tuple[int, int, int] | None:
    """Probe a webp with Pillow, or None if Pillow is unavailable."""
    image = pillow()
    if image is None:
        return None
    with image.open(path) as im:
        return im.width, im.height, getattr(im, "n_frames", 1)


def explode_webp(src: Path, out_dir: Path) -> tuple[str, float]:
    """Dump an animated webp to a png sequence. Returns (pattern, fps).

    ffmpeg 6 ships a webp decoder but no animated-webp demuxer, so it reads
    exactly one frame and then writes nothing. Saved gifs off Tenor and
    Discord are routinely animated webp, so this is not an edge case. Frames
    go out as png rather than straight to gif to keep the full colour range
    for ffmpeg's palettegen further down.
    """
    image = pillow()
    if image is None:
        raise ConversionError(
            "animated webp needs Pillow (pip install Pillow) -- "
            "ffmpeg cannot demux it")
    durations: list[int] = []
    with image.open(src) as im:
        count = getattr(im, "n_frames", 1)
        for index in range(count):
            im.seek(index)
            durations.append(im.info.get("duration", 0) or 0)
            im.convert("RGBA").save(out_dir / f"f_{index:05d}.png")
    # Pillow reports per-frame duration in ms; 0 means the file did not say,
    # in which case browsers settle on roughly 10fps.
    average = sum(durations) / len(durations) if durations else 0
    fps = 1000.0 / average if average > 0 else 10.0
    return str(out_dir / "f_%05d.png"), max(min(fps, 50.0), 1.0)


def probe(path: Path) -> tuple[int, int, int]:
    """Return (width, height, frame_count).

    Frames are counted by decoding rather than trusting the container's
    metadata, because gif and webp both routinely report nb_frames=N/A.
    """
    if path.suffix.lower() == ".webp":
        probed = probe_webp(path)
        if probed is not None:
            return probed
    out = run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=width,height,nb_read_frames",
        "-of", "default=noprint_wrappers=1:nokey=0", str(path),
    ])
    fields: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    try:
        width = int(fields["width"])
        height = int(fields["height"])
    except (KeyError, ValueError) as exc:
        raise ConversionError("could not read image dimensions") from exc
    try:
        frames = int(fields.get("nb_read_frames", "1"))
    except ValueError:
        frames = 1
    return width, height, max(frames, 1)


def fit(width: int, height: int, box: int) -> tuple[int, int]:
    """Scale to fit inside a box, preserving aspect ratio, never upscaling."""
    if width <= box and height <= box:
        return width, height
    scale = box / max(width, height)
    return max(int(width * scale), 1), max(int(height * scale), 1)


def encode_static(source: list[str], dst: Path, width: int, height: int) -> None:
    run([
        "ffmpeg", "-y", "-v", "error", *source,
        "-frames:v", "1",
        "-vf", f"scale={width}:{height}:flags=lanczos",
        "-f", "image2", "-c:v", "png", str(dst),
    ])


def encode_animated(
    source: list[str], dst: Path, width: int, height: int, fps: int, colors: int
) -> None:
    """Transcode to gif with a generated palette.

    palettegen/paletteuse in one pass via split: a naive gif encode uses a
    fixed web palette and looks awful on anything with gradients.
    """
    chain = (
        f"fps={fps},scale={width}:{height}:flags=lanczos,split[s0][s1];"
        f"[s0]palettegen=max_colors={max(4, min(colors, 256))}:stats_mode=diff[p];"
        f"[s1][p]paletteuse=dither=bayer:bayer_scale=3"
    )
    run([
        "ffmpeg", "-y", "-v", "error", *source,
        "-filter_complex", chain, "-loop", "0", "-f", "gif", str(dst),
    ])


def squeeze_gif(path: Path, colors: int, lossy: int) -> None:
    cmd = ["gifsicle", "-O3", "--colors", str(max(2, min(colors, 256)))]
    if lossy:
        cmd += [f"--lossy={lossy}"]
    cmd += ["-b", str(path)]
    run(cmd)


def convert(src: Path, out_dir: Path, budget: int, box: int, name: str) -> dict:
    width, height, frames = probe(src)
    animated = frames > 1
    dst = out_dir / f"{name}{ANIMATED_EXT if animated else STATIC_EXT}"

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / f"work{dst.suffix}"

        source = ffmpeg_source(src, animated, Path(tmp))

        if animated:
            ladder = [r for r in ANIMATED_LADDER if r[0] <= box] or [ANIMATED_LADDER[-1]]
            for rung, (dim, fps, colors, lossy) in enumerate(ladder):
                target_w, target_h = fit(width, height, dim)
                encode_animated(source, work, target_w, target_h, fps, colors)
                squeeze_gif(work, colors, lossy)
                size = work.stat().st_size
                if size <= budget:
                    break
            else:
                raise ConversionError(
                    f"cannot fit under {human(budget)} "
                    f"(best {human(size)} at {target_w}x{target_h})"
                )
        else:
            ladder = [d for d in STATIC_LADDER if d <= box] or [STATIC_LADDER[-1]]
            for rung, dim in enumerate(ladder):
                target_w, target_h = fit(width, height, dim)
                try:
                    encode_static(source, work, target_w, target_h)
                except ConversionError:
                    # Without Pillow we cannot tell an animated webp from a
                    # still one, so it lands here and ffmpeg fails with a
                    # message about empty streams. Say the useful thing.
                    if src.suffix.lower() == ".webp" and pillow() is None:
                        raise ConversionError(
                            "webp failed to decode; if it is animated, ffmpeg "
                            "cannot demux it -- pip install Pillow") from None
                    raise
                size = work.stat().st_size
                if size <= budget:
                    break
            else:
                raise ConversionError(
                    f"cannot fit under {human(budget)} "
                    f"(best {human(size)} at {target_w}x{target_h})"
                )

        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work, dst)

    return {
        "name": name,
        "path": dst,
        "animated": animated,
        "width": target_w,
        "height": target_h,
        "frames": frames,
        "bytes": size,
        "rung": rung,
        "source_bytes": src.stat().st_size,
    }


def gather(inputs: list[Path]) -> list[Path]:
    found: list[Path] = []
    for item in inputs:
        if item.is_dir():
            found += [p for p in sorted(item.rglob("*"))
                      if p.is_file() and p.suffix.lower() in SOURCE_EXTS]
        elif item.is_file():
            found.append(item)
        else:
            print(f"  skip  {item}  (not found)", file=sys.stderr)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert images and gifs into Discord-emoji-ready files.")
    parser.add_argument("inputs", nargs="*", default=["emoji_src"], type=Path,
                        help="files or directories (default: emoji_src/)")
    parser.add_argument("-o", "--out", type=Path, default=Path("emoji_out"),
                        help="output directory (default: emoji_out/)")
    parser.add_argument("--size", type=int, default=MAX_DIM,
                        help=f"max dimension in px (default: {MAX_DIM})")
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES,
                        help=f"byte budget per emoji (default: {MAX_BYTES})")
    parser.add_argument("-f", "--force", action="store_true",
                        help="reconvert even if the output already exists")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="list what would be converted and exit")
    parser.add_argument("--names", type=Path, default=Path("names.txt"),
                        help="'<file> = <emoji name>' mapping (default: names.txt)")
    parser.add_argument("--write-names", action="store_true",
                        help="write a stub mapping for every source and exit")
    parser.add_argument("--preview", type=Path, metavar="DIR",
                        help="render look-at-me images for naming, then exit")
    args = parser.parse_args()

    for binary in ("ffmpeg", "ffprobe", "gifsicle"):
        if not shutil.which(binary):
            print(f"error: {binary} not found on PATH", file=sys.stderr)
            print("  sudo apt install ffmpeg gifsicle", file=sys.stderr)
            return 1

    sources = gather(args.inputs)
    if not sources:
        print("nothing to convert", file=sys.stderr)
        return 1

    if args.write_names:
        if args.names.exists():
            print(f"error: {args.names} exists; delete it or pass --names",
                  file=sys.stderr)
            return 1
        write_names_stub(sources, args.names)
        print(f"wrote {args.names} with {len(sources)} entries -- edit the names")
        return 0

    if args.preview:
        args.preview.mkdir(parents=True, exist_ok=True)
        for index, src in enumerate(sources, 1):
            try:
                _, _, frames = probe(src)
                make_preview(src, args.preview / f"{index:03d}.png", frames)
            except ConversionError as exc:
                print(f"  FAIL  {src.name}  {exc}")
                continue
            kind = f"{frames}f" if frames > 1 else "still"
            print(f"  {index:03d}.png  {src.name}  ({kind})")
        print(f"\n{len(sources)} preview(s) -> {args.preview}/")
        return 0

    names = load_names(args.names)
    if names:
        print(f"using names from {args.names}")
    unused = set(names) - {s.name for s in sources}
    for stale in sorted(unused):
        print(f"  warn  {args.names.name}: '{stale}' matches no source file")

    def resolve(src: Path) -> tuple[str, str | None]:
        wanted = names.get(src.name)
        return emoji_name(wanted) if wanted else emoji_name(src.stem), wanted

    if args.dry_run:
        for src in sources:
            name, wanted = resolve(src)
            via = "  (names.txt)" if wanted else ""
            print(f"  {src}  ->  {':' + name + ':' if name else '(no usable name)'}{via}")
        print(f"\n{len(sources)} file(s)")
        return 0

    seen: dict[str, Path] = {}
    ok = skipped = failed = 0

    for src in sources:
        name, wanted = resolve(src)
        if wanted and name != wanted:
            print(f"  warn  '{wanted}' is not a legal emoji name, using '{name}'")
        if not name:
            print(f"  FAIL  {src.name}  no usable name; add it to {args.names.name}")
            failed += 1
            continue
        if name in seen:
            # Two files collapsing to one emoji name would silently overwrite
            # each other, and the second one wins arbitrarily. Say so.
            print(f"  CLASH {src.name}  -> :{name}: already taken by {seen[name].name}")
            failed += 1
            continue
        seen[name] = src

        existing = list(args.out.glob(f"{name}.*"))
        if existing and not args.force:
            print(f"  have  :{name}:  ({existing[0].name}, --force to redo)")
            skipped += 1
            continue

        try:
            result = convert(src, args.out, args.max_bytes, args.size, name)
        except ConversionError as exc:
            print(f"  FAIL  {src.name}  {exc}")
            failed += 1
            continue

        kind = f"gif {result['frames']}f" if result["animated"] else "png"
        note = "" if result["rung"] == 0 else f"  [degraded x{result['rung']}]"
        print(f"  ok    :{result['name']}:  {kind}  "
              f"{result['width']}x{result['height']}  "
              f"{human(result['source_bytes'])} -> {human(result['bytes'])}{note}")
        ok += 1

    parts = [f"{ok} converted"]
    if skipped:
        parts.append(f"{skipped} already done")
    if failed:
        parts.append(f"{failed} failed")
    print(f"\n{', '.join(parts)}  ->  {args.out}/")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
