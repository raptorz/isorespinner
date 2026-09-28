# isorespinner

旧版 `isorespin.sh` 的 IA32 UEFI 适配、适用镜像、使用方法及验证状态见
[isorespin 文档](docs/isorespin.md)。`--atom` 旧驱动注入已移除；
Mint 20.3 保留原流程，Mint 22.3 和 Ubuntu/Xubuntu 26.04 使用独立的新适配模块。

Linuxium's script to respin an Ubuntu* desktop ISO and optionally add/remove functionality like kernels/repositories/packages/files/boot parameters etc., run pre and post commands and add support for a 32-bit bootloader. Copyright (C) 2022 Ian W. Morrison (linuxium@linuxium.com.au)



This script is a refinement of 'isorespin.sh' (http://url.linuxium.com.au/isorespin_sh) which was initially based on the information documented on the following sites:
https://help.ubuntu.com/community/LiveCDCustomization (shared under a Creative Commons Attribution-ShareAlike 3.0 License available at https://help.ubuntu.com/community/License)
https://wiki.ubuntu.com/KernelTeam/GitKernelBuild (shared under a Creative Commons Attribution-ShareAlike 3.0 License available at https://help.ubuntu.com/community/License)
and then further developed by Linuxium (linuxium@linuxium.com.au).
Version 1.0.0 to 1.0.2: This work is licensed under GNU GPL version 3.

Linuxium's script to respin an Ubuntu, Ubuntu Unity, Kubuntu, Lubuntu, Ubuntu Budgie, Ubuntu GNOME, Ubuntu MATE, Xubuntu or Linux Mint desktop ISO and optionally add/remove functionality like kernels/repositories/packages/files/boot parameters etc., run pre and post commands and add support for a 32-bit bootloader.
Copyright (C) 2022 Ian W. Morrison (linuxium@linuxium.com.au).

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with this program. If not, see <http://www.gnu.org/licenses/>.
