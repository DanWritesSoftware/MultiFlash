#!/usr/bin/env python3

import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import imagemap
from gui import FlashApp

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE    = os.path.join(SCRIPT_DIR, 'config.json')
PISHRINK       = os.path.join(SCRIPT_DIR, 'pishrink.sh')
EXTRACTED_DIR  = os.path.join(SCRIPT_DIR, 'extracted_images')
FLASH_LOG      = os.path.join(SCRIPT_DIR, 'flash_log.csv')


def get_default_image() -> str | None:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f).get('default_image') or None
    except Exception:
        return None


def set_default_image(path: str) -> None:
    config = {}
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
    except Exception:
        pass
    config['default_image'] = path
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


def get_removable_devices() -> list[dict]:
    try:
        out = subprocess.run(
            ['lsblk', '-J', '-o', 'NAME,SIZE,MODEL,HOTPLUG,TYPE,VENDOR,TRAN'],
            capture_output=True, text=True, check=True,
        ).stdout
        devices = []
        for dev in json.loads(out).get('blockdevices', []):
            if dev.get('type') == 'disk' and dev.get('hotplug'):
                tran = (dev.get('tran') or '').lower()
                model = (dev.get('model') or dev.get('vendor') or '').strip()
                if not model:
                    model = 'SD/MMC' if tran == 'mmc' else 'Unknown'
                devices.append({
                    'path':  f"/dev/{dev['name']}",
                    'size':  dev.get('size', '?'),
                    'model': model,
                })
        return devices
    except Exception:
        return []


def unmount_device(device: str) -> tuple[bool, str]:
    """Unmount every mounted partition of `device` before writing or reading it.

    A desktop auto-mounter usually mounts a card the moment it is inserted, and
    writing to the whole-disk node while a partition is mounted can corrupt the
    write. Returns (True, '') when nothing is mounted or all unmounts succeed.
    """
    try:
        out = subprocess.run(
            ['lsblk', '-J', '-o', 'PATH,MOUNTPOINT', device],
            capture_output=True, text=True, check=True,
        ).stdout
    except Exception as exc:
        return False, str(exc)

    mounted: list[str] = []

    def walk(nodes):
        for n in nodes:
            if n.get('mountpoint'):
                mounted.append(n['path'])
            walk(n.get('children', []))

    try:
        walk(json.loads(out).get('blockdevices', []))
    except Exception as exc:
        return False, str(exc)

    for path in mounted:
        result = subprocess.run(['umount', path], capture_output=True, text=True)
        if result.returncode != 0:
            return False, f'{path}: {result.stderr.strip() or "umount failed"}'
    return True, ''


def flash_device(image: str, device: str,
                 progress_cb: Callable | None = None) -> tuple[bool, str]:
    ok, err = unmount_device(device)
    if not ok:
        return False, f'Could not unmount {device}: {err}'
    total = os.path.getsize(image)
    pat = re.compile(r'(\d+) bytes.*?([\d.]+ \S+/s)')
    stderr_buf = []

    proc = subprocess.Popen(
        ['dd', f'if={image}', f'of={device}', 'bs=4M', 'status=progress', 'conv=fsync'],
        stderr=subprocess.PIPE, text=True,
    )

    buf = ''
    for char in iter(lambda: proc.stderr.read(1), ''):
        buf += char
        if char in ('\r', '\n'):
            m = pat.search(buf)
            if m and progress_cb and total > 0:
                pct = min(int(m.group(1)) / total * 100, 99)
                progress_cb(pct, m.group(2))
            stderr_buf.append(buf)
            buf = ''

    proc.wait()
    if proc.returncode == 0:
        return True, ''
    return False, ''.join(stderr_buf)


def get_image_disk_usage(image: str) -> dict | None:
    """Describe the image's root filesystem and overall layout, or None on failure.

    Returns a dict:
      file_size  – size of the .img file on disk
      part_size  – size of the root (p2) partition
      fs_used    – bytes used inside the root filesystem (from df)
      fs_total   – total size of the root filesystem (from df)

    The gap between part_size and file_size is trailing unallocated space: the
    empty tail PiShrink trims away, which df cannot see.
    """
    loop_dev = None
    mountpoint = None
    try:
        loop_dev = subprocess.run(
            ['losetup', '--find', '--show', '--partscan', image],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        part = f'{loop_dev}p2'

        try:
            part_size = int(subprocess.run(
                ['lsblk', '-b', '-d', '-n', '-o', 'SIZE', part],
                capture_output=True, text=True, check=True,
            ).stdout.strip())
        except Exception:
            part_size = None

        mountpoint = tempfile.mkdtemp()
        subprocess.run(
            ['mount', '-o', 'ro', part, mountpoint],
            capture_output=True, check=True,
        )

        out = subprocess.run(
            ['df', '--block-size=1', mountpoint],
            capture_output=True, text=True, check=True,
        ).stdout
        fields = out.strip().split('\n')[1].split()
        fs_used, fs_total = int(fields[2]), int(fields[1])

        return {
            'file_size': os.path.getsize(image),
            'part_size': part_size if part_size is not None else fs_total,
            'fs_used':   fs_used,
            'fs_total':  fs_total,
        }
    except Exception:
        return None
    finally:
        if mountpoint:
            subprocess.run(['umount', mountpoint], capture_output=True)
            os.rmdir(mountpoint)
        if loop_dev:
            subprocess.run(['losetup', '-d', loop_dev], capture_output=True)


def _fmt_speed(bytes_per_sec: float) -> str:
    for unit in ('B/s', 'KB/s', 'MB/s', 'GB/s'):
        if bytes_per_sec < 1024:
            return f'{bytes_per_sec:.1f} {unit}'
        bytes_per_sec /= 1024
    return f'{bytes_per_sec:.1f} TB/s'


def _diff_ranges(a: bytes, b: bytes, base: int, window: int = 4096):
    """Yield (start, end) byte ranges where a and b differ, runs coalesced.

    Compares window-sized slices at C speed and only descends byte-by-byte into
    slices that actually differ, so a device that matches except for small
    metadata regions stays cheap to scan.
    """
    n = len(a)
    i = 0
    cur = None
    while i < n:
        aw = a[i:i + window]
        bw = b[i:i + window]
        if aw == bw:
            if cur:
                yield cur
                cur = None
            i += window
            continue
        for j in range(len(aw)):
            if j >= len(bw) or aw[j] != bw[j]:
                off = base + i + j
                if cur and cur[1] == off:
                    cur = (cur[0], off + 1)
                else:
                    if cur:
                        yield cur
                    cur = (off, off + 1)
            elif cur:
                yield cur
                cur = None
        i += window
    if cur:
        yield cur


@dataclass
class VerifyResult:
    """Outcome of verify_device.

    status:  match | benign | mismatch | error
    detail:  one-line human summary (also shown in the device row)
    regions: classified imagemap.DiffRegion list (benign + real, with labels)
    """
    status: str
    detail: str
    regions: list = field(default_factory=list)
    diff_bytes: int = 0
    truncated: bool = False
    total: int = 0
    scanned: int = 0    # bytes actually compared; < total if the scan stopped early


# Stop refining once this many differing bytes are seen: well above the largest
# benign region (the ext4 journal), so a genuinely-wrong card can't hang the scan.
_DIFF_SCAN_CAP = 96 * 1024 * 1024
_MAX_RANGES    = 20000


def verify_device(image: str, device: str,
                  progress_cb: Callable | None = None,
                  is_cancelled: Callable | None = None) -> VerifyResult:
    """Compare the first len(image) bytes of `device` against `image` in full.

    Scans the whole device, collecting every differing byte range, then labels
    each via imagemap.ImageMap. Differences that fall in mount-volatile regions
    (FAT dirty flag / FSInfo, ext4 superblock mount fields, ext4 journal) are
    benign; anything else is real. The live count is reported through progress_cb.

    `is_cancelled()` is polled once per chunk; if it returns True the scan stops
    and returns a 'cancelled' result with whatever was found so far (the unread
    tail is reported as not-scanned, like any other early stop).
    """
    total = os.path.getsize(image)
    bs    = 4 * 1024 * 1024
    read  = 0
    start = time.monotonic()
    raw: list[tuple[int, int]] = []
    diff_bytes = 0
    truncated  = False
    short      = None
    try:
        with open(image, 'rb') as img, open(device, 'rb') as dev:
            while read < total:
                if is_cancelled is not None and is_cancelled():
                    regions = imagemap.ImageMap(image).classify(raw)
                    return VerifyResult(
                        'cancelled',
                        f'Cancelled after {imagemap.human_size(read)} '
                        f'of {imagemap.human_size(total)}',
                        regions, diff_bytes, True, total, read)
                chunk = img.read(min(bs, total - read))
                if not chunk:
                    break
                other = dev.read(len(chunk))
                if len(other) < len(chunk):
                    short = (read + len(other), total)
                    diff_bytes += total - (read + len(other))
                    break
                if other != chunk:
                    for s, e in _diff_ranges(chunk, other, read):
                        diff_bytes += e - s
                        if raw and raw[-1][1] == s:          # contiguous across windows
                            raw[-1] = (raw[-1][0], e)
                        elif len(raw) < _MAX_RANGES:
                            raw.append((s, e))
                        else:
                            truncated = True
                read += len(chunk)
                # Stop once the card is clearly a mismatch: too many differing
                # regions to list, or more raw difference than worth finishing.
                if len(raw) >= _MAX_RANGES or diff_bytes > _DIFF_SCAN_CAP:
                    truncated = True
                    break
                if progress_cb and total > 0:
                    elapsed = time.monotonic() - start
                    n = len(raw) + (1 if short else 0)
                    status = (f'{n} mismatch' + ('' if n == 1 else 'es')) if n \
                        else (_fmt_speed(read / elapsed) if elapsed else '')
                    progress_cb(min(read / total * 100, 99), status)
    except Exception as exc:
        return VerifyResult('error', str(exc), total=total, scanned=read)

    # `read` now marks how far we actually compared; a short device still counts
    # as fully scanned because its missing tail is recorded as a real difference.
    scanned = total if (short or read >= total) else read

    regions = imagemap.ImageMap(image).classify(raw)
    if short:
        regions.append(imagemap.DiffRegion(short[0], short[1],
                                           'device shorter than image', False))
    if not regions:
        return VerifyResult('match', '', total=total, scanned=scanned)

    real = [r for r in regions if not r.benign]
    if real:
        labels = ', '.join(sorted({r.label for r in real}))
        size   = imagemap.human_size(sum(r.size for r in real))
        suffix = ' (scan stopped early)' if scanned < total else \
                 (' (+more)' if truncated else '')
        return VerifyResult('mismatch', f'{size} differ in {labels}{suffix}',
                            regions, diff_bytes, truncated, total, scanned)
    labels = ', '.join(sorted({r.label for r in regions}))
    return VerifyResult('benign', f'Only OS mount metadata differs: {labels}',
                        regions, diff_bytes, truncated, total, scanned)


def shrink_image(image: str) -> tuple[bool, str]:
    result = subprocess.run(
        ['bash', PISHRINK, image],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True, ''
    return False, result.stderr


def get_device_size(device: str) -> int:
    out = subprocess.run(
        ['lsblk', '-b', '-d', '-n', '-o', 'SIZE', device],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return int(out)


def extract_device(device: str, filename: str, shrink: bool, compress: bool,
                   progress_cb: Callable | None = None) -> tuple[bool, str]:
    os.makedirs(EXTRACTED_DIR, exist_ok=True)
    img_path = os.path.join(EXTRACTED_DIR, f'{filename}.img')

    try:
        ok, err = unmount_device(device)
        if not ok:
            return False, f'Could not unmount {device}: {err}'
        total = get_device_size(device)
        pat   = re.compile(r'(\d+) bytes.*?([\d.]+ \S+/s)')

        proc = subprocess.Popen(
            ['dd', f'if={device}', f'of={img_path}', 'bs=4M', 'status=progress'],
            stderr=subprocess.PIPE, text=True,
        )
        buf = ''
        for char in iter(lambda: proc.stderr.read(1), ''):
            buf += char
            if char in ('\r', '\n'):
                m = pat.search(buf)
                if m and progress_cb and total > 0:
                    pct = min(int(m.group(1)) / total * 100, 99)
                    progress_cb(pct, m.group(2))
                buf = ''
        proc.wait()
        if proc.returncode != 0:
            return False, f'dd failed with exit code {proc.returncode}'

        if shrink:
            if progress_cb:
                progress_cb(100, 'Shrinking…')
            result = subprocess.run(
                ['bash', PISHRINK, img_path],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                return False, f'PiShrink failed: {result.stderr}'

        if compress:
            if progress_cb:
                progress_cb(100, 'Compressing…')
            result = subprocess.run(['gzip', img_path], capture_output=True, text=True)
            if result.returncode != 0:
                return False, f'gzip failed: {result.stderr}'
            img_path += '.gz'

        return True, img_path
    except Exception as exc:
        return False, str(exc)


def eject_device(device: str) -> None:
    subprocess.run(['eject', device], capture_output=True)


def log_flash(image: str, size: str, duration: float, success: bool) -> None:
    write_header = not os.path.exists(FLASH_LOG)
    with open(FLASH_LOG, 'a', newline='') as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(['timestamp', 'image', 'size', 'duration_seconds', 'status'])
        writer.writerow([
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            os.path.basename(image),
            size,
            f'{duration:.1f}',
            'Success' if success else 'Failed',
        ])


REQUIRED_TOOLS = ['dd', 'lsblk', 'eject', 'losetup', 'mount', 'umount', 'gzip', 'df']


def check_dependencies() -> None:
    missing = [t for t in REQUIRED_TOOLS
               if subprocess.run(['which', t], capture_output=True).returncode != 0]
    if missing:
        print('Error: missing required tools: ' + ', '.join(missing), file=sys.stderr)
        print('Install with: sudo apt install ' + ' '.join(missing), file=sys.stderr)
        sys.exit(1)


def main() -> None:
    if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
        print('Error: no display found. Run from a desktop terminal or prefix with DISPLAY=:0',
              file=sys.stderr)
        sys.exit(1)
    check_dependencies()
    if os.geteuid() != 0:
        script = os.path.abspath(sys.argv[0])
        display = os.environ.get('DISPLAY', '')
        xauth = os.environ.get('XAUTHORITY', '')
        os.execvp('pkexec', [
            'pkexec', 'env',
            f'DISPLAY={display}',
            f'XAUTHORITY={xauth}',
            sys.executable, script, *sys.argv[1:],
        ])
    FlashApp(
        default_image=get_default_image(),
        set_default_image=set_default_image,
        project_dir=SCRIPT_DIR,
        extracted_dir=EXTRACTED_DIR,
        get_devices=get_removable_devices,
        get_device_size=get_device_size,
        flash_device=flash_device,
        extract_device=extract_device,
        get_image_disk_usage=get_image_disk_usage,
        eject_device=eject_device,
        verify_device=verify_device,
        shrink_image=shrink_image,
        log_flash=log_flash,
    ).mainloop()


if __name__ == '__main__':
    main()
