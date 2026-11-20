# Third-party notices

Pudding Terminal uses the Python packages below. See [requirements.txt](requirements.txt) for versions and [requirements-win-x64.lock](requirements-win-x64.lock) for wheel hashes. Users install the packages from their respective publishers. This repository contains the application source and installation files, and the linked upstream licenses cover the dependencies.

| Component | Upstream license and source |
| --- | --- |
| PyQt6 6.11.0 | GPL-3.0-only for the PyPI binding. Riverbank also offers a commercial license. [Riverbank](https://www.riverbankcomputing.com/software/pyqt/intro) [PyPI](https://pypi.org/project/PyQt6/6.11.0/) |
| PyQt6-Qt6 6.11.2 and PyQt6-sip 13.13.0 | Qt wheel LGPLv3 with component-specific terms. SIP runtime BSD-2-Clause. [Qt wheel](https://pypi.org/project/PyQt6-Qt6/6.11.2/) [SIP wheel](https://pypi.org/project/PyQt6-sip/13.13.0/) |
| AsyncSSH 2.24.1 | EPL-2.0 OR GPL-2.0-or-later. For the GPL-3.0-only application, the relevant option is the GPLv3 branch. [Versioned LICENSE](https://github.com/ronf/asyncssh/blob/v2.24.1/LICENSE) [Versioned COPYRIGHT](https://github.com/ronf/asyncssh/blob/v2.24.1/COPYRIGHT) |
| pyte 0.8.2 and wcwidth 0.9.4 | pyte LGPLv3 with its AUTHORS notice. wcwidth MIT, including applicable Unicode notices. [pyte](https://pypi.org/project/pyte/0.8.2/) [wcwidth](https://pypi.org/project/wcwidth/0.9.4/) |
| pyserial 3.5 and telnetlib3 5.0.1 | BSD-3-Clause and ISC. [pyserial LICENSE.txt](https://github.com/pyserial/pyserial/blob/v3.5/LICENSE.txt) [telnetlib3](https://pypi.org/project/telnetlib3/5.0.1/) |
| cryptography 50.0.2, cffi 2.1.1, pycparser 3.12 and typing_extensions 4.16.0 | Apache-2.0 OR BSD-3-Clause, MIT-0, BSD-3-Clause and PSF-2.0 respectively. [cryptography](https://pypi.org/project/cryptography/50.0.2/) [cffi](https://pypi.org/project/cffi/2.1.1/) [pycparser](https://pypi.org/project/pycparser/3.12/) [typing_extensions](https://pypi.org/project/typing_extensions/4.16.0/) |
| blessed 1.52.0, jinxed 2.1.0 and ansicon 1.89.0 | Windows dependencies declared by the Telnet stack. blessed uses MIT, jinxed and the ansicon Python wrapper use MPL-2.0, and jinxed includes ncurses notices. The ansicon wheel's native libraries have separate provenance and license terms. The repository includes no redistributable wheels or native libraries. [blessed](https://pypi.org/project/blessed/1.52.0/) [jinxed](https://pypi.org/project/jinxed/2.1.0/) [ansicon](https://pypi.org/project/ansicon/1.89.0/) |

PuTTY is named in the app to describe keyboard and mouse compatibility modes. The PuTTY 0.75 manual was used as a behavior reference. The terminal uses Qt input handling, Pyte for VT parsing and AsyncSSH for SSH. The repository includes no PuTTY binaries, artwork, or documentation copies. [PuTTY manual](https://the.earth.li/~sgtatham/putty/0.75/htmldoc/Chapter4.html) [PuTTY upstream license](https://www.chiark.greenend.org.uk/~sgtatham/putty/licence.html)

Any future third-party binary distribution requires a separate review of the exact payload, including upstream notices, corresponding source and library replacement or rebuild obligations. [GPLv3](https://www.gnu.org/licenses/gpl-3.0.html) [Qt licensing](https://www.qt.io/development/open-source-lgpl-obligations)
