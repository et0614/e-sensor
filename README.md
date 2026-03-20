# E-Sensor Project

This project provides a complete solution for a MIDI-based sensor device, including hardware design files, microcontroller firmware, and a Python client library.

## Directory Structure

The repository is organized as follows:

```text
.
├── firmware/                # Microcontroller Firmware (MPLAB X IDE)
│   ├── E_Sensor_Main.X/     # Main controller (USB-MIDI & System Logic)
│   └── E_Sensor_Velocity.X/ # Sub-controller (Air velocity sensor processing)
├── hardware/                # Hardware Design Materials
│   ├── pcb/                 # PCB layout and schematics
│   ├── datasheets/          # Technical specifications for key components
│   └── BOM.xlsx             # Bill of Materials
├── software/                # PC-side Applications & Libraries
│   ├── e_sensor.py          # Python client library for E-Sensor
│   └── requirements.txt     # Python dependency list
└── .gitignore               # Git ignore rules for MPLAB X and Python
```

---

## Directory Details

### 1. Firmware (`/firmware`)
Contains MPLAB X IDE projects for the onboard microcontrollers.

* **E_Sensor_Main.X**: 
    * Handles USB-MIDI communication using the TinyUSB stack.
    * Manages system-level logic and MIDI SysEx protocol.
    * Configured via MCC (MPLAB Code Configurator).
* **E_Sensor_Velocity.X**: 
    * Dedicated firmware for the air velocity sensor subunit.
    * Focuses on high-speed signal processing and data filtering.

### 2. Hardware (`/hardware`)
Includes all physical design assets.

* **pcb/**: Original CAD files for the board. A `Schematic.pdf` is provided for quick reference without needing specialized CAD software.
* **datasheets/**: A collection of PDFs for the ICs and sensors used in this design, ensuring all technical references are available offline.

### 3. Software (`/software`)
Tools for interfacing with the device from a computer.

* **e_sensor.py**: 
    * A lightweight Python class that wraps the MIDI SysEx protocol.
    * Supports real-time data polling, device ID retrieval, and coefficient calibration.
* **requirements.txt**: List of necessary Python packages (e.g., `mido`, `python-rtmidi`).

---

## Getting Started

### Firmware Development
* **IDE**: MPLAB X IDE
* **Compiler**: Microchip XC8
* **Plugin**: MCC (MPLAB Code Configurator)

### Python Environment
Install dependencies using pip:
```bash
pip install -r software/requirements.txt
```

---