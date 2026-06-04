**MultiFlash**

Linux desktop tool providing multiple utilities for provisioning batches of SD cards sequentially or concurrently.

<img width="810" height="525" alt="MultiFlash_SS" src="https://github.com/user-attachments/assets/e961e56d-c82c-492f-a261-9055bfce64ce" />

**Features**

* Flash multiple cards at once, sequentially or concurrently.
* Compressed images flash directly (.img, .img.gz, .xz, .zip).
* Extract an image back off a card, with optional shrinking and gzip compression
* Verify a card against a known image file with detailed comparison highlighting true differences and harmless OS mount metadata such as FAT dirty flag, ext4 journal, etc.
* View and compare filesystem usage, partition size, and file size quickly.
* Safety considerations such as auto-unmounts before writing, size checks, user confirmation before destruction.
* Flashes are logged to enable analysis of write times.

<img width="566" height="259" alt="MultiFlash_SS_2" src="https://github.com/user-attachments/assets/91dc1a6b-7755-42fa-9362-b22b3b3da48d" />

**Requirements**

Python 3 (uses standard library exclusively with no pip packages)
Most modern Debian/Ubuntu based Linux systems with a graphical desktop
  * System tools: 'dd', 'lsblk', 'losetup', 'mount', 'umount', 'df', 'eject', 'gzip', 'parted', 'xz-utils'
  * A polkit agent for privilege prompt
  * python3-tk apt package not included on some systems

<img width="668" height="501" alt="MultiFlash_SS_3" src="https://github.com/user-attachments/assets/0a29f8e3-e32b-4fa1-8df0-19c9291c5b6e" />

**Running**

python3 flash.py

**Additional Information**

This project bundles PiShrink (https://github.com/Drewsif/PiShrink) by Drew Bonasera, used under the MIT License.
