#!/usr/bin/env python3

import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from typing import Callable
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


def flash_device(image: str, device: str,
                 progress_cb: Callable | None = None) -> tuple[bool, str]:
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


def get_image_disk_usage(image: str) -> tuple[int, int] | None:
    """Return (used_bytes, total_bytes) by mounting the image's root partition."""
    loop_dev = None
    mountpoint = None
    try:
        loop_dev = subprocess.run(
            ['losetup', '--find', '--show', '--partscan', image],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        mountpoint = tempfile.mkdtemp()
        subprocess.run(
            ['mount', '-o', 'ro', f'{loop_dev}p2', mountpoint],
            capture_output=True, check=True,
        )

        out = subprocess.run(
            ['df', '--block-size=1', mountpoint],
            capture_output=True, text=True, check=True,
        ).stdout
        fields = out.strip().split('\n')[1].split()
        return int(fields[2]), int(fields[1])
    except Exception:
        return None
    finally:
        if mountpoint:
            subprocess.run(['umount', mountpoint], capture_output=True)
            os.rmdir(mountpoint)
        if loop_dev:
            subprocess.run(['losetup', '-d', loop_dev], capture_output=True)


def verify_device(image: str, device: str) -> tuple[bool, str]:
    size = os.path.getsize(image)
    result = subprocess.run(
        ['cmp', '-n', str(size), image, device],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True, ''
    return False, result.stdout or result.stderr


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


REQUIRED_TOOLS = ['dd', 'lsblk', 'cmp', 'eject', 'losetup', 'mount', 'gzip', 'df']


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
