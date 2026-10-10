# TraceForge

> **Credit and thanks to [mfg92](https://github.com/mfg92):** TraceForge builds on the [hantek1008py](https://github.com/mfg92/hantek1008py) driver and its reverse engineering of the Hantek 1008 USB protocol. That work made this Linux app possible. The upstream driver is licensed under [Apache-2.0](https://github.com/mfg92/hantek1008py/blob/master/LICENSE).

**for Linux · Hantek 1008 series**

An eight-channel oscilloscope app with live viewing, recordings, reference overlays, and CSV/MP4 export. Tested hardware: Hantek 1008B.

## Run

Download the AppImage or source ZIP from [Releases](https://github.com/naruhaxor/TraceForge/releases).

Make the AppImage executable, then open it:

```bash
chmod +x TraceForge-0.7.5-x86_64.AppImage
./TraceForge-0.7.5-x86_64.AppImage
```

Python and application libraries are included. First-run USB setup asks for sudo in a terminal; desktop and menu shortcuts are optional.

If AppImage mounting fails:

```bash
APPIMAGE_EXTRACT_AND_RUN=1 ./TraceForge-0.7.5-x86_64.AppImage
```

## Build from source

Requires x86_64 Linux and an internet connection. Run the builder as your normal user; only package installation uses sudo.

### Build prerequisites by distro

Install the base tools using the commands for your distro. These include CMake, zlib development files, and the XCB cursor library. The builder checks the other desktop libraries and offers to install any that are missing.

**Ubuntu / Debian / Linux Mint / Pop!_OS / PureOS**

```bash
sudo apt-get update
sudo apt-get install python3 ca-certificates build-essential cmake file binutils \
  zlib1g-dev libxcb-cursor0
```

**Arch Linux / Garuda / EndeavourOS / Manjaro**

```bash
sudo pacman -Syu --needed python ca-certificates base-devel cmake file binutils \
  zlib xcb-util-cursor
```

**Fedora**

```bash
sudo dnf install python3 ca-certificates gcc gcc-c++ make cmake file binutils \
  zlib-devel xcb-util-cursor
```

**openSUSE Leap / Tumbleweed**

```bash
sudo zypper refresh
sudo zypper install python3 ca-certificates gcc gcc-c++ make cmake file binutils \
  zlib-devel libxcb-cursor0
```

For `--portable`, also install `bubblewrap`:

| Distro | Command |
| --- | --- |
| Ubuntu / Debian family | `sudo apt-get install bubblewrap` |
| Arch family | `sudo pacman -Syu --needed bubblewrap` |
| Fedora | `sudo dnf install bubblewrap` |
| openSUSE | `sudo zypper install bubblewrap` |

Python 3.8+ starts the downloader; a compatible private Python and the Python libraries are set up automatically. No manual pip installation is needed.

### Build

Extract the source ZIP, open a terminal in that folder, and run:

```bash
chmod +x build.sh
./build.sh
```

The builder sets up a private Python automatically if needed, then checks for `HT1008_Software.zip`. If missing, choose **Locate ZIP** or **Download**. It fetches the driver and dependencies separately, extracts the vendor files, runs checks, and creates the AppImage in `dist/`. Downloads and build files stay in `work/`. Missing system tools and libraries are detected and offered for installation with sudo. System Python stays unchanged.

For a portable build using the Ubuntu 22.04 library baseline:

```bash
./build.sh --portable --work-dir ./portable-work
```

The builder offers to install `bubblewrap` if missing; unprivileged user namespaces must be enabled. The resulting AppImage targets glibc 2.35 or newer. A normal build uses the host's libraries.

Run `./build.sh --help` for options such as `--vendor-zip`, `--output-dir`, and `--no-gui`.

## Use

- **Connect + run** starts live viewing. **Record** asks for a name and saves automatically until **Finish recording**.
- Default recording folder: `~/Documents/Traceforge/YYYY-MM-DD/`. Change it with **Recording folder…**.
- Name or hide channels and drag their triangles to reposition traces. Hidden channels remain recorded; channel names are saved in recordings.
- **Noise filter** changes voltage precision: OFF `1.234 V`, LOW `1.23 V`, HIGH `1.2 V`. **Apply filter to graph** also rounds displayed traces.
- Recordings always retain unfiltered samples. Open a recording to review it, compare references, or export CSV/video.

## Checks and details

```bash
./TraceForge-0.7.5-x86_64.AppImage --self-test traceforge-check.json
```

This checks the package without capturing hardware samples. Keep the report when testing another distro.

Timing is nominal; see [TIMING.md](TIMING.md). Dependency sources and checksums are in [builder/sources.json](builder/sources.json). The source ZIP contains only our code; downloaded components retain their own licenses.
